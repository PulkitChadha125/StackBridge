"""Strategy-tag routing: decides which accounts get a trade and fans it out to them in parallel."""

import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, wait
from functools import partial

from .db import Database, DuplicateRequest
from .models import TradeIn
from .workers import WorkerGone, WorkerManager

log = logging.getLogger("stakbridge.trading")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RateLimiter:
    """Sliding one-minute window per API token."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True


class TradeService:
    def __init__(self, db: Database, workers: WorkerManager, timeout: float):
        self.db = db
        self.workers = workers
        self.timeout = timeout

    def execute(self, trade: TradeIn) -> dict:
        payload = trade.payload()
        existing = self.db.get_trade(trade.request_id)
        if existing is not None:
            return self._replay(existing, payload)

        strategy = self.db.get_strategy(trade.strategy)
        if strategy is None:
            raise ApiError(400, "UNKNOWN_STRATEGY", f"strategy tag '{trade.strategy}' does not exist")
        # The kill switch and a paused strategy block new risk only. Closing is always
        # allowed so positions can still be flattened during an emergency.
        if trade.action == "open":
            if self.db.get_kill_switch():
                raise ApiError(409, "KILL_SWITCH_ON", "all trading is stopped: press Resume trading in the UI or call DELETE /v1/kill")
            if not strategy["active"]:
                raise ApiError(409, "STRATEGY_PAUSED", f"strategy '{trade.strategy}' is paused")
            if trade.volume > strategy["max_volume"] + 1e-9:
                raise ApiError(400, "VOLUME_TOO_LARGE",
                               f"volume {trade.volume} is above the strategy max of {strategy['max_volume']}")
        if not strategy["account_ids"]:
            raise ApiError(409, "NO_ELIGIBLE_ACCOUNT", f"no account is assigned to '{trade.strategy}'")

        account_ids = strategy["account_ids"]
        try:
            self.db.create_trade_request(trade.request_id, trade.strategy, trade.action, payload, account_ids)
        except DuplicateRequest:  # an identical request won the race a moment ago
            return self._replay(self.db.get_trade(trade.request_id), payload)

        self._dispatch(trade, strategy, account_ids)
        stored = self.db.get_trade(trade.request_id)
        return {k: stored[k] for k in ("requestId", "strategy", "action", "results")}

    def _replay(self, existing: dict, payload: dict) -> dict:
        if existing["payload"] != payload:
            raise ApiError(409, "REQUEST_ID_REUSED",
                           f"requestId '{existing['requestId']}' was already used for a different request")
        body = {k: existing[k] for k in ("requestId", "strategy", "action", "results")}
        body["replayed"] = True
        return body

    def _dispatch(self, trade: TradeIn, strategy: dict, account_ids: list[str]) -> None:
        accounts = {a["id"]: a for a in self.db.list_accounts()}
        futures: dict[str, Future] = {}

        for account_id in account_ids:
            account = accounts.get(account_id)
            skip = self._skip_reason(account)
            if skip:
                self.db.save_result(trade.request_id, account_id, {"status": "skipped", "reason": skip})
                continue
            futures[account_id] = self._send(trade, strategy, account)

        # All commands are already on their way; now wait for the answers together.
        wait(futures.values(), timeout=self.timeout)

        for account_id, future in futures.items():
            if future.done():
                result = _outcome(future)
                if result["status"] in ("rejected", "unknown"):
                    log.warning("trade %s on %s %s: %s", trade.request_id, account_id, result["status"],
                                result.get("reason"), extra={"account": account_id})
                self.db.save_result(trade.request_id, account_id, result)
            else:
                # Never resend. Record UNKNOWN now and let the real answer overwrite it when it arrives.
                log.warning("trade %s on %s: no answer within %s s, shown as unknown", trade.request_id, account_id,
                            self.timeout, extra={"account": account_id})
                self.db.save_result(trade.request_id, account_id, {"status": "unknown", "reason": "TIMEOUT"})
                future.add_done_callback(partial(self._late_result, trade.request_id, account_id))

    def _skip_reason(self, account: dict | None) -> str | None:
        if account is None:
            return "ACCOUNT_REMOVED"
        if not account["enabled"] or self.workers.status(account["id"])["status"] != "connected":
            return "DISCONNECTED"
        if not account["trading_on"]:
            return "TRADING_OFF"
        return None

    def _send(self, trade: TradeIn, strategy: dict, account: dict) -> Future:
        handle = self.workers.get(account["id"])
        suffix = account["symbol_suffix"]
        if handle is None:
            future: Future = Future()
            future.set_result({"status": "skipped", "reason": "DISCONNECTED"})
            return future
        if trade.action == "close":
            symbol = trade.symbol + suffix if trade.symbol else None
            return handle.send("close", magic=strategy["magic"], symbol=symbol)
        return handle.send("open", order={
            "symbol": trade.symbol + suffix,
            "side": trade.side,
            "type": trade.type,
            "volume": trade.volume,
            "price": trade.price,
            "sl": trade.sl,
            "tp": trade.tp,
            "comment": trade.comment or trade.request_id,
            "magic": strategy["magic"],
        })

    def _late_result(self, request_id: str, account_id: str, future: Future) -> None:
        result = _outcome(future)
        result.setdefault("detail", {})["lateReply"] = True
        log.warning("late reply for trade %s on %s: %s", request_id, account_id, result["status"],
                    extra={"account": account_id})
        self.db.save_result(request_id, account_id, result)


def _outcome(future: Future) -> dict:
    try:
        return future.result()
    except WorkerGone:
        # The worker died while holding the order: it may or may not have reached the broker.
        return {"status": "unknown", "reason": "WORKER_STOPPED"}
