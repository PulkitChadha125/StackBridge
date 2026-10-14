"""Trade copier: acct_a is the master, acct_b (x1, suffix .m) and acct_c (x2) are clients.

Trades on the master are made through a strategy that only acct_a receives, which is how
a person or EA trading the master terminal looks to the copier.
"""

import pytest
from fastapi.testclient import TestClient

from stakbridge.api import create_app
from stakbridge.config import load_settings
from stakbridge.db import Database
from stakbridge.security import hash_token

from conftest import TOKEN, Bridge, wait_until


@pytest.fixture
def make_bridge(tmp_path):
    """Starts StakBridge on a data folder. Can be called again to test a restart."""
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0,
                             copy_retry_interval=0.3)
    Database(settings.db_path).add_token(hash_token(TOKEN), "tests")
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
    b.add_strategy("master", max_volume=5, accounts=["acct_a"])
    b.add_strategy("manual_b", max_volume=5, accounts=["acct_b"])
    set_copier(b, active=True)
    wait_until(lambda: b.get("/copier").json()["watching"])
    return b


def set_copier(b, active=True, master="acct_a", clients=(("acct_b", 1.0), ("acct_c", 2.0))):
    res = b.put("/copier", {"masterId": master, "active": active,
                            "clients": [{"accountId": a, "multiplier": m} for a, m in clients]})
    assert res.status_code == 200, res.text
    return res.json()


_ids = iter(range(1, 10_000))


def master_open(b, **fields):
    res = b.trade(f"m-{next(_ids)}", strategy="master", **fields)
    assert res.json()["results"][0]["status"] == "filled", res.text
    return res.json()["results"][0]["ticket"]


def master_close(b):
    res = b.post("/trade", {"requestId": f"m-{next(_ids)}", "strategy": "master", "action": "close"})
    assert res.json()["results"][0]["status"] == "closed", res.text


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
    master = b.client.app.state.workers.get("acct_a")
    assert master.send("modify_position", ticket=ticket, sl=2310.0, tp=2360.0).result(5)["status"] == "modified"
    wait_until(lambda: all((p["sl"], p["tp"]) == (2310.0, 2360.0) for a in ("acct_b", "acct_c") for p in b.positions(a)))


def test_manual_client_trades_are_left_alone(copier):
    b = copier
    b.trade("own-1", strategy="manual_b", volume=0.5)  # the client's own position
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 2)
    master_close(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    assert b.positions("acct_b")[0]["volume"] == 0.5


def test_paused_copier_and_kill_switch_block_opens_but_not_closes(copier):
    b = copier
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)

    set_copier(b, active=False)
    master_open(b)
    wait_until(lambda: sum(r["reason"] == "COPIER_PAUSED" for r in copies(b)) == 2)
    assert len(b.positions("acct_b")) == 1

    b.post("/kill")
    set_copier(b, active=True)
    master_close(b)  # closing is allowed while stopped
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])
    b.delete("/kill")


def test_trading_off_client_is_skipped(copier):
    b = copier
    b.patch("/accounts/acct_c", {"tradingOn": False})
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    wait_until(lambda: copies(b, "acct_c") and copies(b, "acct_c")[0]["reason"] == "TRADING_OFF")
    assert b.positions("acct_c") == []


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
    b.add_strategy("master", max_volume=5, accounts=["acct_a"])
    master_open(b)

    set_copier(b, clients=(("acct_b", 1.0),))
    wait_until(lambda: b.get("/copier").json()["watching"])
    master_open(b)
    wait_until(lambda: len(b.positions("acct_b")) == 1)
    wait_until(lambda: len(copies(b)) == 1 and copies(b)[0]["status"] == "open")
    assert len(b.positions("acct_a")) == 2 and len(b.positions("acct_b")) == 1


def test_master_cannot_be_a_client(copier):
    res = copier.put("/copier", {"masterId": "acct_a", "active": True,
                                 "clients": [{"accountId": "acct_a", "multiplier": 1}]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "MASTER_IS_CLIENT"


def test_removing_an_account_drops_it_from_the_copier(copier):
    copier.delete("/accounts/acct_c")
    config = copier.get("/copier").json()
    assert config["masterId"] == "acct_a" and [c["accountId"] for c in config["clients"]] == ["acct_b"]
    copier.delete("/accounts/acct_a")
    config = copier.get("/copier").json()
    assert config["masterId"] is None and config["active"] is False


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
    res = b.put("/copier", {"masterId": "acct_a", "active": True, "clients": [
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
    res = copier.put("/copier", {"masterId": "acct_a", "active": True, "clients": [
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
