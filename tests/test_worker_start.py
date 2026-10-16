"""A worker whose broker library cannot load reports why, instead of crashing and restarting in a loop."""

import multiprocessing
import threading

from stakbridge.workers import process


def test_broken_library_shows_the_reason_and_refuses_commands(monkeypatch):
    def broken(config):
        raise ImportError("numpy._core.multiarray failed to import")

    monkeypatch.setattr(process, "make_backend", broken)
    gateway, worker = multiprocessing.Pipe()
    thread = threading.Thread(target=process.worker_main, args=(worker, {"backend": "mt5"}), daemon=True)
    thread.start()

    status = gateway.recv()
    assert status["event"] == "status" and status["status"] == "error"
    assert status["reason"].startswith("MT5_LIBRARY_FAILED") and "numpy" in status["reason"]

    gateway.send({"id": 7, "cmd": "open", "order": {}})
    reply = gateway.recv()
    assert reply["id"] == 7 and reply["result"]["status"] == "rejected"

    gateway.send({"id": 0, "cmd": "shutdown"})
    thread.join(5)
    assert not thread.is_alive()
