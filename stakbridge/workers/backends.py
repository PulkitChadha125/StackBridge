"""Broker backends used inside a worker process.

Mt5Backend drives one real MetaTrader 5 terminal. FakeBackend simulates a hedging demo
account in memory so the whole app can be run and tested without MT5.

Every method returns plain dicts so results can travel over a multiprocessing pipe:
    connect()/check() -> {"status": "connected"|"disconnected"|"error", "reason": str|None, "info": {...}}
                         info: balance, equity, profit (open P/L), currency, algoTrading, terminalPath
    open(order)       -> {"status": "filled"|"placed"|"rejected", "reason", "ticket", "price", "volume"}
    close(magic, sym) -> {"status": "closed"|"rejected", "reason", "detail": {...}}
    positions(magic)  -> {"status": "ok", "positions": [...], "orders": [...]}

Used by the live account check:
    account_info()            -> {"status": "ok", "balance", "equity", "freeMargin", "pingMs", ...} read live

Used by the copier:
    snapshot(with_age)          -> {ticket: {...}} of all open positions and waiting limit/stop orders
                                   ("pending": True), or None if they could not be read
    close_position(ticket)      -> {"status": "closed"|"rejected", "reason", "price", "volume", "detail"}
    modify_position(ticket, ..) -> {"status": "modified"|"rejected", "reason"}
    modify_order(ticket, ..)    -> {"status": "modified"|"filled"|"rejected", "reason"}   a waiting order
    cancel_order(ticket)        -> {"status": "cancelled"|"filled"|"rejected", "reason"}
    fill_order(ticket, order)   -> {"status": "filled"|"rejected", "reason", "ticket", "price", ...}
    "filled" from the last three means the waiting order has become a position (its details are given).
Order results carry "elapsedMs": how long the broker took to answer order_send.
"""

import itertools
import math
import os
import time

MAX_COMMENT = 31  # MT5 limit
POSITION_CLOSED = 10036  # TRADE_RETCODE_POSITION_CLOSED: "Position doesn't exist"


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


def symbol_core(name: str) -> str:
    """A symbol without its broker decoration: "XAUUSD.C", "XAUUSDm", "#XAUUSD" and "XAUUSD_i" all give
    "XAUUSD". Trailing small letters (Exness "m", "c") are dropped only after a real name such as XAUUSD."""
    core = name.strip().lstrip("#.-_!+")
    for i, char in enumerate(core):
        if not char.isalnum():
            core = core[:i]
            break
    head = core.rstrip("abcdefghijklmnopqrstuvwxyz")
    if len(head) >= 3 and head.upper() == head:
        core = head
    return core.upper()


def match_symbol(name: str, available: list[dict]) -> tuple[str | None, str | None]:
    """The broker's own name for a symbol from another broker, found by its core name
    (see symbol_core). available: [{"name", "visible", "tradable"}]. Returns (name, None) or (None, reason)."""
    core = symbol_core(name)
    found = [s for s in available if s["tradable"] and symbol_core(s["name"]) == core] if core else []
    if len(found) > 1:  # several account types on one server: the ones in Market Watch are the ones in use
        found = [s for s in found if s["visible"]] or found
    if len(found) == 1:
        return found[0]["name"], None
    if not found:
        return None, f"UNKNOWN_SYMBOL: {name}"
    names = ", ".join(sorted(s["name"] for s in found))
    return None, f"UNKNOWN_SYMBOL: {name} (this broker has {names}: add a symbol mapping in the Copier tab)"


TERMINAL_EXE = "terminal64.exe"

# MT5 Python errors from initialize(), in words a person can act on.
_CONNECT_ERRORS = {
    -6: ("LOGIN_FAILED", "wrong login, password or server"),
    -10003: ("TERMINAL_NOT_STARTED", "MetaTrader could not be started. Check the Terminal path and that this "
                                     "MetaTrader opens when you double-click it"),
    -10004: ("TERMINAL_NOT_REACHED", "MetaTrader is not answering. Close it and let StakBridge start it again"),
    -10005: ("TERMINAL_NOT_REACHED", "MetaTrader took too long to answer (still starting or busy). StakBridge retries"),
}


def normalize_terminal_path(path: str) -> str:
    """Cleans a Terminal path as people paste it: drops quotes and a full stop at the end, and adds
    terminal64.exe to a folder."""
    path = (path or "").strip().strip('"').strip().rstrip(".").strip().strip('"')
    if path and os.path.isdir(path):
        path = os.path.join(path, TERMINAL_EXE)
    return path


def terminal_path_problem(path: str) -> str | None:
    """Why a (normalized) Terminal path cannot work, or None if it looks fine. Empty means the default MT5."""
    if not path:
        return None
    if not os.path.isfile(path):
        return f"nothing found at '{path}'"
    if not path.lower().endswith(".exe"):
        return f"'{path}' is not a program. Use the full path to {TERMINAL_EXE}"
    return None


def connect_error(code: int, message: str) -> str:
    kind, explanation = _CONNECT_ERRORS.get(code, ("LOGIN_FAILED", None))
    return f"{kind}: {explanation} ({code} {message})" if explanation else f"{kind}: {code} {message}"


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
        self._symbol_names = {}  # name asked for -> this broker's name, once found

    # ---- connection ----

    def connect(self) -> dict:
        mt5 = self.mt5
        kwargs = {
            "login": int(self.config["login"]),
            "password": self.config["password"],
            "server": self.config["server"],
            "timeout": 60_000,
        }
        path = normalize_terminal_path(self.config.get("terminal_path", ""))
        problem = terminal_path_problem(path)
        if problem:
            return {"status": "error", "reason": f"TERMINAL_PATH_INVALID: {problem}", "info": {}}
        if path:
            kwargs["path"] = path
        if not mt5.initialize(**kwargs):
            code, message = mt5.last_error()
            mt5.shutdown()
            return {"status": "error", "reason": connect_error(code, message), "info": {}}
        self.initialized = True

        account = mt5.account_info()
        if account is None or account.login != int(self.config["login"]):
            return self._fail("LOGIN_FAILED: terminal is logged into another account")
        if account.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            return self._fail("NETTING_NOT_SUPPORTED: only hedging accounts can be used")
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
        if account.login != int(self.config["login"]):
            # Someone (or another account's worker) logged this MetaTrader into a different account.
            # Never trade then: the order would land on the wrong account.
            return {"status": "error", "reason": f"WRONG_ACCOUNT_IN_TERMINAL: this MetaTrader is now logged into "
                    f"{account.login}. Two accounts probably share one MetaTrader: give each account its own "
                    f"Terminal path", "info": {}}
        if not terminal.connected:
            return {"status": "disconnected", "reason": "NO_BROKER_CONNECTION", "info": {}}
        info = {
            "balance": account.balance,
            "equity": account.equity,
            "profit": account.profit,
            "currency": account.currency,
            "algoTrading": bool(terminal.trade_allowed),
            "terminalPath": os.path.join(terminal.path, TERMINAL_EXE) if terminal.path else "",
        }
        reason = None if terminal.trade_allowed else "ALGO_TRADING_OFF_IN_TERMINAL"
        return {"status": "connected", "reason": reason, "info": info}

    def shutdown(self) -> None:
        if self.initialized:
            self.mt5.shutdown()
            self.initialized = False

    def account_info(self) -> dict:
        """Asks the terminal for the account's numbers right now (the "Check" in the UI)."""
        terminal = self.mt5.terminal_info()
        account = self.mt5.account_info()
        if terminal is None or account is None:
            code, message = self.mt5.last_error()
            return {"status": "rejected", "reason": f"NO_ANSWER_FROM_TERMINAL: {code} {message}"}
        positions = self.mt5.positions_total()
        return {
            "status": "ok", "login": account.login, "name": account.name, "server": account.server,
            "company": account.company, "currency": account.currency, "balance": account.balance,
            "equity": account.equity, "profit": account.profit, "margin": account.margin,
            "freeMargin": account.margin_free, "marginLevel": account.margin_level, "leverage": account.leverage,
            "openPositions": positions if positions is not None else None,
            "demo": account.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO,
            "brokerConnected": bool(terminal.connected), "algoTrading": bool(terminal.trade_allowed),
            "accountTradeAllowed": bool(account.trade_allowed),
            "pingMs": round(terminal.ping_last / 1000, 1) if terminal.ping_last else None,  # MT5 gives microseconds
        }

    # ---- trading ----

    def _filling(self, symbol_info) -> int:
        mt5 = self.mt5
        if symbol_info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if symbol_info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _broker_symbol(self, symbol: str) -> tuple[str | None, str | None]:
        """This broker's name for a symbol: the name itself if it exists, otherwise the one with the same
        core name (master XAUUSD.C -> XAUUSD here) when the suffix or a mapping was not set."""
        if symbol in self._symbol_names:
            return self._symbol_names[symbol], None
        mt5 = self.mt5
        if mt5.symbol_info(symbol) is not None:
            return symbol, None
        disabled = mt5.SYMBOL_TRADE_MODE_DISABLED
        available = [{"name": s.name, "visible": s.visible, "tradable": s.trade_mode != disabled}
                     for s in mt5.symbols_get() or ()]
        name, reason = match_symbol(symbol, available)
        if name:
            self._symbol_names[symbol] = name
        return name, reason

    def open(self, order: dict) -> dict:
        mt5 = self.mt5
        symbol, reason = self._broker_symbol(order["symbol"])
        if symbol is None:
            return {"status": "rejected", "reason": reason}
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

        started = time.perf_counter()
        result = mt5.order_send(request)
        elapsed = _ms_since(started)
        if result is None:
            code, message = mt5.last_error()
            return {"status": "rejected", "reason": f"ORDER_SEND_FAILED: {code} {message}", "elapsedMs": elapsed}
        if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
            return {"status": "filled", "ticket": result.order, "price": result.price, "volume": result.volume,
                    "symbol": symbol, "elapsedMs": elapsed}
        if result.retcode == mt5.TRADE_RETCODE_PLACED:
            return {"status": "placed", "ticket": result.order, "price": request["price"], "volume": volume,
                    "symbol": symbol, "elapsedMs": elapsed}
        return {"status": "rejected", "reason": f"BROKER_{result.retcode}: {result.comment}", "elapsedMs": elapsed}

    def close(self, magic: int, symbol: str | None) -> dict:
        mt5 = self.mt5
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        closed, cancelled, failures = 0, 0, []

        for position in [p for p in positions or () if p.magic == magic]:
            tick = mt5.symbol_info_tick(position.symbol)
            info = mt5.symbol_info(position.symbol)
            if tick is None or info is None:
                failures.append({"ticket": position.ticket, "reason": "NO_PRICE"})
                continue
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
            elif result is not None and result.retcode == POSITION_CLOSED and self._find_position(position.ticket) == ():
                closed += 1  # its own SL/TP closed it a moment before
            else:
                failures.append({"ticket": position.ticket, "reason": _retcode_text(mt5, result)})

        for pending in [o for o in orders or () if o.magic == magic]:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": pending.ticket})
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                cancelled += 1
            else:
                failures.append({"ticket": pending.ticket, "reason": _retcode_text(mt5, result)})

        return _close_result(closed, cancelled, failures)

    # ---- copier ----

    def _positions_or_none(self, **filters):
        """positions_get() that tells "no positions" apart from "could not read them"."""
        positions = self.mt5.positions_get(**filters)
        if positions is None:
            code, _ = self.mt5.last_error()
            return () if code == 1 else None  # 1 = RES_S_OK: the call worked, there is just nothing
        return positions

    def _orders_or_none(self, **filters):
        """orders_get() that tells "no orders" apart from "could not read them"."""
        orders = self.mt5.orders_get(**filters)
        if orders is None:
            code, _ = self.mt5.last_error()
            return () if code == 1 else None
        return orders

    def _pending_kinds(self) -> dict:
        """MT5 order types the copier copies as waiting orders. Stop-limit orders are not among them:
        the position they open is copied at market instead."""
        mt5 = self.mt5
        return {mt5.ORDER_TYPE_BUY_LIMIT: ("buy", "limit"), mt5.ORDER_TYPE_SELL_LIMIT: ("sell", "limit"),
                mt5.ORDER_TYPE_BUY_STOP: ("buy", "stop"), mt5.ORDER_TYPE_SELL_STOP: ("sell", "stop")}

    def snapshot(self, with_age: bool = False) -> dict | None:
        """Open positions and waiting limit/stop orders. A filled order turns into a position with the
        same ticket. Orders are read first: an order filling in between then shows up twice (the
        position wins) rather than not at all."""
        mt5 = self.mt5
        orders = self._orders_or_none()
        positions = self._positions_or_none()
        if orders is None or positions is None:
            return None
        kinds = self._pending_kinds()
        snapshot = {}
        for o in orders:
            if o.type not in kinds:
                continue
            side, kind = kinds[o.type]
            item = {"ticket": o.ticket, "symbol": o.symbol, "side": side, "volume": o.volume_current,
                    "price": o.price_open, "sl": o.sl, "tp": o.tp, "magic": o.magic, "pending": True, "type": kind}
            if with_age:
                item["ageS"] = None  # a waiting order is copied whatever its age: its price has not been taken yet
            snapshot[o.ticket] = item
        for p in positions:
            item = {"ticket": p.ticket, "symbol": p.symbol, "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                    "volume": p.volume, "price": p.price_open, "sl": p.sl, "tp": p.tp, "magic": p.magic,
                    "pending": False, "type": "market"}
            if with_age:
                # Age in broker server time: the last quote of the symbol is the best "now" the terminal has.
                tick = mt5.symbol_info_tick(p.symbol)
                item["ageS"] = (tick.time_msc - p.time_msc) / 1000 if tick is not None else None
            snapshot[p.ticket] = item
        return snapshot

    def _find_position(self, ticket: int, magic: int | None = None, comment: str | None = None):
        """The position with this ticket, or the one a waiting order with this ticket turned into, or
        (with magic and comment) a copy opened for it. () if none, None if positions can't be read."""
        found = self._positions_or_none(ticket=ticket)
        if found:
            return found
        positions = self._positions_or_none()
        if positions is None:
            return None
        return tuple(p for p in positions if p.identifier == ticket
                     or (comment is not None and p.magic == magic and p.comment == comment))

    @staticmethod
    def _filled(position, **extra) -> dict:
        return {"status": "filled", "ticket": position.ticket, "price": position.price_open, "volume": position.volume,
                "sl": position.sl, "tp": position.tp, **extra}

    def modify_order(self, ticket: int, price: float, sl: float, tp: float) -> dict:
        """Moves a waiting order's price, SL and TP. If it has filled meanwhile, says so instead."""
        mt5 = self.mt5
        found = self._orders_or_none(ticket=ticket)
        if not found:
            position = self._find_position(ticket)
            if position:
                return self._filled(position[0])
            return {"status": "rejected", "reason": "ORDER_NOT_FOUND"}
        order = found[0]
        info = mt5.symbol_info(order.symbol)
        digits = info.digits if info is not None else 5
        started = time.perf_counter()
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": ticket,
            "symbol": order.symbol,
            "price": round(price, digits),
            "sl": round(sl, digits) if sl else 0.0,
            "tp": round(tp, digits) if tp else 0.0,
            "type_time": order.type_time,
            "expiration": order.time_expiration,
        })
        elapsed = _ms_since(started)
        if result is not None and result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_NO_CHANGES):
            return {"status": "modified", "elapsedMs": elapsed}
        return {"status": "rejected", "reason": _retcode_text(mt5, result), "elapsedMs": elapsed}

    def cancel_order(self, ticket: int) -> dict:
        """Deletes a waiting order. If it has filled meanwhile, says so (the copier then closes it)."""
        mt5 = self.mt5
        found = self._orders_or_none(ticket=ticket)
        if found is None:
            code, message = mt5.last_error()
            return {"status": "rejected", "reason": f"CANNOT_READ_ORDERS: {code} {message}"}
        if found:
            started = time.perf_counter()
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
            elapsed = _ms_since(started)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                return {"status": "cancelled", "elapsedMs": elapsed}
        position = self._find_position(ticket)
        if position:
            return self._filled(position[0])
        if found:
            return {"status": "rejected", "reason": _retcode_text(mt5, result), "elapsedMs": elapsed}
        return {"status": "cancelled", "detail": {"alreadyGone": True}}  # deleted by hand or expired

    def fill_order(self, ticket: int, order: dict) -> dict:
        """The master's waiting order filled: makes sure this account has the position too. Uses the
        account's own order if it filled already, otherwise deletes it and opens at market."""
        mt5 = self.mt5
        position = self._find_position(ticket, order["magic"], order["comment"])
        if position is None:
            code, message = mt5.last_error()
            return {"status": "rejected", "reason": f"CANNOT_READ_POSITIONS: {code} {message}", "orderRemoved": False}
        if position:
            return self._filled(position[0], own=True)
        found = self._orders_or_none(ticket=ticket)
        if found:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                position = self._find_position(ticket)  # it may have filled a moment ago
                if position:
                    return self._filled(position[0], own=True)
                return {"status": "rejected", "reason": _retcode_text(mt5, result), "orderRemoved": False}
        return _open_keeping_trade(self.open, order)

    def close_position(self, ticket: int) -> dict:
        mt5 = self.mt5
        found = self._find_position(ticket)
        if found is None:
            code, message = mt5.last_error()
            return {"status": "rejected", "reason": f"CANNOT_READ_POSITIONS: {code} {message}"}
        if not found:
            return {"status": "closed", "detail": {"alreadyClosed": True}}  # e.g. its own SL or TP hit first
        position = found[0]
        tick = mt5.symbol_info_tick(position.symbol)
        info = mt5.symbol_info(position.symbol)
        if tick is None or info is None:
            return {"status": "rejected", "reason": "NO_PRICE"}
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        started = time.perf_counter()
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.config["deviation_points"],
            "magic": position.magic,
            "type_filling": self._filling(info),
            "type_time": mt5.ORDER_TIME_GTC,
        })
        elapsed = _ms_since(started)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"status": "closed", "price": result.price, "volume": result.volume, "elapsedMs": elapsed}
        if result is not None and result.retcode == POSITION_CLOSED and self._find_position(ticket) == ():
            # Closed a moment before: the copy has the master's SL/TP, so it is usually hit at the same time.
            return {"status": "closed", "detail": {"alreadyClosed": True}, "elapsedMs": elapsed}
        return {"status": "rejected", "reason": _retcode_text(mt5, result), "elapsedMs": elapsed}

    def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        mt5 = self.mt5
        found = self._find_position(ticket)
        if not found:
            return {"status": "rejected", "reason": "POSITION_NOT_FOUND"}
        position = found[0]
        info = mt5.symbol_info(position.symbol)
        digits = info.digits if info is not None else 5
        started = time.perf_counter()
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": round(sl, digits) if sl else 0.0,
            "tp": round(tp, digits) if tp else 0.0,
            "magic": position.magic,
        })
        elapsed = _ms_since(started)
        if result is not None and result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_NO_CHANGES):
            return {"status": "modified", "elapsedMs": elapsed}
        return {"status": "rejected", "reason": _retcode_text(mt5, result), "elapsedMs": elapsed}

    def positions(self, magic: int | None) -> dict:
        mt5 = self.mt5
        positions = [p for p in mt5.positions_get() or () if magic is None or p.magic == magic]
        orders = [o for o in mt5.orders_get() or () if magic is None or o.magic == magic]
        buy_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT}
        kind_names = {mt5.ORDER_TYPE_BUY_LIMIT: "limit", mt5.ORDER_TYPE_SELL_LIMIT: "limit",
                      mt5.ORDER_TYPE_BUY_STOP: "stop", mt5.ORDER_TYPE_SELL_STOP: "stop",
                      mt5.ORDER_TYPE_BUY_STOP_LIMIT: "stop limit", mt5.ORDER_TYPE_SELL_STOP_LIMIT: "stop limit"}
        return {
            "status": "ok",
            "positions": [
                {"ticket": p.ticket, "symbol": p.symbol, "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                 "volume": p.volume, "price": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit,
                 "magic": p.magic, "comment": p.comment}
                for p in positions
            ],
            "orders": [
                {"ticket": o.ticket, "symbol": o.symbol, "side": "buy" if o.type in buy_types else "sell",
                 "type": kind_names.get(o.type, ""), "volume": o.volume_current, "price": o.price_open,
                 "sl": o.sl, "tp": o.tp, "magic": o.magic, "comment": o.comment}
                for o in orders
            ],
        }


def _open_keeping_trade(open_fn, order: dict) -> dict:
    """Opens at market after this account's waiting order was deleted. If the broker refuses the SL/TP,
    opens without them rather than missing the trade."""
    result = open_fn({**order, "type": "market"})
    if result["status"] == "rejected" and (result.get("reason") or "").startswith("BROKER_10016") \
            and (order.get("sl") or order.get("tp")):
        refused = result["reason"]
        result = open_fn({**order, "type": "market", "sl": None, "tp": None})
        if result["status"] == "filled":
            result.update(sl=0.0, tp=0.0, stopsRefused=refused)
    if result["status"] == "filled":
        result.setdefault("sl", order.get("sl") or 0.0)
        result.setdefault("tp", order.get("tp") or 0.0)
    else:
        result["orderRemoved"] = True
    return result


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


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
        profit = round(sum(p["profit"] for p in self.positions_by_ticket.values()), 2)
        return {"status": "connected", "reason": None,
                "info": {"balance": 10000.0, "equity": 10000.0 + profit, "profit": profit, "currency": "USD",
                         "algoTrading": True, "terminalPath": ""}}

    def shutdown(self) -> None:
        self.connected = False

    def account_info(self) -> dict:
        return {
            "status": "ok", "login": int(self.config["login"]), "name": "Simulated account",
            "server": self.config["server"], "company": "Simulated broker", "currency": "USD", "balance": 10000.0,
            "equity": 10000.0, "profit": 0.0, "margin": 0.0, "freeMargin": 10000.0, "marginLevel": 0.0,
            "leverage": 100, "openPositions": len(self.positions_by_ticket), "demo": True, "brokerConnected": True,
            "algoTrading": True, "accountTradeAllowed": True, "pingMs": 0.0,
        }

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
            "ticket": ticket, "symbol": order["symbol"], "side": order["side"], "type": order["type"], "volume": volume,
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

    def snapshot(self, with_age: bool = False) -> dict | None:
        snapshot = {}
        for pending, records in ((True, self.orders_by_ticket), (False, self.positions_by_ticket)):
            for ticket, p in records.items():
                item = {k: p[k] for k in ("ticket", "symbol", "side", "type", "volume", "price", "sl", "tp", "magic")}
                item["pending"] = pending
                if with_age:
                    item["ageS"] = None if pending else 0.0
                snapshot[ticket] = item
        return snapshot

    def fill_pending(self, ticket: int) -> dict:
        """Test helper: the price reached this waiting order, so it becomes a position with the same ticket."""
        order = self.orders_by_ticket.pop(ticket, None)
        if order is None:
            return {"status": "rejected", "reason": "ORDER_NOT_FOUND"}
        self.positions_by_ticket[ticket] = {**order, "type": "market"}
        return {"status": "filled", "ticket": ticket}

    def _filled(self, position: dict, **extra) -> dict:
        return {"status": "filled", "ticket": position["ticket"], "price": position["price"],
                "volume": position["volume"], "sl": position["sl"], "tp": position["tp"], **extra}

    def modify_order(self, ticket: int, price: float, sl: float, tp: float) -> dict:
        order = self.orders_by_ticket.get(ticket)
        if order is None:
            if ticket in self.positions_by_ticket:
                return self._filled(self.positions_by_ticket[ticket])
            return {"status": "rejected", "reason": "ORDER_NOT_FOUND"}
        order.update(price=price, sl=sl or 0.0, tp=tp or 0.0)
        return {"status": "modified", "elapsedMs": 0.0}

    def cancel_order(self, ticket: int) -> dict:
        if self.orders_by_ticket.pop(ticket, None) is not None:
            return {"status": "cancelled", "elapsedMs": 0.0}
        if ticket in self.positions_by_ticket:
            return self._filled(self.positions_by_ticket[ticket])
        return {"status": "cancelled", "detail": {"alreadyGone": True}}

    def fill_order(self, ticket: int, order: dict) -> dict:
        own = self.positions_by_ticket.get(ticket) or next(
            (p for p in self.positions_by_ticket.values()
             if p["magic"] == order["magic"] and p["comment"] == order["comment"][:MAX_COMMENT]), None)
        if own is not None:
            return self._filled(own, own=True)
        self.orders_by_ticket.pop(ticket, None)
        return _open_keeping_trade(self.open, order)

    def close_position(self, ticket: int) -> dict:
        position = self.positions_by_ticket.pop(ticket, None)
        if position is None:
            return {"status": "closed", "detail": {"alreadyClosed": True}}
        return {"status": "closed", "price": position["price"], "volume": position["volume"], "elapsedMs": 0.0}

    def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        position = self.positions_by_ticket.get(ticket)
        if position is None:
            return {"status": "rejected", "reason": "POSITION_NOT_FOUND"}
        position["sl"], position["tp"] = sl or 0.0, tp or 0.0
        return {"status": "modified", "elapsedMs": 0.0}

    def positions(self, magic: int | None) -> dict:
        return {
            "status": "ok",
            "positions": [p for p in self.positions_by_ticket.values() if magic is None or p["magic"] == magic],
            "orders": [o for o in self.orders_by_ticket.values() if magic is None or o["magic"] == magic],
        }
