"""Trade copier: mirrors the master account's positions onto client accounts.

The master's worker checks its open positions every few milliseconds and reports what
changed (see workers/process.py). For every change the copier sends the matching command
to all client workers at once:

    master opens a position    -> each client opens master volume x its multiplier, same SL/TP
    master changes SL or TP    -> each client copy gets the same SL and TP
    master closes a position   -> each client copy is closed

The copier never waits for a broker. Commands go out immediately and each answer is handled
when it arrives, so a slow or hung client never holds up the others or the next master event.

Every client copy is tracked by its ticket in the `copies` table, so the copier only ever
touches positions it opened itself. Manual trades on a client are never touched.

Opens are blocked by the kill switch, by pausing the copier and by a client's trading switch.
Closes and SL/TP changes still go through when the copier is paused or the kill switch is on,
so clients can always follow the master out. A close or SL/TP change that fails (client
offline, broker refused) is retried until it succeeds.
"""

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from .db import Database, now_iso
from .workers import WorkerGone, WorkerManager

log = logging.getLogger("stakbridge.copier")

COPY_MAGIC = 990_000  # magic number of every copied position, so they are easy to spot in MT5
INVALID_STOPS = "BROKER_10016"  # TRADE_RETCODE_INVALID_STOPS
MAX_RETRY_DELAY = 60.0
COMMANDS = {"open": "open", "close": "close_position", "modify": "modify_position", "find": "positions"}
LIVE = ("pending", "open", "unknown")
# Open orders whose answer was lost for good. The copy is looked up on the client by its comment.
LOST_ANSWER = ("WORKER_STOPPED", "RESTARTED_WHILE_SENDING")


@dataclass
class _Job:
    kind: str  # "open" | "close" | "modify" | "find"
    row: dict
    future: Future
    # When the master change was seen. time.perf_counter is one precise clock shared by every process on this PC.
    detected_at: float
    sent_at: float
    fields: dict = field(default_factory=dict)
    done_at: float | None = None
    timed_out: bool = False


def _key(row: dict) -> tuple:
    return row["master_ticket"], row["client_id"]


class Copier:
    def __init__(self, db: Database, workers: WorkerManager, settings):
        self.db = db
        self.workers = workers
        self.timeout = settings.trade_timeout
        self.max_age = settings.copy_max_age
        self.retry_interval = settings.copy_retry_interval
        # All copier state is changed on this one thread, so no locks are needed. Nothing on it
        # waits for a broker: it only sends commands and handles answers as they arrive.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="copier")
        self._config = db.get_copier()
        self._accounts: dict[str, dict] = {}
        self._kill = False
        self._master_positions: dict[int, dict] | None = None  # None until the master's first report
        self._copies: dict[int, dict[str, dict]] = {}  # master ticket -> client id -> copy row
        self._inflight: dict[tuple, _Job] = {}  # the command each copy is waiting on
        self._retry_at: dict[tuple, float] = {}
        self._attempts: dict[tuple, int] = {}
        self._failed_stops: dict[tuple, tuple] = {}  # SL/TP the broker refused, not retried until they change
        self._dirty: dict[int, dict] = {}  # rows to save once the commands are out
        self._running = False
        workers.on_event = self._on_event
        self.reload()
        self._load_copies()

    # ---- control (called from the API) ----

    @property
    def master_id(self) -> str | None:
        return self._config.get("masterId")

    def reload(self) -> None:
        """Re-reads accounts and the kill switch. The API calls this after changing either."""
        self._accounts = {a["id"]: a for a in self.db.list_accounts()}
        self._kill = self.db.get_kill_switch()

    def configure(self, config: dict) -> None:
        def apply():
            changed = config.get("masterId") != self.master_id
            self._config = config
            if changed:
                # Copies of the old master stay in the database and are picked up again if it comes back.
                self._master_positions = None
                for state in (self._copies, self._inflight, self._retry_at, self._attempts, self._failed_stops):
                    state.clear()
                self._load_copies()

        self.reload()
        self._executor.submit(apply).result()

    def status(self) -> dict:
        positions = self._master_positions
        return {"watching": positions is not None, "masterPositions": None if positions is None else len(positions)}

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._retry_loop, name="copier-retry", daemon=True).start()

    def stop(self) -> None:
        self._running = False
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _submit(self, fn, *args) -> None:
        try:
            self._executor.submit(fn, *args)
        except RuntimeError:
            pass  # shutting down

    # ---- events from the master worker ----

    def _on_event(self, account_id: str, message: dict) -> None:
        if account_id == self.master_id:
            self._submit(self._handle, message)

    def _handle(self, message: dict) -> None:
        if message["kind"] == "baseline":
            self._baseline(message)
        elif self._master_positions is None:
            return  # a change report always follows a baseline; this one is from before a master switch
        else:
            self._diff(message)
        self._flush()
        self.db.set_seen_tickets(self.master_id, self._master_positions.keys())

    def _baseline(self, message: dict) -> None:
        """First report after the master (re)connects."""
        positions = {p["ticket"]: p for p in message["positions"]}
        self._master_positions = positions
        seen = self.db.get_seen_tickets(self.master_id)
        if seen is None:
            log.info("copier now watches %s: its %d open positions are not copied", self.master_id, len(positions))
            return
        # Opened while StakBridge was not looking. Copy only if still recent, the price may have moved on.
        missed = [p for t, p in positions.items() if t not in seen and t not in self._copies]
        fresh = [p for p in missed if p.get("ageS") is not None and p["ageS"] <= self.max_age]
        self._open(fresh, message["at"])
        for position in missed:
            if position not in fresh:
                log.warning("master position %s opened while not watching, too old to copy", position["ticket"])
                for client in self._config["clients"]:
                    row = self._new_row(position, client)
                    row.update(status="skipped", reason="TOO_OLD_AFTER_RESTART")
                    self._copies.setdefault(position["ticket"], {})[client["accountId"]] = row
                    self._dirty[id(row)] = row
        self._retry()  # closes and SL/TP changes the master made while not watched

    def _diff(self, message: dict) -> None:
        at = message["at"]
        for position in message["opened"] + message["modified"]:
            self._master_positions[position["ticket"]] = position
        for position in message["closed"]:
            self._master_positions.pop(position["ticket"], None)
        self._open(message["opened"], at)
        for position in message["closed"] + message["modified"]:
            for row in self._copies.get(position["ticket"], {}).values():
                self._follow_master(row, at, retrying=False)

    # ---- sending ----

    def _open(self, positions: list[dict], at: float) -> None:
        for position in positions:
            for client in self._config["clients"]:
                if client["accountId"] in self._copies.get(position["ticket"], {}):
                    continue
                row = self._new_row(position, client)
                self._copies.setdefault(position["ticket"], {})[client["accountId"]] = row
                skip = self._skip_reason(client["accountId"], opening=True)
                if skip:
                    row.update(status="skipped", reason=skip)
                    self._dirty[id(row)] = row
                    continue
                # Saved as "pending" once all orders are out, so a crash before the answer is noticed on restart.
                self._dirty[id(row)] = row
                self._send("open", row, at, order={
                    "symbol": row["symbol"], "side": row["side"], "type": "market", "volume": row["volume"],
                    "sl": position["sl"] or None, "tp": position["tp"] or None,
                    "comment": f"copy {position['ticket']}", "magic": COPY_MAGIC,
                })

    def _follow_master(self, row: dict, at: float, retrying: bool) -> None:
        """Brings one open copy in line with its master position: closes it, or fixes its SL/TP."""
        if row["status"] != "open" or self._master_positions is None:
            return
        key = _key(row)
        master = self._master_positions.get(row["master_ticket"])
        busy = self._inflight.get(key)
        if retrying and time.monotonic() < self._retry_at.get(key, 0):
            return
        skip = self._skip_reason(row["client_id"], opening=False)

        if master is None:
            if busy is not None and busy.kind == "close":
                return  # never two closes at once; a modify still running is fine, the close queues behind it
            if skip:
                if row["reason"] != f"CLOSE_WAITING: {skip}":
                    row["reason"] = f"CLOSE_WAITING: {skip}"  # closed as soon as the client is back
                    self._dirty[id(row)] = row
                return
            self._send("close", row, at, ticket=row["client_ticket"])
        elif (row["sl"], row["tp"]) != (master["sl"], master["tp"]):
            if busy is None and not skip and self._failed_stops.get(key) != (master["sl"], master["tp"]):
                self._send("modify", row, at, ticket=row["client_ticket"], sl=master["sl"], tp=master["tp"])

    def _send(self, kind: str, row: dict, at: float, **fields) -> None:
        handle = self.workers.get(row["client_id"])
        if handle is None:
            future: Future = Future()
            future.set_result({"status": "skipped", "reason": "DISCONNECTED"})
        else:
            future = handle.send(COMMANDS[kind], **fields)
        job = _Job(kind, row, future, at, time.perf_counter(), fields)
        self._inflight[_key(row)] = job
        future.add_done_callback(lambda _, j=job: self._answered(j))

    def _answered(self, job: _Job) -> None:
        # Runs on the thread that read the answer. Stamp the time now, handle it on the copier thread.
        job.done_at = time.perf_counter()
        self._submit(self._finish, job)

    # ---- answers ----

    def _finish(self, job: _Job) -> None:
        row = job.row
        if self._inflight.get(_key(row)) is job:
            del self._inflight[_key(row)]
        self._apply(job, _outcome(job.future))
        self._dirty[id(row)] = row
        if job.timed_out:
            log.warning("late %s reply from %s for master ticket %s", job.kind, row["client_id"], row["master_ticket"],
                        extra={"account": row["client_id"]})
        if self._copies.get(row["master_ticket"], {}).get(row["client_id"]) is row:
            # The master may have closed or moved its SL/TP while this command was on its way.
            self._follow_master(row, time.perf_counter(), retrying=False)
        self._flush()
        self._forget_finished()

    def _apply(self, job: _Job, result: dict) -> None:
        row = job.row
        key = _key(row)
        took = round((job.done_at - job.detected_at) * 1000, 2) if job.done_at else None
        status, reason = result["status"], result.get("reason")

        if job.kind == "open":
            order = job.fields["order"]
            if status == "rejected" and (reason or "").startswith(INVALID_STOPS) and (order["sl"] or order["tp"]):
                # This broker refuses the master's SL/TP (too close to its price). Open anyway: missing
                # the trade is worse. The SL/TP is tried again afterwards and the reason is shown.
                row["_stops_refused"] = reason
                self._send("open", row, job.detected_at, order={**order, "sl": None, "tp": None})
                return
            if status == "filled":
                refused = row.pop("_stops_refused", None)
                row.update(status="open", reason=f"OPENED_WITHOUT_SLTP: {refused}" if refused else None,
                           client_ticket=result["ticket"], open_price=result.get("price"), volume=result.get("volume"),
                           open_ms=took, broker_open_ms=result.get("elapsedMs"),
                           sl=order["sl"] or 0.0, tp=order["tp"] or 0.0)
                if refused:
                    self._failed_stops[key] = (self._master_stops(row) or (0.0, 0.0))
            else:
                row.update(status=status if status in ("skipped", "unknown") else "rejected", reason=reason)
                if status == "unknown":
                    self._schedule_retry(row)
                elif status != "skipped":
                    log.warning("copy of master ticket %s (%s) on %s was rejected: %s", row["master_ticket"],
                                row["symbol"], row["client_id"], reason, extra={"account": row["client_id"]})
        elif job.kind == "find":
            self._apply_find(row, result)
        elif job.kind == "close":
            if status == "closed":
                already = (result.get("detail") or {}).get("alreadyClosed")
                row.update(status="closed", reason="ALREADY_CLOSED_ON_CLIENT" if already else None,
                           close_price=result.get("price"), close_ms=took, broker_close_ms=result.get("elapsedMs"))
                self._retry_at.pop(key, None)
                self._attempts.pop(key, None)
            else:
                row["reason"] = f"CLOSE_FAILED: {reason}"
                self._schedule_retry(row)
                log.warning("closing the copy of master ticket %s on %s failed, retrying: %s", row["master_ticket"],
                            row["client_id"], reason, extra={"account": row["client_id"]})
        elif row["status"] == "open":  # modify; ignored if the copy was closed meanwhile
            stops = (job.fields["sl"], job.fields["tp"])
            if status == "modified":
                row.update(sl=stops[0], tp=stops[1], reason=None)
                self._failed_stops.pop(key, None)
            else:
                row["reason"] = f"SLTP_FAILED: {reason}"
                log.warning("SL/TP change of master ticket %s on %s failed: %s", row["master_ticket"], row["client_id"],
                            reason, extra={"account": row["client_id"]})
                if status == "rejected":
                    self._failed_stops[key] = stops  # the broker said no: wait until the master's SL/TP change
                else:
                    self._schedule_retry(row)

    def _apply_find(self, row: dict, result: dict) -> None:
        """Answer to a lookup of a copy whose open result was lost."""
        if result.get("status") != "ok":
            self._schedule_retry(row)
            return
        comment = f"copy {row['master_ticket']}"
        found = next((p for p in result["positions"] if p["magic"] == COPY_MAGIC and p["comment"] == comment), None)
        if found is None:
            row.update(status="rejected", reason="NOT_FOUND_ON_CLIENT")
            return
        row.update(status="open", reason="FOUND_AFTER_LOST_ANSWER", client_ticket=found["ticket"],
                   open_price=found["price"], volume=found["volume"], sl=found["sl"] or 0.0, tp=found["tp"] or 0.0)

    # ---- retries ----

    def _retry_loop(self) -> None:
        while self._running:
            time.sleep(self.retry_interval)
            self._submit(self._retry_and_flush)

    def _retry_and_flush(self) -> None:
        self._retry()
        self._flush()

    def _retry(self) -> None:
        """Marks overdue answers, finds lost opens, closes copies whose master is gone and re-syncs SL/TP."""
        now, at = time.perf_counter(), time.perf_counter()
        for job in list(self._inflight.values()):
            if not job.timed_out and now - job.sent_at > self.timeout:
                # Never resend a command whose outcome is unknown. Show it and wait for the late answer.
                job.timed_out = True
                if job.kind == "open":
                    job.row.update(status="unknown", reason="TIMEOUT")
                else:
                    job.row["reason"] = f"{job.kind.upper()}_TIMEOUT"
                self._dirty[id(job.row)] = job.row
        if self._master_positions is None:
            return
        for rows in list(self._copies.values()):
            for row in list(rows.values()):
                if row["status"] == "unknown" and row["reason"] in LOST_ANSWER and _key(row) not in self._inflight:
                    if time.monotonic() >= self._retry_at.get(_key(row), 0) and \
                            not self._skip_reason(row["client_id"], opening=False):
                        self._send("find", row, at, magic=COPY_MAGIC)
                else:
                    self._follow_master(row, at, retrying=True)

    def _schedule_retry(self, row: dict) -> None:
        key = _key(row)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        delay = min(self.retry_interval * 2 ** (self._attempts[key] - 1), MAX_RETRY_DELAY)
        self._retry_at[key] = time.monotonic() + delay

    def _forget_finished(self) -> None:
        """Drops master tickets that are closed and have no client copy left to look after."""
        for master_ticket in list(self._copies):
            if master_ticket in (self._master_positions or {}):
                continue
            if all(row["status"] not in LIVE for row in self._copies[master_ticket].values()):
                del self._copies[master_ticket]

    # ---- helpers ----

    def _flush(self) -> None:
        """Saves changed rows. Called after the commands are sent, so the disk never delays an order."""
        rows, self._dirty = list(self._dirty.values()), {}
        for row in rows:
            self.db.save_copy(row)

    def _load_copies(self) -> None:
        if not self.master_id:
            return
        for row in self.db.live_copies(self.master_id):
            if row["status"] == "pending":  # StakBridge stopped while this order was on its way
                row.update(status="unknown", reason="RESTARTED_WHILE_SENDING")
                self.db.save_copy(row)
            elif row["status"] == "unknown" and row["reason"] in ("TIMEOUT", "WORKER_STOPPED"):
                row["reason"] = "RESTARTED_WHILE_SENDING"  # its late answer can no longer arrive
                self.db.save_copy(row)
            self._copies.setdefault(row["master_ticket"], {})[row["client_id"]] = row

    def _master_stops(self, row: dict) -> tuple | None:
        master = (self._master_positions or {}).get(row["master_ticket"])
        return None if master is None else (master["sl"], master["tp"])

    def _new_row(self, position: dict, client: dict) -> dict:
        return {
            "master_id": self.master_id, "master_ticket": position["ticket"], "client_id": client["accountId"],
            "symbol": self._client_symbol(position["symbol"], client), "side": position["side"],
            "master_volume": position["volume"], "volume": round(position["volume"] * client["multiplier"], 8),
            "sl": 0.0, "tp": 0.0, "status": "pending", "reason": None, "client_ticket": None, "created_at": now_iso(),
        }

    def _client_symbol(self, symbol: str, client: dict) -> str:
        """The client's name for a master symbol: its own mapping first, otherwise the suffix rule."""
        mapped = (client.get("symbols") or {}).get(symbol)
        if mapped:
            return mapped
        client_id = client["accountId"]
        master_suffix = (self._accounts.get(self.master_id) or {}).get("symbol_suffix", "")
        if master_suffix and symbol.endswith(master_suffix):
            symbol = symbol[: -len(master_suffix)]
        return symbol + (self._accounts.get(client_id) or {}).get("symbol_suffix", "")

    def _skip_reason(self, client_id: str, opening: bool) -> str | None:
        account = self._accounts.get(client_id)
        if account is None:
            return "ACCOUNT_REMOVED"
        if not account["enabled"] or self.workers.status(client_id)["status"] != "connected":
            return "DISCONNECTED"
        if not account["trading_on"]:
            return "TRADING_OFF"
        if opening and self._kill:
            return "KILL_SWITCH_ON"
        if opening and not self._config.get("active"):
            return "COPIER_PAUSED"
        return None


def _outcome(future: Future) -> dict:
    try:
        return future.result()
    except WorkerGone:
        return {"status": "unknown", "reason": "WORKER_STOPPED"}
