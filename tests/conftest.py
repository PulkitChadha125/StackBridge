import time

import pytest
from fastapi.testclient import TestClient

from stakbridge.api import create_app
from stakbridge.config import load_settings
from stakbridge.db import Database
from stakbridge.security import hash_token

TOKEN = "sb_test_token"


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
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

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

    def add_strategy(self, tag, max_volume=1.0, accounts=()):
        assert self.post("/strategies", {"tag": tag, "maxVolume": max_volume}).status_code == 201
        if accounts:
            assert self.put(f"/strategies/{tag}/accounts", {"accountIds": list(accounts)}).status_code == 200

    def positions(self, account_id):
        res = self.get(f"/accounts/{account_id}/positions")
        assert res.status_code == 200, res.text
        return res.json()["positions"]

    def trade(self, request_id, strategy="gold_scalper", **fields):
        body = {"requestId": request_id, "strategy": strategy, "action": "open", "symbol": "XAUUSD",
                "side": "buy", "type": "market", "volume": 0.10}
        body.update(fields)
        return self.post("/trade", {k: v for k, v in body.items() if v is not None})

    def set_offline(self, account_id, offline=True):
        handle = self.client.app.state.workers.get(account_id)
        assert handle.send("set_offline", offline=offline).result(timeout=5)["status"] == "ok"


@pytest.fixture
def bridge(tmp_path):
    settings = load_settings(data_dir=tmp_path, backend="fake", status_interval=0.2, trade_timeout=5.0)
    Database(settings.db_path).add_token(hash_token(TOKEN), "tests")
    with TestClient(create_app(settings)) as client:
        yield Bridge(client)


@pytest.fixture
def three_accounts(bridge):
    """Acct A and B on gold_scalper, Acct C only on swing_eur, all connected with trading on."""
    bridge.add_account("acct_a", 5012345)
    bridge.add_account("acct_b", 5012346, suffix=".m")
    bridge.add_account("acct_c", 7788120)
    bridge.wait_connected("acct_a", "acct_b", "acct_c")
    bridge.add_strategy("gold_scalper", max_volume=1.0, accounts=["acct_a", "acct_b"])
    bridge.add_strategy("swing_eur", max_volume=2.0, accounts=["acct_b", "acct_c"])
    return bridge
