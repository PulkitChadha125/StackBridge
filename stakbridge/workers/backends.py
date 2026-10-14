"""Broker backends used inside a worker process.

Mt5Backend drives one real MetaTrader 5 terminal. FakeBackend simulates a hedging demo
account in memory so the whole app can be run and tested without MT5.

Every method returns plain dicts so results can travel over a multiprocessing pipe:
    connect()/check() -> {"status": "connected"|"disconnected"|"error", "reason": str|None, "info": {...}}
    open(order)       -> {"status": "filled"|"placed"|"rejected", "reason", "ticket", "price", "volume"}
    close(magic, sym) -> {"status": "closed"|"rejected", "reason", "detail": {...}}
    positions(magic)  -> {"status": "ok", "positions": [...], "orders": [...]}
"""

import itertools
import math

MAX_COMMENT = 31  # MT5 limit


def normalize_volume(volume: float, vmin: float, vmax: float, step: float) -> tuple[float | None, str | None]:
    """Rounds the volume down to the broker's lot step and checks the broker's min/max."""
    if step > 0:
        volume = math.floor(volume / step + 1e-9) * step
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        volume = round(volume, decimals)
    if volume < vmin - 1e-9:
        return None, "VOLUME_BELOW_BROKER_MIN"
    if volume > vmax + 1e-9:
        return None, "VOLUME_ABOVE_BROKER_MAX"
    return volume, None


def make_backend(config: dict):
    if config["backend"] == "fake":
        return FakeBackend(config)
    return Mt5Backend(config)


class Mt5Backend:
    def __init__(self, config: dict):
        import MetaTrader5 as mt5  # imported here so the fake backend works without MT5

        self.mt5 = mt5
        self.config = config
        self.initialized = False

    # ---- connection ----

    def connect(self) -> dict:
        mt5 = self.mt5
        kwargs = {
            "login": int(self.config["login"]),
            "password": self.config["password"],
            "server": self.config["server"],
            "timeout": 60_000,
        }
        if self.config.get("terminal_path"):
            kwargs["path"] = self.config["terminal_path"]
        if not mt5.initialize(**kwargs):
            code, message = mt5.last_error()
            mt5.shutdown()
            return {"status": "error", "reason": f"LOGIN_FAILED: {code} {message}", "info": {}}
        self.initialized = True

        account = mt5.account_info()
        if account is None or account.login != int(self.config["login"]):
            return self._fail("LOGIN_FAILED: terminal is logged into another account")
        if account.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            return self._fail("NETTING_NOT_SUPPORTED: only hedging accounts can be used")
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not self.config["allow_live"]:
            return self._fail("LIVE_ACCOUNT_BLOCKED: set STAKBRIDGE_ALLOW_LIVE=1 to allow live accounts")
        return self.check()

    def _fail(self, reason: str) -> dict:
        self.mt5.shutdown()
        self.initialized = False
        return {"status": "error", "reason": reason, "info": {}}

    def check(self) -> dict:
        if not self.initialized:
            return {"status": "disconnected", "reason": "NOT_INITIALIZED", "info": {}}
        terminal = self.mt5.terminal_info()
        account = self.mt5.account_info()
        if terminal is None or account is None:
            self.mt5.shutdown()
            self.initialized = False
            return {"status": "disconnected", "reason": "TERMINAL_LOST", "info": {}}
        if not terminal.connected:
            return {"status": "disconnected", "reason": "NO_BROKER_CONNECTION", "info": {}}
        info = {
            "balance": account.balance,
            "equity": account.equity,
            "currency": account.currency,
            "algoTrading": bool(terminal.trade_allowed),
        }
        reason = None if terminal.trade_allowed else "ALGO_TRADING_OFF_IN_TERMINAL"
        return {"status": "connected", "reason": reason, "info": info}

    def shutdown(self) -> None:
        if self.initialized:
            self.mt5.shutdown()
            self.initialized = False

    # ---- trading ----

    def _filling(self, symbol_info) -> int:
        mt5 = self.mt5
        if symbol_info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if symbol_info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def open(self, order: dict) -> dict:
        mt5 = self.mt5
        symbol = order["symbol"]
        if not mt5.symbol_select(symbol, True):
            return {"status": "rejected", "reason": f"UNKNOWN_SYMBOL: {symbol}"}
        info = mt5.symbol_info(symbol)
        if info is None:
            return {"status": "rejected", "reason": f"UNKNOWN_SYMBOL: {symbol}"}
        volume, error = normalize_volume(order["volume"], info.volume_min, info.volume_max, info.volume_step)
        if error:
            return {"status": "rejected", "reason": error}

        buy = order["side"] == "buy"
        request = {
            "symbol": symbol,
            "volume": volume,
            "magic": order["magic"],
            "comment": (order.get("comment") or "")[:MAX_COMMENT],
            "sl": round(order["sl"], info.digits) if order.get("sl") else 0.0,
            "tp": round(order["tp"], info.digits) if order.get("tp") else 0.0,
        }
        if order["type"] == "market":
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return {"status": "rejected", "reason": "NO_PRICE"}
            request.update(
                action=mt5.TRADE_ACTION_DEAL,
                type=mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                price=tick.ask if buy else tick.bid,
                deviation=self.config["deviation_points"],
                type_filling=self._filling(info),
                type_time=mt5.ORDER_TIME_GTC,
            )
        else:
            pending_types = {
                ("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
                ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
                ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP,
                ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP,
            }
            request.update(
                action=mt5.TRADE_ACTION_PENDING,
                type=pending_types[(order["side"], order["type"])],
                price=round(order["price"], info.digits),
                type_filling=mt5.ORDER_FILLING_RETURN,
                type_time=mt5.ORDER_TIME_GTC,
            )

        result = mt5.order_send(request)
        if result is None:
            code, message = mt5.last_error()
            return {"status": "rejected", "reason": f"ORDER_SEND_FAILED: {code} {message}"}
        if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
            return {"status": "filled", "ticket": result.order, "price": result.price, "volume": result.volume}
        if result.retcode == mt5.TRADE_RETCODE_PLACED:
            return {"status": "placed", "ticket": result.order, "price": request["price"], "volume": volume}
        return {"status": "rejected", "reason": f"BROKER_{result.retcode}: {result.comment}"}

    def close(self, magic: int, symbol: str | None) -> dict:
        mt5 = self.mt5
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        closed, cancelled, failures = 0, 0, []

        for position in [p for p in positions or () if p.magic == magic]:
            tick = mt5.symbol_info_tick(position.symbol)
            info = mt5.symbol_info(position.symbol)
            is_buy = position.type == mt5.POSITION_TYPE_BUY
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "position": position.ticket,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if is_buy else tick.ask,
                "deviation": self.config["deviation_points"],
                "magic": magic,
                "type_filling": self._filling(info),
                "type_time": mt5.ORDER_TIME_GTC,
            })
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
            else:
                failures.append({"ticket": position.ticket, "reason": _retcode_text(mt5, result)})

        for pending in [o for o in orders or () if o.magic == magic]:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": pending.ticket})
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                cancelled += 1
            else:
                failures.append({"ticket": pending.ticket, "reason": _retcode_text(mt5, result)})

        return _close_result(closed, cancelled, failures)

    def positions(self, magic: int | None) -> dict:
        mt5 = self.mt5
        positions = [p for p in mt5.positions_get() or () if magic is None or p.magic == magic]
        orders = [o for o in mt5.orders_get() or () if magic is None or o.magic == magic]
        return {
            "status": "ok",
            "positions": [
                {"ticket": p.ticket, "symbol": p.symbol, "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                 "volume": p.volume, "price": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit,
                 "magic": p.magic, "comment": p.comment}
                for p in positions
            ],
            "orders": [
                {"ticket": o.ticket, "symbol": o.symbol, "volume": o.volume_current, "price": o.price_open,
                 "magic": o.magic, "comment": o.comment}
                for o in orders
            ],
        }


def _retcode_text(mt5, result) -> str:
    if result is None:
        code, message = mt5.last_error()
        return f"ORDER_SEND_FAILED: {code} {message}"
    return f"BROKER_{result.retcode}: {result.comment}"


def _close_result(closed: int, cancelled: int, failures: list) -> dict:
    detail = {"closedPositions": closed, "cancelledOrders": cancelled}
    if failures:
        detail["failures"] = failures
        return {"status": "rejected", "reason": failures[0]["reason"], "detail": detail}
    return {"status": "closed", "detail": detail}


class FakeBackend:
    """In-memory hedging demo account. Prices are fixed so results are predictable."""

    PRICES = {"XAUUSD": 2321.45, "EURUSD": 1.0850, "GBPUSD": 1.2700, "US30": 39000.0}
    VOLUME_MIN, VOLUME_MAX, VOLUME_STEP = 0.01, 100.0, 0.01

    def __init__(self, config: dict):
        self.config = config
        self.connected = False
        self.simulate_offline = False
        self.positions_by_ticket: dict[int, dict] = {}
        self.orders_by_ticket: dict[int, dict] = {}
        self._tickets = itertools.count(100000 + int(config["login"]) % 1000 * 1000)

    def connect(self) -> dict:
        self.connected = not self.simulate_offline
        return self.check()

    def check(self) -> dict:
        if self.simulate_offline or not self.connected:
            return {"status": "disconnected", "reason": "SIMULATED_OFFLINE", "info": {}}
        return {"status": "connected", "reason": None,
                "info": {"balance": 10000.0, "equity": 10000.0, "currency": "USD", "algoTrading": True}}

    def shutdown(self) -> None:
        self.connected = False

    def set_offline(self, offline: bool) -> None:
        self.simulate_offline = offline
        self.connected = not offline

    def _price(self, symbol: str) -> float:
        for base, price in self.PRICES.items():
            if symbol.startswith(base):  # ignores broker suffixes such as ".m"
                return price
        return 100.0

    def open(self, order: dict) -> dict:
        volume, error = normalize_volume(order["volume"], self.VOLUME_MIN, self.VOLUME_MAX, self.VOLUME_STEP)
        if error:
            return {"status": "rejected", "reason": error}
        ticket = next(self._tickets)
        record = {
            "ticket": ticket, "symbol": order["symbol"], "side": order["side"], "volume": volume,
            "sl": order.get("sl") or 0.0, "tp": order.get("tp") or 0.0, "profit": 0.0,
            "magic": order["magic"], "comment": (order.get("comment") or "")[:MAX_COMMENT],
        }
        if order["type"] == "market":
            record["price"] = self._price(order["symbol"])
            self.positions_by_ticket[ticket] = record
            return {"status": "filled", "ticket": ticket, "price": record["price"], "volume": volume}
        record["price"] = order["price"]
        self.orders_by_ticket[ticket] = record
        return {"status": "placed", "ticket": ticket, "price": record["price"], "volume": volume}

    def close(self, magic: int, symbol: str | None) -> dict:
        def matches(record):
            return record["magic"] == magic and (symbol is None or record["symbol"] == symbol)

        closing = [t for t, p in self.positions_by_ticket.items() if matches(p)]
        cancelling = [t for t, o in self.orders_by_ticket.items() if matches(o)]
        for ticket in closing:
            del self.positions_by_ticket[ticket]
        for ticket in cancelling:
            del self.orders_by_ticket[ticket]
        return _close_result(len(closing), len(cancelling), [])

    def positions(self, magic: int | None) -> dict:
        return {
            "status": "ok",
            "positions": [p for p in self.positions_by_ticket.values() if magic is None or p["magic"] == magic],
            "orders": [o for o in self.orders_by_ticket.values() if magic is None or o["magic"] == magic],
        }
