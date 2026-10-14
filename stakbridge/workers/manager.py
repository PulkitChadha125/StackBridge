"""Gateway side of the workers: starts one process per account, sends commands, tracks status."""

import itertools
import logging
import multiprocessing
import threading
import time
from concurrent.futures import Future

from .process import worker_main

log = logging.getLogger("stakbridge.workers")
_ctx = multiprocessing.get_context("spawn")  # the only start method on Windows; also safest elsewhere


NOT_SENT = {"status": "skipped", "reason": "DISCONNECTED"}


class WorkerGone(Exception):
    """The worker process ended before answering. The command may or may not have run."""


class WorkerHandle:
    def __init__(self, account_id: str, config: dict):
        self.account_id = account_id
        self.config = config
        self.status = "starting"
        self.reason: str | None = None
        self.info: dict = {}
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._stopping = False
        self._conn, child_conn = _ctx.Pipe()
        self._process = _ctx.Process(
            target=worker_main, args=(child_conn, config), name=f"stakbridge-{account_id}", daemon=True
        )
        self._process.start()
        child_conn.close()
        threading.Thread(target=self._read_loop, name=f"reader-{account_id}", daemon=True).start()

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def send(self, cmd: str, **fields) -> Future:
        """Sends a command. If the worker is down the command is never sent, and the
        future resolves to 'skipped' so callers know for certain nothing happened."""
        future: Future = Future()
        with self._lock:
            if self._stopping or not self._process.is_alive():
                future.set_result(dict(NOT_SENT))
                return future
            message_id = next(self._ids)
            self._pending[message_id] = future
            try:
                self._conn.send({"id": message_id, "cmd": cmd, **fields})
            except (OSError, BrokenPipeError):
                del self._pending[message_id]
                future.set_result(dict(NOT_SENT))
        return future

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._conn.recv()
                if message.get("event") == "status":
                    self.status, self.reason, self.info = message["status"], message["reason"], message["info"]
                    log.info("account %s is %s %s", self.account_id, self.status, self.reason or "")
                    continue
                with self._lock:
                    future = self._pending.pop(message["id"], None)
                if future is not None:
                    future.set_result(message["result"])
        except (EOFError, OSError):
            pass
        self.status, self.reason, self.info = "disconnected", "WORKER_STOPPED", {}
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for future in pending:
            future.set_exception(WorkerGone(self.account_id))

    def stop(self, wait: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            try:
                self._conn.send({"id": 0, "cmd": "shutdown"})
            except (OSError, BrokenPipeError):
                pass
        self._process.join(wait)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(2)
        self._conn.close()


class WorkerManager:
    """Keeps one worker running for every enabled account and restarts any that crash."""

    def __init__(self, settings):
        self.settings = settings
        self._workers: dict[str, WorkerHandle] = {}
        self._configs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._supervisor: threading.Thread | None = None

    def _worker_config(self, account: dict, password: str) -> dict:
        return {
            "account_id": account["id"],
            "login": account["login"],
            "password": password,
            "server": account["server"],
            "terminal_path": account["terminal_path"],
            "backend": self.settings.backend,
            "status_interval": self.settings.status_interval,
            "deviation_points": self.settings.deviation_points,
            "allow_live": self.settings.allow_live,
        }

    def start(self) -> None:
        self._running = True
        self._supervisor = threading.Thread(target=self._supervise, name="worker-supervisor", daemon=True)
        self._supervisor.start()

    def ensure(self, account: dict, password: str) -> None:
        """Starts the account's worker, or restarts it if its connection details changed."""
        config = self._worker_config(account, password)
        with self._lock:
            current = self._workers.get(account["id"])
            if current is not None and self._configs.get(account["id"]) == config and current.alive:
                return
            self._configs[account["id"]] = config
            old = self._workers.pop(account["id"], None)
        if old is not None:
            old.stop()
        handle = WorkerHandle(account["id"], config)
        with self._lock:
            self._workers[account["id"]] = handle

    def remove(self, account_id: str) -> None:
        with self._lock:
            handle = self._workers.pop(account_id, None)
            self._configs.pop(account_id, None)
        if handle is not None:
            handle.stop()

    def get(self, account_id: str) -> WorkerHandle | None:
        with self._lock:
            return self._workers.get(account_id)

    def status(self, account_id: str) -> dict:
        handle = self.get(account_id)
        if handle is None:
            return {"status": "stopped", "reason": None, "info": {}}
        return {"status": handle.status, "reason": handle.reason, "info": handle.info}

    def _supervise(self) -> None:
        while self._running:
            time.sleep(2)
            with self._lock:
                dead = [(aid, h, self._configs[aid]) for aid, h in self._workers.items() if not h.alive]
            for account_id, old, config in dead:
                if not self._running:
                    return
                log.warning("worker for %s stopped, restarting", account_id)
                old.stop(wait=0)
                handle = WorkerHandle(account_id, config)
                with self._lock:
                    if account_id in self._configs:
                        self._workers[account_id] = handle
                        continue
                handle.stop()  # account was removed meanwhile

    def stop_all(self) -> None:
        self._running = False
        with self._lock:
            handles = list(self._workers.values())
            self._workers.clear()
            self._configs.clear()
        for handle in handles:
            handle.stop()
