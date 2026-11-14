"""Worker process: owns exactly one broker connection and runs its commands one by one.

MT5's Python bridge can only drive one terminal per process, which is why each account
gets its own worker. Commands arrive over a pipe from the gateway, and replies and
status events go back the same way.

Messages from the gateway:  {"id": int, "cmd": "open"|"close"|"positions"|"close_position"|"modify_position"
                                            |"modify_order"|"cancel_order"|"fill_order"|"account_info"|"watch"
                                            |"confirm_gone"|"set_offline"|"fill_pending"|"glitch"|"shutdown", ...}
Replies to the gateway:     {"id": int, "result": {...}}
Status events:              {"event": "status", "status": ..., "reason": ..., "info": {...}}
Position events (copier master only, while watching: config["watch_ms"] or the "watch" command).
Waiting limit/stop orders are included with "pending": True; when one fills it is reported as
"modified" with "pending": False (a position keeps its order's ticket).
                            {"event": "positions", "kind": "baseline", "positions": [...], "at": perf_counter seconds}
                            {"event": "positions", "kind": "diff", "opened": [...], "closed": [...],
                             "modified": [...], "at": perf_counter seconds}
Log events (shown in the app log):  {"event": "log", "level": "warning"|"info", "message": str}

A master trade that is missing from a read is reported closed only when the terminal is connected, still logged into
this account, and its history has the close (the closing deal, or the deleted order). While MetaTrader logs in again
it shows no trades at all for a moment, which must never close every client copy. If the history still has no close
after CONFIRM_GONE_SECONDS while the terminal is healthy, the trade is reported closed anyway.
"""

import sys
import time
import traceback

from .backends import make_backend

RECONNECT_DELAY = 10.0
CONFIRM_GONE_SECONDS = 30.0


def worker_main(conn, config: dict) -> None:
    _give_output_a_home()
    try:
        backend = make_backend(config)
    except Exception as exc:  # e.g. the MetaTrader5 library cannot load: say so instead of crashing in a loop
        traceback.print_exc()
        _serve_broken(conn, f"MT5_LIBRARY_FAILED: the MetaTrader5 Python library could not load ({exc})")
        return
    state = {"status": None, "reason": None, "info": None}
    next_check = 0.0
    last_connect_attempt = -RECONNECT_DELAY
    watch_interval = config["watch_ms"] / 1000 if config.get("watch_ms") else None
    known = None  # positions last reported to the gateway; None until a baseline was sent
    unconfirmed: dict[int, float] = {}  # tickets missing from the reads but not yet confirmed closed -> first missed

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
                unconfirmed.clear()

            # On Windows a pipe poll with a timeout waits at least ~15 ms, while time.sleep is
            # precise to about 1 ms. So the watcher checks the pipe without waiting and sleeps instead.
            if conn.poll(0 if watching else 0.2):
                message = conn.recv()
                if message["cmd"] == "shutdown":
                    break
                if message["cmd"] == "watch":
                    watch_interval = message["watch_ms"] / 1000 if message["watch_ms"] else None
                    known = None
                    unconfirmed.clear()
                    conn.send({"id": message["id"], "result": {"status": "ok"}})
                    continue
                result = _run(backend, message, state["status"])
                conn.send({"id": message["id"], "result": result})
                if message["cmd"] == "set_offline":
                    next_check = 0.0

            if watching:
                known = _watch(backend, conn, known, unconfirmed)
                time.sleep(watch_interval)
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass  # gateway went away
    finally:
        backend.shutdown()


def _give_output_a_home() -> None:
    """In the windowless EXE a worker has no stdout/stderr, so its first error message would crash it.
    Its prints and errors go to the daily log file instead (logs\stakbridge-YYYY-MM-DD.log)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .. import logfiles
    from ..licensing import paths

    logfiles.install(paths.device_root() / "logs", capture_output=True)


def _serve_broken(conn, reason: str) -> None:
    """Shows the account as Error with the reason, and refuses every command, until StakBridge stops it."""
    try:
        conn.send({"event": "status", "status": "error", "reason": reason, "info": {}})
        while True:
            message = conn.recv()
            if message["cmd"] == "shutdown":
                return
            conn.send({"id": message["id"], "result": {"status": "rejected", "reason": reason}})
    except (EOFError, BrokenPipeError, OSError, KeyboardInterrupt):
        pass


def _watch(backend, conn, known: dict | None, unconfirmed: dict[int, float]) -> dict | None:
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
    held = _held_back(backend, conn, [known[t] for t in known if t not in current], unconfirmed)
    back = [t for t in unconfirmed if t in current]  # in the list again (MetaTrader had logged in again)
    for ticket in back:
        del unconfirmed[ticket]
    if back:
        _log(conn, "info", f"master trade(s) {_tickets(back)} shown again: they never closed, nothing was changed")
    opened = [p for ticket, p in current.items() if ticket not in known]
    closed = [p for ticket, p in known.items() if ticket not in current and ticket not in held]
    modified = [p for ticket, p in current.items() if ticket in known and _shape(p) != _shape(known[ticket])]
    if opened or closed or modified:
        conn.send({"event": "positions", "kind": "diff", "opened": opened, "closed": closed, "modified": modified,
                   "at": detected})
    return {**current, **held}


def _held_back(backend, conn, missing: list[dict], unconfirmed: dict[int, float]) -> dict[int, dict]:
    """The missing trades that are not reported closed yet: no close in the terminal's history (or the terminal is
    not connected and logged in, so nothing it shows can be trusted)."""
    if not missing:
        return {}
    gone = backend.confirm_gone(missing)
    now, held, new, expired = time.monotonic(), {}, [], []
    for item in missing:
        ticket = item["ticket"]
        if gone is not None and gone.get(ticket):
            unconfirmed.pop(ticket, None)
            continue
        first = unconfirmed.get(ticket)
        if first is None:
            unconfirmed[ticket] = now
            new.append(ticket)
        elif gone is not None and now - first >= CONFIRM_GONE_SECONDS:
            del unconfirmed[ticket]
            expired.append(ticket)
            continue
        held[ticket] = item
    if new:
        why = "MetaTrader is not connected and logged in" if gone is None else "its history has no close for them"
        _log(conn, "warning", f"master trade(s) {_tickets(new)} no longer shown, but {why} (MetaTrader logging in "
                              "again?): not treated as closed, client copies stay open")
    if expired:
        _log(conn, "warning", f"master trade(s) {_tickets(expired)} gone for {CONFIRM_GONE_SECONDS:.0f} s while "
                              "MetaTrader is connected, without a close in its history: treated as closed")
    return held


def _tickets(tickets: list[int]) -> str:
    return ", ".join(map(str, tickets[:20])) + (f" and {len(tickets) - 20} more" if len(tickets) > 20 else "")


def _log(conn, level: str, message: str) -> None:
    conn.send({"event": "log", "level": level, "message": message})


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
        if cmd == "glitch":
            if not hasattr(backend, "glitch"):
                return {"status": "rejected", "reason": "ONLY_FOR_FAKE_BACKEND"}
            backend.glitch(message["on"])
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
        if cmd == "confirm_gone":
            gone = backend.confirm_gone(message["items"])
            if gone is None:
                return {"status": "rejected", "reason": "TERMINAL_NOT_READY"}
            return {"status": "ok", "gone": gone}
        if cmd == "account_info":
            return backend.account_info()
        return {"status": "rejected", "reason": f"UNKNOWN_COMMAND: {cmd}"}
    except Exception as exc:  # never let one bad order kill the worker
        traceback.print_exc()
        return {"status": "rejected", "reason": f"WORKER_ERROR: {exc}"}
