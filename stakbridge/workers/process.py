"""Worker process: owns exactly one broker connection and runs its commands one by one.

MT5's Python bridge can only drive one terminal per process, which is why each account
gets its own worker. Commands arrive over a pipe from the gateway, and replies and
status events go back the same way.

Messages from the gateway:  {"id": int, "cmd": "open"|"close"|"positions"|"set_offline"|"shutdown", ...}
Replies to the gateway:     {"id": int, "result": {...}}
Status events:              {"event": "status", "status": ..., "reason": ..., "info": {...}}
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

            if not conn.poll(0.2):
                continue
            message = conn.recv()
            if message["cmd"] == "shutdown":
                break
            result = _run(backend, message, state["status"])
            conn.send({"id": message["id"], "result": result})
            if message["cmd"] == "set_offline":
                next_check = 0.0
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass  # gateway went away
    finally:
        backend.shutdown()


def _run(backend, message: dict, status: str | None) -> dict:
    cmd = message["cmd"]
    try:
        if cmd == "set_offline":
            if not hasattr(backend, "set_offline"):
                return {"status": "rejected", "reason": "ONLY_FOR_FAKE_BACKEND"}
            backend.set_offline(message["offline"])
            return {"status": "ok"}
        # The gateway already skips disconnected accounts; this covers a drop in between.
        if status != "connected":
            return {"status": "skipped", "reason": "DISCONNECTED"}
        if cmd == "open":
            return backend.open(message["order"])
        if cmd == "close":
            return backend.close(message["magic"], message.get("symbol"))
        if cmd == "positions":
            return backend.positions(message.get("magic"))
        return {"status": "rejected", "reason": f"UNKNOWN_COMMAND: {cmd}"}
    except Exception as exc:  # never let one bad order kill the worker
        traceback.print_exc()
        return {"status": "rejected", "reason": f"WORKER_ERROR: {exc}"}
