"""Finding a client broker's own name for a master symbol (XAUUSD.C on the master, XAUUSD on the client)."""

from types import SimpleNamespace

import pytest

from stakbridge.workers.backends import Mt5Backend, match_symbol, symbol_core


def _symbols(*names, hidden=(), disabled=()):
    return [{"name": n, "visible": n not in hidden, "tradable": n not in disabled} for n in names]


@pytest.mark.parametrize("name, core", [
    ("XAUUSD", "XAUUSD"), ("XAUUSD.C", "XAUUSD"), ("XAUUSD.c", "XAUUSD"), ("XAUUSDm", "XAUUSD"),
    ("XAUUSDc", "XAUUSD"), ("#XAUUSD", "XAUUSD"), ("XAUUSD_i", "XAUUSD"), ("XAUUSD+", "XAUUSD"),
    ("XAUUSD.pro", "XAUUSD"), ("US30.cash", "US30"), ("GER40", "GER40"), ("Brent", "BRENT"), ("", ""),
])
def test_symbol_core(name, core):
    assert symbol_core(name) == core


def test_master_suffix_is_dropped_on_a_client_without_one():
    assert match_symbol("XAUUSD.C", _symbols("EURUSD", "XAUUSD", "XAGUSD")) == ("XAUUSD", None)


def test_client_suffix_is_found_when_the_master_has_none():
    assert match_symbol("XAUUSD", _symbols("EURUSDm", "XAUUSDm")) == ("XAUUSDm", None)
    assert match_symbol("XAUUSD.C", _symbols("XAUUSD.r")) == ("XAUUSD.r", None)


def test_unknown_symbol_is_refused():
    name, reason = match_symbol("XAUUSD.C", _symbols("EURUSD", "XAUEUR"))
    assert name is None and reason == "UNKNOWN_SYMBOL: XAUUSD.C"


def test_symbols_that_cannot_be_traded_are_ignored():
    assert match_symbol("XAUUSD.C", _symbols("XAUUSD", "XAUUSD.r", disabled=("XAUUSD",))) == ("XAUUSD.r", None)


def test_several_matches_prefer_the_one_in_market_watch():
    assert match_symbol("XAUUSD.C", _symbols("XAUUSD", "XAUUSD.pro", hidden=("XAUUSD.pro",))) == ("XAUUSD", None)


def test_several_matches_in_market_watch_ask_for_a_mapping():
    name, reason = match_symbol("XAUUSD.C", _symbols("XAUUSD", "XAUUSD.pro"))
    assert name is None
    assert "XAUUSD, XAUUSD.pro" in reason and "symbol mapping" in reason


class _FakeMt5:
    SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, names):
        self.names = names
        self.listed = 0

    def symbol_info(self, name):
        return SimpleNamespace(name=name) if name in self.names else None

    def symbols_get(self):
        self.listed += 1
        return [SimpleNamespace(name=n, visible=True, trade_mode=4) for n in self.names]


def _backend(names):
    backend = Mt5Backend.__new__(Mt5Backend)  # skips importing MetaTrader5
    backend.mt5, backend.config, backend.initialized, backend._symbol_names = _FakeMt5(names), {}, True, {}
    return backend


def test_backend_uses_the_exact_name_when_it_exists():
    backend = _backend(["XAUUSD.C", "XAUUSD"])
    assert backend._broker_symbol("XAUUSD.C") == ("XAUUSD.C", None)
    assert backend.mt5.listed == 0


def test_backend_finds_its_own_name_once_and_remembers_it():
    backend = _backend(["EURUSD", "XAUUSD"])
    assert backend._broker_symbol("XAUUSD.C") == ("XAUUSD", None)
    assert backend._broker_symbol("XAUUSD.C") == ("XAUUSD", None)
    assert backend.mt5.listed == 1
