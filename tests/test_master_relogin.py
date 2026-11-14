"""The master's MetaTrader logs in again (13 Nov 2026 incident): for a moment it shows no trades at all.

That must never close the client copies. A master trade counts as closed only once MetaTrader's history has the
close. acct_a is the master, acct_b and acct_c are clients (simulated broker; "glitch" makes the master show no
trades while still looking connected, the worst case).
"""

import time

from fastapi.testclient import TestClient

from stakbridge.api import create_app
from stakbridge.config import load_settings

from conftest import Bridge, wait_until
from test_copier import copier, copies, make_bridge, master_open, set_copier  # noqa: F401 (fixtures)


def glitch(b, on=True):
    assert b.send("acct_a", "glitch", on=on)["status"] == "ok"


def warnings(b):
    return [e["message"] for e in b.get("/logs?level=warning").json()["logs"]]


def nothing_kept(b):
    """The copier holds no "unsure" master trades any more (memory used only while one is open to question)."""
    engine = b.client.app.state.copier
    return not engine._unsure and not engine._log_when_gone


def all_open(b, count):
    rows = copies(b)
    return len(rows) == count and all(r["status"] == "open" for r in rows)


def test_master_showing_no_trades_for_a_moment_closes_nothing(copier):
    b = copier
    first, second = master_open(b), master_open(b)
    wait_until(lambda: all_open(b, 4))
    glitch(b)
    time.sleep(2.0)  # many master reads, retries and periodic checks
    assert len(b.positions("acct_b")) == 2 and len(b.positions("acct_c")) == 2
    assert all_open(b, 4)
    wait_until(lambda: any(f"master trade(s) {first}, {second} no longer shown" in m for m in warnings(b)))
    glitch(b, on=False)
    time.sleep(0.5)
    assert all_open(b, 4)
    # The master still works as normal afterwards: a real close is followed.
    b.close_by_hand("acct_a", second)
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1)
    assert {r["status"] for r in copies(b) if r["masterTicket"] == first} == {"open"}


def test_a_real_close_during_the_glitch_is_still_followed(copier):
    b = copier
    first, second = master_open(b), master_open(b)
    wait_until(lambda: all_open(b, 4))
    glitch(b)
    time.sleep(0.5)
    b.close_by_hand("acct_a", first)  # in the history: confirmed even while nothing is shown
    wait_until(lambda: len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1)
    wait_until(lambda: {r["status"] for r in copies(b) if r["masterTicket"] == first} == {"closed"})
    assert {r["status"] for r in copies(b) if r["masterTicket"] == second} == {"open"}
    glitch(b, on=False)


def test_master_reconnecting_without_its_trades_keeps_the_copies(copier):
    b = copier
    ticket = master_open(b)
    wait_until(lambda: all_open(b, 2))
    glitch(b)
    b.set_offline("acct_a")
    wait_until(lambda: b.status("acct_a") != "connected")
    b.set_offline("acct_a", offline=False)  # connects again, first list of trades is empty
    wait_until(lambda: b.status("acct_a") == "connected")
    wait_until(lambda: any("connected again without 1 trade(s)" in m for m in warnings(b)))
    time.sleep(2.0)
    assert len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1
    assert all_open(b, 2)
    glitch(b, on=False)  # MetaTrader has loaded the account: the trade is there again
    time.sleep(0.5)
    assert all_open(b, 2)
    assert not any("too old to copy" in m for m in warnings(b))
    wait_until(lambda: nothing_kept(b))  # the trade was found again: nothing about it stays in memory
    b.close_by_hand("acct_a", ticket)
    wait_until(lambda: b.positions("acct_b") == [] and b.positions("acct_c") == [])
    wait_until(lambda: all(r["status"] == "closed" for r in copies(b)))


def test_master_trade_closed_while_disconnected_is_closed_after_the_reconnect(copier):
    b = copier
    kept, closed = master_open(b), master_open(b)
    wait_until(lambda: all_open(b, 4))
    glitch(b)
    b.close_by_hand("acct_a", closed)
    b.set_offline("acct_a")
    wait_until(lambda: b.status("acct_a") != "connected")
    glitch(b, on=False)
    b.set_offline("acct_a", offline=False)
    # The baseline lacks `closed`; the master's history confirms it, so its copies are closed.
    wait_until(lambda: all(r["status"] == "closed" for r in copies(b) if r["masterTicket"] == closed))
    assert {r["status"] for r in copies(b) if r["masterTicket"] == kept} == {"open"}
    assert len(b.positions("acct_b")) == 1 and len(b.positions("acct_c")) == 1
    wait_until(lambda: nothing_kept(b))


def test_trade_copied_before_is_not_marked_too_old_again(tmp_path):
    """A master trade found after a reconnect that was copied before keeps its copy rows (they used to be
    overwritten with "skipped TOO_OLD_AFTER_RESTART")."""
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0,
                             copy_retry_interval=0.3, copy_check_interval=0.5, copy_max_age=-1)
    with TestClient(create_app(settings)) as client:
        b = Bridge(client)
        b.add_account("acct_a", 5012345)
        b.add_account("acct_b", 5012346)
        b.add_account("acct_c", 7788120)
        b.wait_connected("acct_a", "acct_b", "acct_c")
        set_copier(b)
        wait_until(lambda: b.get("/copier").json()["watching"])
        ticket = master_open(b)
        wait_until(lambda: all_open(b, 2))
        for client_id in ("acct_b", "acct_c"):  # copies let go: no longer live, so not loaded on a master change
            b.patch(f"/accounts/{client_id}", {"tradingOn": False})
        wait_until(lambda: all(r["status"] == "released" for r in copies(b)))
        db = client.app.state.db
        db.set_seen_tickets("acct_a", [])  # as if the master had shown no trades at its last check
        set_copier(b, master="acct_b", clients=())
        db.set_seen_tickets("acct_a", [])
        set_copier(b)  # back to acct_a: `ticket` is now "opened while not watching", and too old
        wait_until(lambda: any(f"master trade {ticket} was copied before" in m for m in warnings(b)))
        assert {r["status"] for r in copies(b) if r["masterTicket"] == ticket} == {"released"}
        assert not any(f"master position {ticket} opened while not watching" in m for m in warnings(b))
