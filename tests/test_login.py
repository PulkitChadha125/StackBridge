"""Web UI password login."""

import pytest
from fastapi.testclient import TestClient

from stakbridge.security import hash_password, verify_password

UI = {"X-StakBridge": "1"}
PASSWORD = "correct horse 42"


@pytest.fixture
def local(bridge):
    """A browser on the PC running StakBridge."""
    return TestClient(bridge.client.app, client=("127.0.0.1", 50001))


@pytest.fixture
def remote(bridge):
    """A browser on another computer."""
    return TestClient(bridge.client.app, client=("192.168.1.50", 50002))


def setup_password(local):
    res = local.post("/v1/session/setup", json={"password": PASSWORD}, headers=UI)
    assert res.status_code == 200, res.text
    return res


def test_first_password_can_only_be_created_locally(local, remote):
    assert remote.get("/v1/session").json() == {"licensed": False, "passwordSet": False, "loggedIn": False,
                                                "setupAllowedHere": False}
    res = remote.post("/v1/session/setup", json={"password": PASSWORD}, headers=UI)
    assert res.status_code == 403 and res.json()["error"]["code"] == "SETUP_LOCAL_ONLY"

    # A request forwarded by a proxy looks local but must not count as local.
    proxied = local.post("/v1/session/setup", json={"password": PASSWORD}, headers={**UI, "X-Forwarded-For": "8.8.8.8"})
    assert proxied.status_code == 403

    assert local.get("/v1/session").json()["setupAllowedHere"] is True
    setup_password(local)
    assert local.get("/v1/session").json() == {"licensed": False, "passwordSet": True, "loggedIn": True, "setupAllowedHere": True}

    again = local.post("/v1/session/setup", json={"password": "another password"}, headers=UI)
    assert again.status_code == 409


def test_short_password_is_refused(local):
    res = local.post("/v1/session/setup", json={"password": "short"}, headers=UI)
    assert res.status_code == 400


def test_sign_in_and_use_the_app(local, remote):
    setup_password(local)

    assert remote.get("/v1/accounts").status_code == 401
    wrong = remote.post("/v1/session", json={"password": "nope"}, headers=UI)
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "WRONG_PASSWORD"

    assert remote.post("/v1/session", json={"password": PASSWORD}, headers=UI).status_code == 200
    cookie = remote.cookies.get("stakbridge_session")
    assert cookie
    assert remote.get("/v1/accounts").status_code == 200
    assert remote.post("/v1/kill", headers=UI).json() == {"killSwitch": True}


def test_session_cookie_is_protected(local):
    res = setup_password(local)
    cookie_header = res.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=strict" in cookie_header

    # Without the UI header a state-changing request is refused: another website cannot send it.
    blocked = local.post("/v1/kill")
    assert blocked.status_code == 403
    assert local.get("/v1/health").json()["killSwitch"] is False


def test_sign_out_ends_the_session(local):
    setup_password(local)
    assert local.get("/v1/accounts").status_code == 200
    assert local.delete("/v1/session", headers=UI).status_code == 200
    assert local.get("/v1/accounts").status_code == 401


def test_too_many_wrong_passwords_are_slowed_down(local, remote):
    setup_password(local)
    codes = [remote.post("/v1/session", json={"password": "guess"}, headers=UI).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_reset_password_command_signs_everyone_out(bridge, local, monkeypatch):
    from stakbridge import __main__ as cli

    setup_password(local)
    answers = iter(["short", "new password 1", "new password 1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    cli.set_password(bridge.client.app.state.db)

    assert local.get("/v1/accounts").status_code == 401  # old session gone
    assert local.post("/v1/session", json={"password": PASSWORD}, headers=UI).status_code == 401
    assert local.post("/v1/session", json={"password": "new password 1"}, headers=UI).status_code == 200


def test_password_is_stored_hashed():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong", stored)
    assert stored != hash_password(PASSWORD)  # random salt each time
