"""Trade copier: mirrors the master account's positions onto client accounts.

The master's worker checks its open positions every few milliseconds and reports what
changed (see workers/process.py). For every change the copier sends the matching command
to all client workers at once:

Every account except the master is a client. Each copies at x1 unless the Copier tab sets
another lot multiplier or symbol names for it.

    master opens a position    -> each client opens master volume x its multiplier, same SL/TP
    master changes SL or TP    -> each client copy gets the same SL and TP
    master closes a position   -> each client copy is closed

Waiting limit and stop orders are copied too (stop-limit orders are not: their position is):

    master places an order     -> each client places the same order (price, SL/TP, lots x multiplier)
    master moves it            -> each client order gets the same price, SL and TP
    master deletes it/expires  -> each client order is deleted
    master order fills         -> each client gets the position: its own order if that filled already,
                                  otherwise its order is deleted and the trade opened at market

The copier never waits for a broker. Commands go out immediately and each answer is handled
when it arrives, so a slow or hung client never holds up the others or the next master event.

Every client copy is tracked by its ticket in the `copies` table, so the copier only ever
touches positions it opened itself. Manual trades on a client are never touched.

Opens are blocked by the kill switch, by an invalid licence, by the master's Trading switch (for
all clients) and by a client's own Trading switch (for that client only). Closes and SL/TP changes still go through
when the master's switch or the kill switch is off, so clients can always follow the master out. A close or SL/TP change that fails (client
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
COMMANDS = {"open": "open", "place": "open", "close": "close_position", "modify": "modify_position",
            "find": "positions", "amend": "modify_order", "cancel": "cancel_order", "fill": "fill_order"}
LIVE = ("pending", "placed", "open", "unknown")
OPENING = ("open", "place")
# Open orders whose answer was lost for good. The copy is looked up on the client by its comment.
LOST_ANSWER = ("WORKER_STOPPED", "RESTARTED_WHILE_SENDING")


@dataclass
class _Job:
    kind: str  # "open" | "close" | "modify" | "find", and for waiting orders "place" | "amend" | "cancel" | "fill"
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
    def __init__(self, db: Database, workers: WorkerManager, settings, license_ok=lambda: True):
        self.db = db
        self.license_ok = license_ok  # False while the licence is not valid: no new trades are copied
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
        self._order_log: list[tuple] = []  # master order events for the Orders tab, saved once the commands are out
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
                for state in (self._copies, self._inflight, self._retry_at, self._attempts, self._failed_stops,
                              self._order_log):
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
        self._save_order_log()
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
        # A waiting order is always copied: its price has not been taken yet.
        fresh = [p for p in missed if p.get("pending") or (p.get("ageS") is not None and p["ageS"] <= self.max_age)]
        self._open(fresh, message["at"])
        for ticket in self.db.open_master_tickets(self.master_id):
            if ticket not in positions:
                self._order_log.append(("close", ticket, "Closed on the master while StakBridge was not watching", {}))
        for position in missed:
            if position in fresh and position.get("pending"):
                self._order_log.append(("open", position, f"{_order_name(position)} placed on the master while "
                                        "StakBridge was not watching, copied", {}))
            elif position in fresh:
                self._order_log.append(("open", position, "Opened on the master while StakBridge was not watching "
                                        f"({position['ageS']:.0f} s before), copied", {}))
            else:
                self._order_log.append(("open", position, "Opened on the master while StakBridge was not watching, "
                                        f"too old to copy (over {self.max_age:.0f} s)", {}))
                log.warning("master position %s opened while not watching, too old to copy", position["ticket"])
                for client in self._clients():
                    row = self._new_row(position, client)
                    row.update(status="skipped", reason="TOO_OLD_AFTER_RESTART")
                    self._copies.setdefault(position["ticket"], {})[client["accountId"]] = row
                    self._dirty[id(row)] = row
        self._retry()  # closes and SL/TP changes the master made while not watched

    def _diff(self, message: dict) -> None:
        at = message["at"]
        before = {p["ticket"]: self._master_positions.get(p["ticket"]) for p in message["modified"]}
        for position in message["opened"] + message["modified"]:
            self._master_positions[position["ticket"]] = position
        for position in message["closed"]:
            self._master_positions.pop(position["ticket"], None)
        self._open(message["opened"], at)
        for position in message["opened"]:
            text = f"{_order_name(position)} placed on the master at {position['price']}" if position.get("pending") \
                else "Opened on the master"
            self._order_log.append(("open", position, text, {}))
        for position in message["modified"]:
            ticket, old = position["ticket"], before.get(position["ticket"]) or {}
            stops = {"sl": position["sl"], "tp": position["tp"]}
            if old.get("pending") and not position.get("pending"):
                self._order_log.append(("fill", ticket, f"Filled on the master at {position['price']}",
                                        {**stops, "price": position["price"]}))
            elif position.get("pending"):
                self._order_log.append(("modify", ticket, f"Order changed: price {position['price']}, SL/TP "
                                        f"{position['sl'] or 'none'} / {position['tp'] or 'none'}",
                                        {**stops, "price": position["price"]}))
            else:
                self._order_log.append(("modify", ticket, f"SL/TP changed to {position['sl'] or 'none'} / "
                                        f"{position['tp'] or 'none'}", stops))
        for position in message["closed"]:
            if position.get("pending"):
                self._order_log.append(("cancel", position["ticket"], "Order deleted on the master (or it expired)", {}))
            else:
                self._order_log.append(("close", position["ticket"], "Closed on the master", {}))
        for position in message["closed"] + message["modified"]:
            for row in self._copies.get(position["ticket"], {}).values():
                self._follow_master(row, at, retrying=False)

    # ---- sending ----

    def _open(self, positions: list[dict], at: float) -> None:
        for position in positions:
            for client in self._clients():
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
                order = self._market_order(row, position)
                if position.get("pending"):
                    self._send("place", row, at, order={**order, "type": position["type"], "price": position["price"]})
                else:
                    self._send("open", row, at, order=order)

    @staticmethod
    def _market_order(row: dict, master: dict) -> dict:
        return {"symbol": row["symbol"], "side": row["side"], "type": "market", "volume": row["volume"],
                "sl": master["sl"] or None, "tp": master["tp"] or None,
                "comment": f"copy {row['master_ticket']}", "magic": COPY_MAGIC}

    def _follow_master(self, row: dict, at: float, retrying: bool) -> None:
        """Brings one open copy in line with its master position: closes it, or fixes its SL/TP."""
        if row["status"] not in ("open", "placed") or self._master_positions is None:
            return
        key = _key(row)
        master = self._master_positions.get(row["master_ticket"])
        busy = self._inflight.get(key)
        if retrying and time.monotonic() < self._retry_at.get(key, 0):
            return
        skip = self._skip_reason(row["client_id"], opening=False)
        if row["status"] == "placed":
            self._follow_master_order(row, master, busy, skip, at)
            return

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

    def _follow_master_order(self, row: dict, master: dict | None, busy, skip: str | None, at: float) -> None:
        """Brings one waiting client order in line with the master's: moves, deletes or fills it."""
        if busy is not None:
            return  # one command at a time; its answer calls this again
        if skip:
            waiting = "CANCEL_WAITING" if master is None else None if master.get("pending") else "FILL_WAITING"
            if waiting and row["reason"] != f"{waiting}: {skip}":
                row["reason"] = f"{waiting}: {skip}"  # done as soon as the account is back
                self._dirty[id(row)] = row
            return
        if master is None:
            self._send("cancel", row, at, ticket=row["client_ticket"])
        elif not master.get("pending"):
            blocked = self._skip_reason(row["client_id"], opening=True)
            if blocked:  # Stop all or the master's Trading switch: no new trade, so the order is withdrawn
                row["_cancel_reason"] = blocked
                self._send("cancel", row, at, ticket=row["client_ticket"])
            else:
                self._send("fill", row, at, ticket=row["client_ticket"], order=self._market_order(row, master))
        elif (row["open_price"], row["sl"], row["tp"]) != _order_target(master) \
                and self._failed_stops.get(_key(row)) != _order_target(master):
            self._send("amend", row, at, ticket=row["client_ticket"], price=master["price"], sl=master["sl"],
                       tp=master["tp"])

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

        if job.kind in OPENING:
            order = job.fields["order"]
            if status == "rejected" and (reason or "").startswith(INVALID_STOPS) and (order["sl"] or order["tp"]):
                # This broker refuses the master's SL/TP (too close to its price). Open anyway: missing
                # the trade is worse. The SL/TP is tried again afterwards and the reason is shown.
                row["_stops_refused"] = reason
                self._send(job.kind, row, job.detected_at, order={**order, "sl": None, "tp": None})
                return
            if status == "placed":
                refused = row.pop("_stops_refused", None)
                row.update(status="placed", reason=f"PLACED_WITHOUT_SLTP: {refused}" if refused else None,
                           client_ticket=result["ticket"], open_price=result.get("price"), volume=result.get("volume"),
                           open_ms=took, broker_open_ms=result.get("elapsedMs"),
                           sl=order["sl"] or 0.0, tp=order["tp"] or 0.0)
                if refused:
                    master = (self._master_positions or {}).get(row["master_ticket"])
                    if master is not None and master.get("pending"):
                        self._failed_stops[key] = _order_target(master)
            elif status == "filled":
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
        elif job.kind in ("amend", "cancel", "fill"):
            self._apply_order(job, result, took)
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

    def _apply_order(self, job: _Job, result: dict, took: float | None) -> None:
        """Answer to a command for a waiting client order."""
        row, key = job.row, _key(job.row)
        status, reason = result["status"], result.get("reason")
        if row["status"] != "placed":
            return
        if status == "filled":  # the client's order is a position now (filled by itself, or opened at market)
            refused = result.get("stopsRefused")
            row.update(status="open", client_ticket=result["ticket"], open_price=result.get("price"),
                       volume=result.get("volume"), sl=result.get("sl") or 0.0, tp=result.get("tp") or 0.0,
                       reason=f"OPENED_WITHOUT_SLTP: {refused}" if refused else None)
            row.pop("_cancel_reason", None)
            if job.kind == "fill":
                row.update(open_ms=took, broker_open_ms=result.get("elapsedMs"))
            if refused:
                self._failed_stops[key] = (self._master_stops(row) or (0.0, 0.0))
            else:
                self._failed_stops.pop(key, None)
            self._retry_at.pop(key, None)
            self._attempts.pop(key, None)
        elif job.kind == "cancel" and status == "cancelled":
            already = (result.get("detail") or {}).get("alreadyGone")
            row.update(status="cancelled",
                       reason=row.pop("_cancel_reason", None) or ("ALREADY_GONE_ON_CLIENT" if already else None),
                       close_ms=took, broker_close_ms=result.get("elapsedMs"))
        elif job.kind == "amend" and status == "modified":
            row.update(open_price=job.fields["price"], sl=job.fields["sl"], tp=job.fields["tp"], reason=None)
            self._failed_stops.pop(key, None)
        elif job.kind == "fill" and result.get("orderRemoved"):
            # The client's order was deleted but the market order was refused: like a failed open.
            row.update(status="rejected", reason=reason)
            log.warning("filling the copy of master order %s on %s was rejected: %s", row["master_ticket"],
                        row["client_id"], reason, extra={"account": row["client_id"]})
        else:
            label = {"amend": "ORDER_CHANGE_FAILED", "cancel": "CANCEL_FAILED", "fill": "FILL_FAILED"}[job.kind]
            row["reason"] = f"{label}: {reason}"
            row.pop("_cancel_reason", None)
            log.warning("%s of the copied order %s on %s failed: %s", job.kind, row["master_ticket"], row["client_id"],
                        reason, extra={"account": row["client_id"]})
            if job.kind == "amend" and status == "rejected":
                master = (self._master_positions or {}).get(row["master_ticket"])
                if master is not None:
                    self._failed_stops[key] = _order_target(master)  # wait until the master's order changes
            else:
                self._schedule_retry(row)

    def _apply_find(self, row: dict, result: dict) -> None:
        """Answer to a lookup of a copy whose open result was lost."""
        if result.get("status") != "ok":
            self._schedule_retry(row)
            return
        comment = f"copy {row['master_ticket']}"
        mine = lambda item: item["magic"] == COPY_MAGIC and item["comment"] == comment  # noqa: E731
        found = next((p for p in result["positions"] if mine(p)), None)
        waiting = next((o for o in result.get("orders", []) if mine(o)), None)
        if found is None and waiting is None:
            row.update(status="rejected", reason="NOT_FOUND_ON_CLIENT")
            return
        found = found or waiting
        row.update(status="open" if waiting is None or found is not waiting else "placed",
                   reason="FOUND_AFTER_LOST_ANSWER", client_ticket=found["ticket"], open_price=found["price"],
                   volume=found["volume"], sl=found.get("sl") or 0.0, tp=found.get("tp") or 0.0)

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
                if job.kind in OPENING:
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

    def _save_order_log(self) -> None:
        events, self._order_log = self._order_log, []
        for kind, position_or_ticket, text, values in events:
            if kind == "open":
                self.db.master_order_opened(self.master_id, position_or_ticket, text)
            else:
                self.db.master_order_changed(self.master_id, position_or_ticket, kind, text, **values)

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

    def _clients(self) -> list[dict]:
        """Every account except the master, with its lot multiplier and symbol names (x1 if not set)."""
        settings = {c["accountId"]: c for c in self._config.get("clients", [])}
        return [settings.get(account_id) or {"accountId": account_id, "multiplier": 1.0, "symbols": {}}
                for account_id in self._accounts if account_id != self.master_id]

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
        if opening and not self.license_ok():
            return "LICENSE_INACTIVE"
        if opening and not (self._accounts.get(self.master_id) or {}).get("trading_on"):
            return "MASTER_TRADING_OFF"
        return None


def _order_target(master: dict) -> tuple:
    """Price, SL and TP of a waiting master order, as a client order should have them."""
    return master["price"], master["sl"], master["tp"]


def _order_name(position: dict) -> str:
    """For example "Buy Limit"."""
    return f"{position['side'].capitalize()} {position.get('type', 'market').capitalize()}"


def _outcome(future: Future) -> dict:
    try:
        return future.result()
    except WorkerGone:
        return {"status": "unknown", "reason": "WORKER_STOPPED"}
