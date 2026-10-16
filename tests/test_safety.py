"""Safety rules from section 6 of the spec and the edge cases decided before coding."""

from stakbridge.workers.backends import normalize_volume

from conftest import wait_until


def test_requests_without_a_session_are_refused(bridge):
    bridge.client.cookies.clear()
    assert bridge.client.get("/v1/health").status_code == 401
    assert bridge.client.get("/v1/health", headers={"Authorization": "Bearer anything"}).status_code == 401


def test_new_accounts_start_with_trading_off(bridge):
    bridge.add_account("acct_x", 1111, trading_on=False)
    account = bridge.get("/accounts").json()["accounts"][0]
    assert account["tradingOn"] is False


def test_password_never_returned(bridge):
    bridge.add_account("acct_x", 1111)
    for body in (bridge.get("/accounts").text, bridge.get("/health").text):
        assert "secret-pass" not in body and "password" not in body.lower()
    bad = bridge.post("/accounts", {"name": "x", "login": "not-a-number", "server": "s", "password": "secret-pass"})
    assert bad.status_code == 400 and "secret-pass" not in bad.text


def test_strategy_endpoints_are_gone(bridge):
    assert bridge.get("/strategies").status_code in (404, 405)
    assert bridge.post("/trade", {"requestId": "t-1"}).status_code in (404, 405)


def test_bad_payloads_return_400(bridge):
    assert bridge.post("/accounts", {"name": "x", "login": 1, "server": "s", "password": "p",
                                     "symbol_suffx": "typo"}).status_code == 400
    assert bridge.put("/copier", {"masterId": None, "clients": [{"accountId": "x", "multiplier": 0}]}).status_code == 400


def test_edit_and_disable_account(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")

    edited = bridge.patch("/accounts/acct_x", {"name": "Renamed", "symbolSuffix": ".pro"}).json()
    assert edited["name"] == "Renamed" and edited["symbolSuffix"] == ".pro"

    assert bridge.patch("/accounts/acct_x", {"enabled": False}).json()["status"] == "disabled"
    bridge.patch("/accounts/acct_x", {"enabled": True})
    bridge.wait_connected("acct_x")


def test_volume_is_rounded_to_broker_lot_step():
    assert normalize_volume(0.157, 0.01, 100, 0.01) == (0.15, None)
    assert normalize_volume(0.35, 0.1, 50, 0.1) == (0.3, None)
    assert normalize_volume(0.05, 0.1, 50, 0.1) == (None, "VOLUME_BELOW_BROKER_MIN")
    assert normalize_volume(60, 0.1, 50, 0.1) == (None, "VOLUME_ABOVE_BROKER_MAX")
