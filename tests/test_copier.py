"""Trade copier: acct_a is the master, acct_b (x1, suffix .m) and acct_c (x2) are clients.

Trades on the master are placed straight through its worker, which is how a person or EA
trading the master terminal looks to the copier.
"""

import time

import pytest
from fastapi.testclient import TestClient

from stakbridge.api import create_app
from stakbridge.config import load_settings

from conftest import Bridge, wait_until


@pytest.fixture
def make_bridge(tmp_path):
    """Starts StakBridge on a data folder. Can be called again to test a restart."""
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0,
                             copy_retry_interval=0.3, copy_check_interval=0.5)
    clients = []

    def start():
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return Bridge(client)

    yield start
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def copier(make_bridge):
    b = make_bridge()
    b.add_account("acct_a", 5012345)
    b.add_account("acct_b", 5012346, suffix=".m")
    b.add_account("acct_c", 7788120)
    b.wait_connected("acct_a", "acct_b", "acct_c")
    set_copier(b)
    wait_until(lambda: b.get("/copier").json()["watching"])
    return b


def set_copier(b, master="acct_a", clients=(("acct_b", 1.0), ("acct_c", 2.0))):
    res = b.put("/copier", {"masterId": master,
                            "clients": [{"accountId": a, "multiplier": m} for a, m in clients]})
    assert res.status_code == 200, res.text
    return res.json()


def master_open(b, **fields):
    return b.open_by_hand("acct_a", **fields)


def master_close(b):
    """Closes every position on the master."""
    for position in b.positions("acct_a"):
        b.close_by_hand("acct_a", position["ticket"])


def copies(b, client_id=None):
    rows = b.get("/copier").json()["copies"]
    return [r for r in rows if client_id is None or r["clientId"] == client_id]


def test_master_open_is_copied_with_multiplier_suffix_and_stops(copier):
    b = copier
    ticket = master_open(b, volume=0.1, sl=2300.0, tp=2350.0)
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1)

    pos_b, pos_c = b.positions("acct_b")[0], b.positions("acct_c")[0]
    assert (pos_b["symbol"], pos_b["volume"], pos_b["side"]) == ("XAUUSD.m", 0.1, "buy")
    assert (pos_c["symbol"], pos_c["volume"]) == ("XAUUSD", 0.2)
    assert (pos_b["sl"], pos_b["tp"]) == (2300.0, 2350.0)
    assert pos_b["comment"] == f"copy {ticket}"

    wait_until(lambda: all(r["status"] == "open" for r in copies(b)) and len(copies(b)) == 2)
    row = copies(b, "acct_b")[0]
    assert row["masterTicket"] == ticket and row["clientTicket"] == pos_b["ticket"]
    assert row["openMs"] is not None and row["openMs"] < 1000


def test_master_close_closes_every_client_copy(copier):
    b = copier
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1)
    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])
    wait_until(lambda: all(r["status"] == "closed" and r["closeMs"] is not None for r in copies(b)))


def test_sl_tp_change_on_master_is_copied(copier):
    b = copier
    ticket = master_open(b, sl=2300.0, tp=2350.0)
    wait_until(lambda: len(b.positions("acct_c")) == 1)
    assert b.send("acct_a", "modify_position", ticket=ticket, sl=2310.0, tp=2360.0)["status"] == "modified"
    wait_until(lambda: all((p["sl"], p["tp"]) == (2310.0, 2360.0) for a in ("acct_b", "acct_c") for p in b.positions(a)))


def test_manual_client_trades_are_left_alone(copier):
    b = copier
    b.open_by_hand("acct_b", volume=0.5)  # the client's own position
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 2)
    master_close(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    assert b.positions("acct_b")[0]["volume"] == 0.5


def test_master_trading_off_and_kill_switch_block_opens_but_not_closes(copier):
    b = copier
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)

    b.patch("/accounts/acct_a", {"tradingOn": False})
    master_open(b)
    wait_until(lambda: sum(r["reason"] == "MASTER_TRADING_OFF" for r in copies(b)) == 2)
    assert len(b.positions("acct_b")) == 1

    b.post("/kill")
    b.patch("/accounts/acct_a", {"tradingOn": True})
    master_open(b)
    wait_until(lambda: sum(r["reason"] == "KILL_SWITCH_ON" for r in copies(b)) == 2)
    master_close(b)  # closing is allowed while stopped
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])
    b.delete("/kill")


def test_trading_off_client_is_skipped_and_the_others_still_copy(copier):
    b = copier
    b.patch("/accounts/acct_c", {"tradingOn": False})
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    wait_until(lambda: copies(b, "acct_c") and copies(b, "acct_c")[0]["reason"] == "TRADING_OFF")
    assert b.positions("acct_c") == []


def test_every_account_except_the_master_is_a_client(make_bridge):
    """No client list needed: a new account copies the master at x1 as soon as its Trading is ON."""
    b = make_bridge()
    b.add_account("acct_a", 5012345)
    b.add_account("acct_b", 5012346)
    b.wait_connected("acct_a", "acct_b")
    set_copier(b, clients=())
    wait_until(lambda: b.get("/copier").json()["watching"])
    master_open(b, volume=0.3)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    assert b.positions("acct_b")[0]["volume"] == 0.3

    b.add_account("acct_c", 7788120)  # added later
    b.wait_connected("acct_c")
    master_open(b, volume=0.2)
    wait_until(lambda: len(b.positions("acct_c")) == 1 and len(b.positions("acct_b")) == 2)


def test_client_offline_at_close_is_closed_when_it_returns(copier):
    b = copier
    master_open(b)
    wait_until(lambda: len(b.positions("acct_c")) == 1)
    b.set_offline("acct_c")
    wait_until(lambda: b.status("acct_c") == "disconnected")

    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [])
    wait_until(lambda: copies(b, "acct_c")[0]["reason"] == "CLOSE_WAITING: DISCONNECTED")

    b.set_offline("acct_c", offline=False)
    b.wait_connected("acct_c")
    wait_until(lambda: b.positions("acct_c") == [])
    wait_until(lambda: copies(b, "acct_c")[0]["status"] == "closed")


def test_positions_open_before_the_copier_started_are_not_copied(make_bridge):
    b = make_bridge()
    b.add_account("acct_a", 5012345)
    b.add_account("acct_b", 5012346)
    b.wait_connected("acct_a", "acct_b")
    master_open(b)

    set_copier(b, clients=(("acct_b", 1.0),))
    wait_until(lambda: b.get("/copier").json()["watching"])
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    wait_until(lambda: len(copies(b)) == 1 and copies(b)[0]["status"] == "open")
    assert len(b.positions("acct_a")) == 2 and len(b.positions("acct_b")) == 1


def test_master_cannot_be_a_client(copier):
    res = copier.put("/copier", {"masterId": "acct_a",
                                 "clients": [{"accountId": "acct_a", "multiplier": 1}]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "MASTER_IS_CLIENT"


def test_removing_an_account_drops_it_from_the_copier(copier):
    copier.delete("/accounts/acct_c")
    config = copier.get("/copier").json()
    assert config["masterId"] == "acct_a" and [c["accountId"] for c in config["clients"]] == ["acct_b"]
    copier.delete("/accounts/acct_a")
    config = copier.get("/copier").json()
    assert config["masterId"] is None


def test_copy_whose_open_answer_was_lost_is_found_and_still_closed(copier):
    b = copier
    ticket = master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(copies(b)) == 2
               and all(r["status"] == "open" for r in copies(b)))
    client_ticket = b.positions("acct_b")[0]["ticket"]

    # Pretend the worker died after sending the order, so the copier never learned the client ticket.
    engine = b.client.app.state.copier

    def lose_answer():
        engine._copies[ticket]["acct_b"].update(status="unknown", reason="WORKER_STOPPED", client_ticket=None)
    engine._executor.submit(lose_answer).result()

    wait_until(lambda: copies(b, "acct_b")[0]["reason"] == "FOUND_AFTER_LOST_ANSWER")
    assert copies(b, "acct_b")[0]["clientTicket"] == client_ticket
    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])


def test_symbol_mapping_per_client_wins_over_the_suffix(copier):
    b = copier
    res = b.put("/copier", {"masterId": "acct_a", "clients": [
        {"accountId": "acct_b", "multiplier": 1, "symbols": {"XAUUSD": "GOLD.pro"}},
        {"accountId": "acct_c", "multiplier": 1, "symbols": {"EURUSD": "EURUSDx"}},
    ]})
    assert res.status_code == 200, res.text
    assert res.json()["clients"][0]["symbols"] == {"XAUUSD": "GOLD.pro"}

    master_open(b)  # XAUUSD
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1)
    assert b.positions("acct_b")[0]["symbol"] == "GOLD.pro"  # mapped; acct_b's ".m" suffix is not added
    assert b.positions("acct_c")[0]["symbol"] == "XAUUSD"    # no mapping for XAUUSD on acct_c

    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])


def test_symbol_mapping_must_have_both_names(copier):
    res = copier.put("/copier", {"masterId": "acct_a", "clients": [
        {"accountId": "acct_b", "multiplier": 1, "symbols": {"XAUUSD": "  "}}]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "BAD_PAYLOAD"


def test_copy_is_saved_as_pending_before_the_answer(copier):
    """If StakBridge stops while an order is on its way, the copy must already be in the database."""
    b = copier
    engine = b.client.app.state.copier
    sent = []
    real_send = engine._send

    def hold_answer(kind, row, at, **fields):
        sent.append(kind)  # record the order but never deliver it, like a crash right after sending

    engine._send = hold_answer
    try:
        master_open(b)
        wait_until(lambda: len(copies(b)) == 2)
        assert {r["status"] for r in copies(b)} == {"pending"} and sent == ["open", "open"]
    finally:
        engine._send = real_send


def orders(b):
    return b.get("/orders").json()["orders"]


def test_master_orders_are_logged_with_every_client_result(copier):
    """Trades tab: one entry per master order, with what happened on each client."""
    b = copier
    b.patch("/accounts/acct_c", {"tradingOn": False})
    ticket = master_open(b, volume=0.1, sl=2300.0, tp=2350.0)
    wait_until(lambda: len(orders(b)) == 1 and len(orders(b)[0]["copies"]) == 2
               and all(c["status"] != "pending" for c in orders(b)[0]["copies"]))
    order = orders(b)[0]
    assert (order["ticket"], order["masterId"], order["symbol"], order["side"]) == (ticket, "acct_a", "XAUUSD", "buy")
    assert (order["volume"], order["price"], order["sl"], order["tp"], order["status"]) == (0.1, 2321.45, 2300.0, 2350.0, "open")
    by_client = {c["clientId"]: c for c in order["copies"]}
    assert by_client["acct_b"]["status"] == "open" and by_client["acct_b"]["openMs"] is not None
    assert (by_client["acct_c"]["status"], by_client["acct_c"]["reason"]) == ("skipped", "TRADING_OFF")

    assert b.send("acct_a", "modify_position", ticket=ticket, sl=2310.0, tp=2360.0)["status"] == "modified"
    wait_until(lambda: orders(b)[0]["sl"] == 2310.0)
    master_close(b)
    wait_until(lambda: orders(b)[0]["status"] == "closed")
    order = orders(b)[0]
    assert [e["kind"] for e in order["events"]] == ["open", "modify", "close"]
    assert order["closedAt"] is not None
    wait_until(lambda: {c["clientId"]: c["status"] for c in orders(b)[0]["copies"]}["acct_b"] == "closed")
    assert b.get(f"/orders/{ticket}/logs").status_code == 200


def test_account_check_reads_the_balance_live(copier):
    b = copier
    res = b.get("/accounts/acct_b/check")
    assert res.status_code == 200, res.text
    info = res.json()
    assert (info["balance"], info["currency"], info["brokerConnected"]) == (10000.0, "USD", True)
    assert info["roundTripMs"] >= 0 and info["checkedAt"]

    b.patch("/accounts/acct_c", {"enabled": False})
    res = b.get("/accounts/acct_c/check")
    assert res.status_code == 409 and res.json()["error"]["code"] == "DISCONNECTED"


def test_open_pl_is_shown_live(copier):
    info = next(a for a in copier.get("/accounts").json()["accounts"] if a["id"] == "acct_b")["info"]
    assert (info["profit"], info["equity"], info["balance"]) == (0.0, 10000.0, 10000.0)


def test_open_mt5_needs_a_real_terminal(copier):
    res = copier.post("/accounts/acct_b/open-terminal")
    assert res.status_code == 409 and res.json()["error"]["code"] == "SIMULATED_BROKER"


# ---- waiting limit / stop orders ----

def waiting_orders(b, account_id):
    res = b.get(f"/accounts/{account_id}/positions")
    assert res.status_code == 200, res.text
    return res.json()["orders"]


def master_place(b, side="buy", kind="limit", price=2300.0, volume=0.1, sl=None, tp=None):
    """Places a waiting order on the master as if by hand. Returns its ticket."""
    result = b.send("acct_a", "open", order={"symbol": "XAUUSD", "side": side, "type": kind, "price": price,
                                             "volume": volume, "sl": sl, "tp": tp, "magic": 0, "comment": "hand"})
    assert result["status"] == "placed", result
    return result["ticket"]


def test_limit_order_is_copied_moved_and_deleted(copier):
    b = copier
    ticket = master_place(b, price=2300.0, sl=2290.0, tp=2350.0)
    wait_until(lambda: len(waiting_orders(b, "acct_b")) == 1 and len(waiting_orders(b, "acct_c")) == 1)
    order_b, order_c = waiting_orders(b, "acct_b")[0], waiting_orders(b, "acct_c")[0]
    assert (order_b["symbol"], order_b["side"], order_b["type"], order_b["volume"]) == ("XAUUSD.m", "buy", "limit", 0.1)
    assert (order_b["price"], order_b["sl"], order_b["tp"]) == (2300.0, 2290.0, 2350.0)
    assert order_c["volume"] == 0.2 and order_c["comment"] == f"copy {ticket}"
    assert b.positions("acct_b") == []
    wait_until(lambda: {c["status"] for c in copies(b)} == {"placed"})
    assert orders(b)[0]["status"] == "waiting" and orders(b)[0]["orderType"] == "limit"

    assert b.send("acct_a", "modify_order", ticket=ticket, price=2295.0, sl=2285.0, tp=2345.0)["status"] == "modified"
    wait_until(lambda: all((o["price"], o["sl"], o["tp"]) == (2295.0, 2285.0, 2345.0)
                           for a in ("acct_b", "acct_c") for o in waiting_orders(b, a)))

    assert b.send("acct_a", "cancel_order", ticket=ticket)["status"] == "cancelled"
    wait_until(lambda: waiting_orders(b, "acct_b") == [] and waiting_orders(b, "acct_c") == [])
    wait_until(lambda: {c["status"] for c in copies(b)} == {"cancelled"})
    wait_until(lambda: orders(b)[0]["status"] == "cancelled")
    assert [e["kind"] for e in orders(b)[0]["events"]] == ["open", "modify", "cancel"]


def test_filled_master_order_gives_every_client_the_position(copier):
    b = copier
    ticket = master_place(b, side="sell", kind="stop", price=2310.0, sl=2330.0)
    wait_until(lambda: len(waiting_orders(b, "acct_b")) == 1 and len(waiting_orders(b, "acct_c")) == 1)
    own = waiting_orders(b, "acct_b")[0]["ticket"]
    assert b.send("acct_b", "fill_pending", ticket=own)["status"] == "filled"  # acct_b's broker got there first

    assert b.send("acct_a", "fill_pending", ticket=ticket)["status"] == "filled"
    wait_until(lambda: len(b.positions("acct_c")) == 1 and waiting_orders(b, "acct_c") == [])
    assert [p["ticket"] for p in b.positions("acct_b")] == [own]  # its own fill is kept, nothing opened twice
    pos_c = b.positions("acct_c")[0]
    assert (pos_c["side"], pos_c["volume"], pos_c["sl"], pos_c["comment"]) == ("sell", 0.2, 2330.0, f"copy {ticket}")
    wait_until(lambda: {c["status"] for c in copies(b)} == {"open"})
    wait_until(lambda: orders(b)[0]["status"] == "open")
    assert copies(b, "acct_c")[0]["openMs"] is not None

    # after the fill it is an ordinary copy: SL/TP and the close are followed
    assert b.send("acct_a", "modify_position", ticket=ticket, sl=2325.0, tp=2280.0)["status"] == "modified"
    wait_until(lambda: all((p["sl"], p["tp"]) == (2325.0, 2280.0) for a in ("acct_b", "acct_c") for p in b.positions(a)))
    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])


def test_stop_all_before_the_fill_withdraws_the_client_orders(copier):
    b = copier
    ticket = master_place(b)
    wait_until(lambda: len(waiting_orders(b, "acct_b")) == 1 and len(waiting_orders(b, "acct_c")) == 1)
    b.post("/kill")
    assert b.send("acct_a", "fill_pending", ticket=ticket)["status"] == "filled"
    wait_until(lambda: waiting_orders(b, "acct_b") == [] and waiting_orders(b, "acct_c") == [])
    wait_until(lambda: [(c["status"], c["reason"]) for c in copies(b)] == [("cancelled", "KILL_SWITCH_ON")] * 2)
    assert b.positions("acct_b") == [] and b.positions("acct_c") == []
    b.delete("/kill")


def test_client_that_cannot_take_the_order_does_not_hold_up_the_others(copier):
    b = copier
    b.patch("/accounts/acct_c", {"tradingOn": False})
    ticket = master_place(b)
    wait_until(lambda: len(waiting_orders(b, "acct_b")) == 1)
    wait_until(lambda: copies(b, "acct_c") and copies(b, "acct_c")[0]["reason"] == "TRADING_OFF")
    assert waiting_orders(b, "acct_c") == []
    assert b.send("acct_a", "fill_pending", ticket=ticket)["status"] == "filled"
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    assert b.positions("acct_c") == []


# ---- periodic check: the client's copies against the master ----

def _edit_row(b, ticket, client_id, **values):
    """Changes the copier's own record of a copy, as if it had lost track of it."""
    engine = b.client.app.state.copier

    def edit():
        row = engine._copies[ticket][client_id]
        row.update(values)
        engine.db.save_copy(row)
        if values.get("status") not in ("pending", "placed", "open", "unknown"):
            del engine._copies[ticket][client_id]  # a finished copy is no longer looked after
    engine._executor.submit(edit).result()


def test_copy_still_open_after_the_master_closed_is_closed_by_the_check(copier):
    b = copier
    ticket = master_open(b)
    wait_until(lambda: len(copies(b)) == 2 and all(r["status"] == "open" for r in copies(b)))
    # StakBridge wrongly believes acct_b's copy is closed already, so the normal close skips it.
    _edit_row(b, ticket, "acct_b", status="closed", reason=None)
    master_close(b)
    wait_until(lambda: b.positions("acct_c") == [])
    wait_until(lambda: b.positions("acct_b") == [])  # closed by the periodic check
    row = copies(b, "acct_b")[0]
    assert row["status"] == "closed" and row["reason"].startswith("CLOSED_BY_CHECK")
    assert any("still open after the master closed it" in e["message"] for e in b.get("/logs?level=warning").json()["logs"])


def test_open_reported_as_failed_is_taken_back_and_closed_with_the_master(copier):
    b = copier
    ticket = master_open(b)
    wait_until(lambda: len(copies(b)) == 2 and all(r["status"] == "open" for r in copies(b)))
    client_ticket = b.positions("acct_b")[0]["ticket"]
    _edit_row(b, ticket, "acct_b", status="rejected", reason="ORDER_SEND_FAILED: -10005 IPC timeout", client_ticket=None)
    wait_until(lambda: copies(b, "acct_b")[0]["status"] == "open")
    row = copies(b, "acct_b")[0]
    assert row["clientTicket"] == client_ticket and row["reason"].startswith("FOUND_BY_CHECK (was rejected")
    master_close(b)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])


def test_copies_the_check_cannot_map_to_this_master_are_left_alone(copier):
    b = copier
    b.open_by_hand("acct_b", magic=990000)  # a copy-like position with no record (e.g. from an earlier master)
    master_open(b)
    master_close(b)
    wait_until(lambda: any("not from master acct_a" in e["message"] for e in b.get("/logs?level=warning").json()["logs"]))
    assert len(b.positions("acct_b")) == 1


def test_the_check_runs_every_second_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("STAKBRIDGE_COPY_CHECK_SECONDS", raising=False)
    assert load_settings(data_dir=tmp_path, backend="fake").copy_check_interval == 1.0


def test_the_check_keeps_its_own_pace_when_retries_are_slow(tmp_path):
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0,
                             copy_retry_interval=30.0, copy_check_interval=0.5)
    with TestClient(create_app(settings)) as client:
        b = Bridge(client)
        b.add_account("acct_a", 5012345)
        b.add_account("acct_b", 5012346, suffix=".m")
        b.wait_connected("acct_a", "acct_b")
        set_copier(b, clients=(("acct_b", 1.0),))
        wait_until(lambda: b.get("/copier").json()["watching"])
        ticket = master_open(b)
        wait_until(lambda: len(copies(b)) == 1 and copies(b)[0]["status"] == "open")
        _edit_row(b, ticket, "acct_b", status="closed", reason=None)
        master_close(b)
        # With retries every 30 s, only the check (every 0.5 s) can close it this soon.
        wait_until(lambda: b.positions("acct_b") == [], timeout=5.0)


def test_trading_off_lets_go_of_the_client_copies_and_on_starts_fresh(copier):
    b = copier
    first = master_open(b)
    wait_until(lambda: len(copies(b)) == 2 and all(r["status"] == "open" for r in copies(b)))
    b.patch("/accounts/acct_b", {"tradingOn": False})
    wait_until(lambda: copies(b, "acct_b")[0]["status"] == "released")
    assert copies(b, "acct_b")[0]["reason"] == "RELEASED_TRADING_OFF"
    master_close(b)
    wait_until(lambda: b.positions("acct_c") == [])
    b.patch("/accounts/acct_b", {"tradingOn": True})
    time.sleep(1.5)  # three periodic checks: the released copy is not closed or taken back
    assert len(b.positions("acct_b")) == 1
    assert [r["status"] for r in copies(b, "acct_b") if r["masterTicket"] == first] == ["released"]
    second = master_open(b)  # a fresh start: new master trades are copied again
    wait_until(lambda: any(r["masterTicket"] == second and r["status"] == "open" for r in copies(b, "acct_b")))
    logs = [e["message"] for e in b.get("/logs").json()["logs"]]
    assert any("Trading switched OFF on" in m and "1 copies are no longer followed" in m for m in logs)


def test_each_master_trade_logs_every_client_latency(copier):
    b = copier
    ticket = master_open(b)
    wait_until(lambda: len(copies(b)) == 2 and all(r["status"] == "open" for r in copies(b)))
    master_close(b)
    wait_until(lambda: len(copies(b)) == 2 and all(r["status"] == "closed" for r in copies(b)))

    def line(words):
        logs = [e["message"] for e in b.get("/logs").json()["logs"]]
        return next((m for m in logs if m.startswith(f"Master trade #{ticket} {words}")), None)
    wait_until(lambda: line("copied to") and line("closed on"))
    for words in ("copied to", "closed on"):
        text = line(words)
        assert f"{words} 2 of 2 clients - fastest" in text and " ms" in text, text
        assert "ACCT_B" in text and "ACCT_C" in text, text  # account names


def test_a_client_that_fails_is_named_in_the_latency_line(copier):
    b = copier
    b.patch("/accounts/acct_c", {"tradingOn": False})
    ticket = master_open(b)
    wait_until(lambda: copies(b, "acct_b") and copies(b, "acct_b")[0]["status"] == "open")
    wait_until(lambda: any(e["message"].startswith(f"Master trade #{ticket} copied to 1 of 2 clients")
                           for e in b.get("/logs?level=warning").json()["logs"]))
    text = next(e["message"] for e in b.get("/logs?level=warning").json()["logs"]
                if e["message"].startswith(f"Master trade #{ticket}"))
    assert "not copied (TRADING_OFF)" in text, text
