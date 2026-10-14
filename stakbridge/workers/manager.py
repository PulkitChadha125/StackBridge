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
    def __init__(self, account_id: str, config: dict, on_event=None):
        self.account_id = account_id
        self.config = config
        self._on_event = on_event  # called with (account_id, message) for position events
        self._last_logged: tuple | None = None  # last status written to the app log, so retries don't repeat it
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
                    self._log_status()
                    continue
                if message.get("event") == "positions":
                    if self._on_event is not None:
                        try:
                            self._on_event(self.account_id, message)
                        except Exception:  # a copier bug must not stop replies from this worker
                            log.exception("copier failed on a position change from %s", self.account_id,
                                          extra={"account": self.account_id})
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

    def _log_status(self) -> None:
        """Writes connects and problems to the app log. "connecting" is skipped, and a problem is
        written once, not on every retry."""
        if self.status in ("starting", "connecting"):
            return
        current = (self.status, self.reason)
        if current == self._last_logged:
            return
        self._last_logged = current
        extra = {"account": self.account_id}
        if self.status == "error":
            log.error("%s cannot connect: %s", self.account_id, self.reason, extra=extra)
        elif self.status == "disconnected":
            log.warning("%s disconnected: %s", self.account_id, self.reason, extra=extra)
        elif self.reason:  # connected, but with a warning such as ALGO_TRADING_OFF_IN_TERMINAL
            log.warning("%s connected, but: %s", self.account_id, self.reason, extra=extra)
        else:
            log.info("%s connected (balance %s %s)", self.account_id, self.info.get("balance"),
                     self.info.get("currency", ""), extra=extra)

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
        # Set by the copier: receives position events from a watched (master) account.
        self.on_event = None

    def _emit(self, account_id: str, message: dict) -> None:
        if self.on_event is not None:
            self.on_event(account_id, message)

    def _worker_config(self, account: dict, password: str, watch: bool) -> dict:
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
            "watch_ms": self.settings.copy_poll_ms if watch else None,
        }

    def start(self) -> None:
        self._running = True
        self._supervisor = threading.Thread(target=self._supervise, name="worker-supervisor", daemon=True)
        self._supervisor.start()

    def ensure(self, account: dict, password: str, watch: bool = False) -> None:
        """Starts the account's worker, or restarts it if its connection details changed.

        watch=True makes the worker report every change of its open positions (copier master)."""
        config = self._worker_config(account, password, watch)
        with self._lock:
            current = self._workers.get(account["id"])
            if current is not None and self._configs.get(account["id"]) == config and current.alive:
                return
            self._configs[account["id"]] = config
            old = self._workers.pop(account["id"], None)
        if old is not None:
            old.stop()
        handle = WorkerHandle(account["id"], config, self._emit)
        with self._lock:
            self._workers[account["id"]] = handle

    def set_watch(self, account_id: str, watch: bool) -> None:
        """Starts or stops position reports from a running worker without logging it in again."""
        watch_ms = self.settings.copy_poll_ms if watch else None
        with self._lock:
            config = self._configs.get(account_id)
            if config is None or config["watch_ms"] == watch_ms:
                return
            config["watch_ms"] = watch_ms  # a restarted worker keeps it
            handle = self._workers.get(account_id)
        if handle is not None:
            handle.send("watch", watch_ms=watch_ms)

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
                log.warning("the worker for %s stopped unexpectedly, restarting it", account_id,
                            extra={"account": account_id})
                old.stop(wait=0)
                handle = WorkerHandle(account_id, config, self._emit)
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
