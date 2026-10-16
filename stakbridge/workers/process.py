"""Worker process: owns exactly one broker connection and runs its commands one by one.

MT5's Python bridge can only drive one terminal per process, which is why each account
gets its own worker. Commands arrive over a pipe from the gateway, and replies and
status events go back the same way.

Messages from the gateway:  {"id": int, "cmd": "open"|"close"|"positions"|"close_position"|"modify_position"
                                            |"modify_order"|"cancel_order"|"fill_order"|"account_info"|"watch"
                                            |"set_offline"|"fill_pending"|"shutdown", ...}
Replies to the gateway:     {"id": int, "result": {...}}
Status events:              {"event": "status", "status": ..., "reason": ..., "info": {...}}
Position events (copier master only, while watching: config["watch_ms"] or the "watch" command).
Waiting limit/stop orders are included with "pending": True; when one fills it is reported as
"modified" with "pending": False (a position keeps its order's ticket).
                            {"event": "positions", "kind": "baseline", "positions": [...], "at": perf_counter seconds}
                            {"event": "positions", "kind": "diff", "opened": [...], "closed": [...],
                             "modified": [...], "at": perf_counter seconds}
"""

import time
import traceback

from .backends import make_backend

RECONNECT_DELAY = 10.0


def worker_main(conn, config: dict) -> None:
    backend = make_backend(config)
    state = {"status": None, "reason": None, "info": None}
    next_check = 0.0
    last_connect_attempt = -RECONNECT_DELAY
    watch_interval = config["watch_ms"] / 1000 if config.get("watch_ms") else None
    known = None  # positions last reported to the gateway; None until a baseline was sent

    def publish(snapshot: dict) -> None:
        if (snapshot["status"], snapshot["reason"], snapshot["info"]) != (state["status"], state["reason"], state["info"]):
            state.update(snapshot)
            conn.send({"event": "status", **snapshot})

    try:
        while True:
            now = time.monotonic()
            if now >= next_check:
                snapshot = backend.check()
                if snapshot["status"] != "connected" and now - last_connect_attempt >= RECONNECT_DELAY:
                    last_connect_attempt = now
                    publish({"status": "connecting", "reason": None, "info": {}})
                    snapshot = backend.connect()
                publish(snapshot)
                next_check = now + config["status_interval"]

            watching = watch_interval is not None and state["status"] == "connected"
            if not watching:
                known = None  # after a reconnect the gateway gets a fresh baseline

            # On Windows a pipe poll with a timeout waits at least ~15 ms, while time.sleep is
            # precise to about 1 ms. So the watcher checks the pipe without waiting and sleeps instead.
            if conn.poll(0 if watching else 0.2):
                message = conn.recv()
                if message["cmd"] == "shutdown":
                    break
                if message["cmd"] == "watch":
                    watch_interval = message["watch_ms"] / 1000 if message["watch_ms"] else None
                    known = None
                    conn.send({"id": message["id"], "result": {"status": "ok"}})
                    continue
                result = _run(backend, message, state["status"])
                conn.send({"id": message["id"], "result": result})
                if message["cmd"] == "set_offline":
                    next_check = 0.0

            if watching:
                known = _watch(backend, conn, known)
                time.sleep(watch_interval)
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass  # gateway went away
    finally:
        backend.shutdown()


def _watch(backend, conn, known: dict | None) -> dict | None:
    """Compares the open positions with the last ones reported and sends what changed."""
    current = backend.snapshot(with_age=known is None)
    if current is None:
        return known  # could not read them: never treat that as "everything closed"
    if known is None:
        conn.send({"event": "positions", "kind": "baseline", "positions": list(current.values()), "at": time.perf_counter()})
        return current

    detected = time.perf_counter()
    if any(ticket not in current for ticket in known):
        # A close makes every client close too, so read once more to be sure it is real.
        current = backend.snapshot()
        if current is None:
            return known
    opened = [p for ticket, p in current.items() if ticket not in known]
    closed = [p for ticket, p in known.items() if ticket not in current]
    modified = [p for ticket, p in current.items() if ticket in known and _shape(p) != _shape(known[ticket])]
    if opened or closed or modified:
        conn.send({"event": "positions", "kind": "diff", "opened": opened, "closed": closed, "modified": modified,
                   "at": detected})
    return current


def _shape(item: dict) -> tuple:
    """What the copier follows: SL and TP, and for a waiting order its price and whether it filled."""
    pending = item.get("pending", False)
    return pending, item["sl"], item["tp"], item["price"] if pending else None


def _run(backend, message: dict, status: str | None) -> dict:
    cmd = message["cmd"]
    try:
        if cmd == "set_offline":
            if not hasattr(backend, "set_offline"):
                return {"status": "rejected", "reason": "ONLY_FOR_FAKE_BACKEND"}
            backend.set_offline(message["offline"])
            return {"status": "ok"}
        if cmd == "fill_pending":
            if not hasattr(backend, "fill_pending"):
                return {"status": "rejected", "reason": "ONLY_FOR_FAKE_BACKEND"}
            return backend.fill_pending(message["ticket"])
        # The gateway already skips disconnected accounts; this covers a drop in between.
        if status != "connected":
            return {"status": "skipped", "reason": "DISCONNECTED"}
        if cmd == "open":
            return backend.open(message["order"])
        if cmd == "close":
            return backend.close(message["magic"], message.get("symbol"))
        if cmd == "positions":
            return backend.positions(message.get("magic"))
        if cmd == "close_position":
            return backend.close_position(message["ticket"])
        if cmd == "modify_position":
            return backend.modify_position(message["ticket"], message["sl"], message["tp"])
        if cmd == "modify_order":
            return backend.modify_order(message["ticket"], message["price"], message["sl"], message["tp"])
        if cmd == "cancel_order":
            return backend.cancel_order(message["ticket"])
        if cmd == "fill_order":
            return backend.fill_order(message["ticket"], message["order"])
        if cmd == "account_info":
            return backend.account_info()
        return {"status": "rejected", "reason": f"UNKNOWN_COMMAND: {cmd}"}
    except Exception as exc:  # never let one bad order kill the worker
        traceback.print_exc()
        return {"status": "rejected", "reason": f"WORKER_ERROR: {exc}"}
