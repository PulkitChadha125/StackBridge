"""The nine acceptance tests from section 7 of the spec, run against the fake MT5 backend."""

from conftest import wait_until


def results_by_account(response):
    return {r["accountId"]: r for r in response.json()["results"]}


def test_1_connect_three_terminals(three_accounts):
    accounts = three_accounts.get("/accounts").json()["accounts"]
    assert [a["status"] for a in accounts] == ["connected"] * 3


def test_2_trade_reaches_only_assigned_accounts(three_accounts):
    b = three_accounts
    res = b.trade("sig-0001", sl=2310.50, tp=2340.00)
    assert res.status_code == 200, res.text
    results = results_by_account(res)
    assert set(results) == {"acct_a", "acct_b"}  # acct_c is not even listed
    assert all(r["status"] == "filled" for r in results.values())

    assert len(b.positions("acct_a")) == 1
    assert b.positions("acct_b")[0]["symbol"] == "XAUUSD.m"  # per-account suffix applied
    assert b.positions("acct_c") == []


def test_3_unknown_tag_is_rejected(three_accounts):
    b = three_accounts
    res = b.trade("sig-0002", strategy="does_not_exist")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "UNKNOWN_STRATEGY"
    missing_tag = b.post("/trade", {"requestId": "sig-0003", "action": "open", "symbol": "XAUUSD",
                                    "side": "buy", "type": "market", "volume": 0.1})
    assert missing_tag.status_code == 400
    assert all(b.positions(a) == [] for a in ("acct_a", "acct_b", "acct_c"))


def test_4_disconnected_account_is_skipped(three_accounts):
    b = three_accounts
    b.set_offline("acct_b")
    wait_until(lambda: b.status("acct_b") == "disconnected")

    results = results_by_account(b.trade("sig-0004"))
    assert results["acct_a"]["status"] == "filled"
    assert results["acct_b"] == {"accountId": "acct_b", "status": "skipped", "reason": "DISCONNECTED"}

    b.set_offline("acct_b", offline=False)
    b.wait_connected("acct_b")
    assert b.positions("acct_b") == []


def test_5_same_request_id_never_trades_twice(three_accounts):
    b = three_accounts
    first = b.trade("sig-0005")
    second = b.trade("sig-0005")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["results"] == first.json()["results"]
    assert len(b.positions("acct_a")) == 1 and len(b.positions("acct_b")) == 1

    reused = b.trade("sig-0005", volume=0.5)  # same id, different order
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "REQUEST_ID_REUSED"


def test_6_volume_above_strategy_max_is_rejected(three_accounts):
    b = three_accounts
    res = b.trade("sig-0006", volume=1.5)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VOLUME_TOO_LARGE"
    assert b.positions("acct_a") == [] and b.positions("acct_b") == []


def test_7_stop_all_blocks_new_trades(three_accounts):
    b = three_accounts
    b.trade("sig-0007a")
    assert b.post("/kill").json() == {"killSwitch": True}

    res = b.trade("sig-0007b")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "KILL_SWITCH_ON"
    assert len(b.positions("acct_a")) == 1  # only the trade from before the stop

    # Closing is still allowed while stopped, so positions can be flattened.
    close = b.post("/trade", {"requestId": "sig-0007c", "strategy": "gold_scalper", "action": "close"})
    assert close.status_code == 200
    assert b.positions("acct_a") == []

    b.delete("/kill")
    assert b.trade("sig-0007d").status_code == 200


def test_8_assignment_change_applies_to_next_request(three_accounts):
    b = three_accounts
    assert b.put("/strategies/gold_scalper/accounts", {"accountIds": ["acct_c"]}).status_code == 200
    results = results_by_account(b.trade("sig-0008"))
    assert set(results) == {"acct_c"}
    assert b.positions("acct_a") == [] and len(b.positions("acct_c")) == 1


def test_9_close_by_strategy_only_closes_that_strategy(three_accounts):
    b = three_accounts
    b.trade("sig-0009a")  # gold_scalper on A and B
    b.trade("sig-0009b", strategy="swing_eur", symbol="EURUSD", volume=1.0)  # swing_eur on B and C
    b.trade("sig-0009c", type="limit", price=2300.0)  # pending gold_scalper orders

    res = b.post("/trade", {"requestId": "sig-0009d", "strategy": "gold_scalper", "action": "close"})
    results = results_by_account(res)
    assert results["acct_b"]["status"] == "closed"
    assert results["acct_b"]["detail"] == {"closedPositions": 1, "cancelledOrders": 1}

    assert b.positions("acct_a") == []
    remaining_b = b.positions("acct_b")
    assert [p["symbol"] for p in remaining_b] == ["EURUSD.m"]  # swing_eur position untouched
    assert len(b.positions("acct_c")) == 1
