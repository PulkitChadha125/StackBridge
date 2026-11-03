"""The Store saves the copier's changes on its own thread (see stakbridge/store.py)."""

import sqlite3
import time

from stakbridge.db import Database
from stakbridge.store import Store

from conftest import wait_until


def _row(ticket, status, client="acct_b"):
    return {"master_id": "acct_a", "master_ticket": ticket, "client_id": client, "symbol": "XAUUSD", "side": "buy",
            "master_volume": 0.1, "volume": 0.1, "sl": 0.0, "tp": 0.0, "status": status, "reason": None,
            "created_at": "2026-10-31T10:00:00+00:00"}


def test_the_latest_version_of_a_row_is_saved_and_later_changes_to_the_row_are_not_lost(tmp_path):
    db = Database(tmp_path / "s.db")
    store = Store(db)
    row = _row(1, "pending")
    store.save_copies([row])
    row["status"] = "open"  # the copier changes its own row afterwards: the queued copy must not change with it
    assert store.find_copy("acct_a", "acct_b", master_ticket=1)["status"] == "pending"
    store.save_copies([row])
    store.start()
    assert store.wait_idle(5)
    assert db.find_copy("acct_a", "acct_b", master_ticket=1)["status"] == "open"
    store.stop()


def test_calls_run_in_order_and_a_bug_does_not_block_the_ones_after_it(tmp_path):
    store = Store(Database(tmp_path / "s.db"))
    store.start()
    done = []

    def broken():
        raise ValueError("a bug")
    store.call(done.append, 1)
    store.call(broken, then=done.append)  # gets None
    store.call(done.append, 2)
    assert store.wait_idle(5)
    assert done == [1, None, 2]
    store.stop()


def test_disk_errors_are_kept_and_tried_again_until_they_work(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    store = Store(db)
    monkeypatch.setattr("stakbridge.store.RETRY_MIN", 0.05)
    fails = {"left": 3}
    original = Database.save_copies

    def flaky(self, *a, **k):
        if fails["left"]:
            fails["left"] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, *a, **k)
    monkeypatch.setattr(Database, "save_copies", flaky)
    store.start()
    store.save_copies([_row(7, "open")])
    wait_until(lambda: store.problem is not None)
    assert store.problem.startswith("SAVE_FAILED")
    wait_until(lambda: db.find_copy("acct_a", "acct_b", master_ticket=7) is not None)
    wait_until(lambda: store.problem is None)
    store.stop()


def test_stop_saves_what_is_still_waiting(tmp_path):
    db = Database(tmp_path / "s.db")
    store = Store(db)
    store.start()
    store.call(time.sleep, 0.3)
    store.save_copies([_row(9, "closed")])
    store.stop()
    assert db.find_copy("acct_a", "acct_b", master_ticket=9)["status"] == "closed"


def test_a_row_that_cannot_be_saved_is_dropped_without_blocking_the_others(tmp_path):
    db = Database(tmp_path / "s.db")
    store = Store(db)
    store.start()
    bad = _row(1, "open")
    del bad["master_volume"]  # NOT NULL in the table: a bug, not the disk
    store.save_copies([bad, _row(2, "open")])
    assert store.wait_idle(5)
    assert db.find_copy("acct_a", "acct_b", master_ticket=2) is not None
    assert db.find_copy("acct_a", "acct_b", master_ticket=1) is None
    store.save_copies([_row(3, "open")])  # the thread is still alive
    assert store.wait_idle(5) and db.find_copy("acct_a", "acct_b", master_ticket=3) is not None
    store.stop()
