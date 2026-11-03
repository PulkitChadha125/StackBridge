"""Saves the copier's records on a thread of its own, so a slow or full disk never holds up an order.

The copier works from memory and only hands its changes over: rows of the `copies` table (the latest version of each
row wins) and other database calls (the Orders tab, the master's seen tickets), which run in the order given.
Lookups the copier needs from the database go through here too, after the changes before them are saved.

When a save fails because of the disk (full, locked, I/O error), everything is kept in memory and tried again until
it works; meanwhile `problem` says so and the page shows it. Copying goes on as normal.
"""

import logging
import sqlite3
import threading
from collections import deque

from .db import Database, now_iso

log = logging.getLogger("stakbridge.store")

RETRY_MIN, RETRY_MAX = 1.0, 10.0


def _key(row: dict) -> tuple:
    return row["master_id"], row["master_ticket"], row["client_id"]


class Store:
    def __init__(self, db: Database):
        self.db = db
        self.problem: str | None = None  # why saving does not work right now, for the page
        self._wake = threading.Condition()
        self._copies: dict[tuple, tuple[dict, str]] = {}  # copy rows waiting to be saved: key -> (row, changed at)
        self._calls: deque = deque()  # (function, args, kwargs, then) waiting to run, in order
        self._busy = False
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._wake:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._run, name="store", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Saves what is still waiting (for up to `timeout` seconds), then stops."""
        if not self.wait_idle(timeout):
            log.error("StakBridge stopped before it could save every change (%s)", self.problem or "disk too slow")
        with self._wake:
            self._running = False
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(2)

    # ---- called by the copier ----

    def save_copies(self, rows: list[dict]) -> None:
        """Queues copy rows to be saved. Each row is copied now: the copier keeps changing its own."""
        stamp = now_iso()
        with self._wake:
            for row in rows:
                self._copies[_key(row)] = ({k: v for k, v in row.items() if not k.startswith("_")}, stamp)
            self._wake.notify_all()

    def call(self, function, *args, then=None, **kwargs) -> None:
        """Runs a database call on the store's thread, after everything queued before it. `then` gets its result
        (or None if it failed)."""
        with self._wake:
            self._calls.append((function, args, kwargs, then))
            self._wake.notify_all()

    def find_copy(self, master_id: str, client_id: str, master_ticket: int | None = None,
                  client_ticket: int | None = None) -> dict | None:
        """Database.find_copy that also sees rows still waiting to be saved. Run it through call()."""
        with self._wake:
            waiting = [row for row, _ in self._copies.values() if row["master_id"] == master_id
                       and row["client_id"] == client_id
                       and (row["master_ticket"] == master_ticket if master_ticket is not None
                            else row.get("client_ticket") == client_ticket)]
        if waiting:
            return dict(max(waiting, key=lambda row: row.get("created_at") or ""))
        return self.db.find_copy(master_id, client_id, master_ticket=master_ticket, client_ticket=client_ticket)

    def wait_idle(self, timeout: float) -> bool:
        """Waits until everything queued is saved. False if that took longer than `timeout` seconds."""
        with self._wake:
            if self._thread is None:  # not started (tests, start-up): save on this thread
                pass
            else:
                return self._wake.wait_for(lambda: not self._copies and not self._calls and not self._busy, timeout)
        try:
            self._save_all_now()
        except Exception as exc:  # kept: the thread tries again once it runs
            self._failed(exc)
            return False
        return True

    # ---- the store's thread ----

    def _run(self) -> None:
        delay = RETRY_MIN
        while True:
            with self._wake:
                self._wake.wait_for(lambda: self._copies or self._calls or not self._running)
                if not self._running and not self._copies and not self._calls:
                    return
                self._busy = True
            try:
                self._save_copies()
                while self._next_call():
                    pass
            except Exception as exc:  # the disk (full, locked, read-only, I/O error); the thread must never end
                if not isinstance(exc, sqlite3.OperationalError):
                    log.exception("saving failed")
                self._failed(exc)
                with self._wake:
                    self._busy = False
                    self._wake.notify_all()
                    self._wake.wait(delay)
                delay = min(delay * 2, RETRY_MAX)
                continue
            delay = RETRY_MIN
            if self.problem is not None:
                self.problem = None
                log.warning("saving works again: every change made meanwhile is saved")
            with self._wake:
                self._busy = False
                self._wake.notify_all()

    def _save_copies(self) -> None:
        with self._wake:
            batch = dict(self._copies)
        if not batch:
            return
        try:
            self.db.save_copies([row for row, _ in batch.values()], stamps=[stamp for _, stamp in batch.values()])
        except sqlite3.OperationalError:
            raise  # the disk: kept and tried again
        except Exception:  # a bad row (a bug, not the disk): save the others one by one, drop only that one
            for key, (row, stamp) in batch.items():
                try:
                    self.db.save_copies([row], stamps=[stamp])
                except sqlite3.OperationalError:
                    raise
                except Exception:
                    log.exception("could not save the copy of master ticket %s on %s", key[1], key[2])
        with self._wake:
            for key, entry in batch.items():
                if self._copies.get(key) is entry:  # not changed again meanwhile
                    del self._copies[key]

    def _next_call(self) -> bool:
        with self._wake:
            if not self._calls:
                return False
            function, args, kwargs, then = self._calls[0]
        result = None
        try:
            result = function(*args, **kwargs)
        except sqlite3.OperationalError:
            raise  # the disk: kept and tried again
        except Exception:  # a bug, not the disk: trying again would only block everything behind it
            log.exception("could not save %s", getattr(function, "__name__", function))
        with self._wake:
            self._calls.popleft()
        if then is not None:
            try:
                then(result)
            except Exception:
                log.exception("store callback failed")
        return True

    def _failed(self, exc: Exception) -> None:
        text = str(exc)
        if "full" in text.lower():
            problem = f"DISK_FULL: the disk is full ({text}). Trades are still copied but not saved: free disk space"
        else:
            problem = f"SAVE_FAILED: {text}. Trades are still copied but not saved: StakBridge keeps trying"
        if problem != self.problem:
            self.problem = problem
            log.error("%s", problem)

    def _save_all_now(self) -> None:
        """Before the thread runs (start-up, tests): saves on the caller's thread."""
        self._save_copies()
        while self._next_call():
            pass
