"""A copy closed by its own SL/TP the moment StakBridge closes it: the broker answers 10036
("Position doesn't exist"). That is a finished close, not a failure to retry and warn about."""

from types import SimpleNamespace

from stakbridge.workers.backends import Mt5Backend


class _RacingMt5:
    """One buy position that its SL/TP closes while the close order is on its way (when gone_on_send)."""

    TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE = 1, 8
    POSITION_TYPE_BUY, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, ORDER_TIME_GTC = 0, 1, 2, 0
    TRADE_RETCODE_DONE = 10009

    def __init__(self, gone_on_send=True):
        self.positions = [SimpleNamespace(ticket=555, identifier=555, symbol="XAUUSD", type=0, volume=0.1,
                                          magic=990000, comment="copy 7")]
        self.gone_on_send = gone_on_send

    def positions_get(self, ticket=None, symbol=None):
        return tuple(p for p in self.positions if ticket is None or p.ticket == ticket)

    def orders_get(self, symbol=None):
        return ()

    def last_error(self):
        return 1, "Success"

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=2320.0, ask=2320.5)

    def symbol_info(self, symbol):
        return SimpleNamespace(filling_mode=1, digits=2)

    def order_send(self, request):
        if self.gone_on_send:
            self.positions = []
        return SimpleNamespace(retcode=10036, comment="Position doesn't exist", price=0.0, volume=0.0)


def _backend(mt5):
    backend = Mt5Backend.__new__(Mt5Backend)  # skips importing MetaTrader5
    backend.mt5, backend.config, backend.initialized, backend._symbol_names = mt5, {"deviation_points": 20}, True, {}
    return backend


def test_position_gone_during_the_close_counts_as_closed():
    result = _backend(_RacingMt5()).close_position(555)
    assert result["status"] == "closed" and result["detail"] == {"alreadyClosed": True}


def test_10036_while_the_position_is_still_there_is_still_a_failure():
    result = _backend(_RacingMt5(gone_on_send=False)).close_position(555)
    assert result["status"] == "rejected" and result["reason"].startswith("BROKER_10036")


def test_close_all_counts_a_position_gone_meanwhile_as_closed():
    result = _backend(_RacingMt5()).close(990000, None)
    assert result["status"] == "closed" and result["detail"]["closedPositions"] == 1
