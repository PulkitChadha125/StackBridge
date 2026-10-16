import itertools
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from stakbridge.api import create_app
from stakbridge.config import load_settings
from stakbridge.security import hash_token

SESSION = "test-session"  # a signed-in browser session, stored straight into the database
UI = {"X-StakBridge": "1"}
_comments = itertools.count(1)


def wait_until(condition, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    raise AssertionError("condition not met in time")


class Bridge:
    """Small helper around the TestClient so tests read like the acceptance table."""

    def __init__(self, client: TestClient):
        self.client = client
        self.headers = UI
        client.app.state.db.add_session(hash_token(SESSION), datetime.now(timezone.utc) + timedelta(days=1))
        client.cookies.set("stakbridge_session", SESSION)

    def get(self, path, **kw):
        return self.client.get(f"/v1{path}", headers=self.headers, **kw)

    def post(self, path, json=None):
        return self.client.post(f"/v1{path}", headers=self.headers, json=json)

    def patch(self, path, json):
        return self.client.patch(f"/v1{path}", headers=self.headers, json=json)

    def put(self, path, json):
        return self.client.put(f"/v1{path}", headers=self.headers, json=json)

    def delete(self, path):
        return self.client.delete(f"/v1{path}", headers=self.headers)

    def add_account(self, account_id, login, suffix="", trading_on=True):
        res = self.post("/accounts", {
            "id": account_id, "name": account_id.upper(), "login": login,
            "server": "Demo-Server-1", "password": "secret-pass", "symbolSuffix": suffix,
        })
        assert res.status_code == 201, res.text
        if trading_on:
            assert self.patch(f"/accounts/{account_id}", {"tradingOn": True}).status_code == 200

    def status(self, account_id):
        return next(a for a in self.get("/accounts").json()["accounts"] if a["id"] == account_id)["status"]

    def wait_connected(self, *account_ids):
        wait_until(lambda: all(self.status(a) == "connected" for a in account_ids))


    def positions(self, account_id):
        res = self.get(f"/accounts/{account_id}/positions")
        assert res.status_code == 200, res.text
        return res.json()["positions"]

    def send(self, account_id, cmd, **fields):
        """Sends a command straight to an account's worker, like a person trading in its MetaTrader."""
        return self.client.app.state.workers.get(account_id).send(cmd, **fields).result(timeout=5)

    def open_by_hand(self, account_id, symbol="XAUUSD", side="buy", volume=0.1, sl=None, tp=None, magic=0):
        """Opens a market position on the account as if placed by hand in its MetaTrader. Returns the ticket."""
        result = self.send(account_id, "open", order={
            "symbol": symbol, "side": side, "type": "market", "volume": volume, "sl": sl, "tp": tp,
            "magic": magic, "comment": f"hand {next(_comments)}"})
        assert result["status"] == "filled", result
        return result["ticket"]

    def close_by_hand(self, account_id, ticket):
        assert self.send(account_id, "close_position", ticket=ticket)["status"] == "closed"

    def set_offline(self, account_id, offline=True):
        handle = self.client.app.state.workers.get(account_id)
        assert handle.send("set_offline", offline=offline).result(timeout=5)["status"] == "ok"


@pytest.fixture
def bridge(tmp_path):
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0)
    with TestClient(create_app(settings)) as client:
        yield Bridge(client)
