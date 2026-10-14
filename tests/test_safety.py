"""Safety rules from section 6 of the spec and the edge cases decided before coding."""

from stakbridge.workers.backends import normalize_volume

from conftest import wait_until


def test_requests_without_valid_token_are_refused(bridge):
    assert bridge.client.get("/v1/health").status_code == 401
    assert bridge.client.get("/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert bridge.get("/health").status_code == 200


def test_new_accounts_start_with_trading_off(bridge):
    bridge.add_account("acct_x", 1111, trading_on=False)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])
    res = bridge.trade("t-1", strategy="s1")
    assert res.json()["results"] == [{"accountId": "acct_x", "status": "skipped", "reason": "TRADING_OFF"}]


def test_password_never_returned(bridge):
    bridge.add_account("acct_x", 1111)
    for body in (bridge.get("/accounts").text, bridge.get("/health").text):
        assert "secret-pass" not in body and "password" not in body.lower()
    bad = bridge.post("/accounts", {"name": "x", "login": "not-a-number", "server": "s", "password": "secret-pass"})
    assert bad.status_code == 400 and "secret-pass" not in bad.text


def test_paused_strategy_rejects_opens_but_allows_close(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])
    bridge.trade("t-1", strategy="s1")
    bridge.patch("/strategies/s1", {"active": False})

    res = bridge.trade("t-2", strategy="s1")
    assert res.status_code == 409 and res.json()["error"]["code"] == "STRATEGY_PAUSED"
    close = bridge.post("/trade", {"requestId": "t-3", "strategy": "s1", "action": "close"})
    assert close.json()["results"][0]["status"] == "closed"


def test_strategy_without_accounts_returns_409(bridge):
    bridge.add_strategy("lonely")
    res = bridge.trade("t-1", strategy="lonely")
    assert res.status_code == 409 and res.json()["error"]["code"] == "NO_ELIGIBLE_ACCOUNT"


def test_all_assigned_accounts_offline_still_returns_200(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])
    bridge.set_offline("acct_x")
    wait_until(lambda: bridge.status("acct_x") == "disconnected")
    res = bridge.trade("t-1", strategy="s1")
    assert res.status_code == 200
    assert res.json()["results"][0]["reason"] == "DISCONNECTED"


def test_bad_payloads_return_400(bridge):
    bridge.add_strategy("s1")
    assert bridge.trade("t-1", strategy="s1", type="limit").status_code == 400  # limit without price
    assert bridge.trade("t-2", strategy="s1", side="up").status_code == 400
    assert bridge.post("/trade", {"requestId": "t-3", "strategy": "s1", "action": "open",
                                  "symbol": "XAUUSD", "side": "buy", "type": "market", "volume": 0.1,
                                  "strategy_tag": "typo"}).status_code == 400


def test_trade_is_stored_and_listed(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])
    bridge.trade("t-1", strategy="s1")
    stored = bridge.get("/trades/t-1").json()
    assert stored["results"][0]["status"] == "filled"
    assert bridge.get("/trades").json()["trades"][0]["succeeded"] == 1
    assert bridge.get("/trades/missing").status_code == 404


def test_edit_and_disable_account(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])

    edited = bridge.patch("/accounts/acct_x", {"name": "Renamed", "symbolSuffix": ".pro"}).json()
    assert edited["name"] == "Renamed" and edited["symbolSuffix"] == ".pro"
    bridge.trade("t-1", strategy="s1")
    assert bridge.positions("acct_x")[0]["symbol"] == "XAUUSD.pro"

    assert bridge.patch("/accounts/acct_x", {"enabled": False}).json()["status"] == "disabled"
    res = bridge.trade("t-2", strategy="s1")
    assert res.json()["results"] == [{"accountId": "acct_x", "status": "skipped", "reason": "DISCONNECTED"}]

    bridge.patch("/accounts/acct_x", {"enabled": True})
    bridge.wait_connected("acct_x")


def test_request_id_with_slash_can_be_fetched(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", accounts=["acct_x"])
    bridge.trade("2026/10/14-1", strategy="s1")
    assert bridge.get("/trades/2026%2F10%2F14-1").json()["requestId"] == "2026/10/14-1"


def test_volume_is_rounded_to_broker_lot_step():
    assert normalize_volume(0.157, 0.01, 100, 0.01) == (0.15, None)
    assert normalize_volume(0.35, 0.1, 50, 0.1) == (0.3, None)
    assert normalize_volume(0.05, 0.1, 50, 0.1) == (None, "VOLUME_BELOW_BROKER_MIN")
    assert normalize_volume(60, 0.1, 50, 0.1) == (None, "VOLUME_ABOVE_BROKER_MAX")
