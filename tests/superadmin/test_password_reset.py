"""Forgot Password by emailed code - SuperAdmin panel pages and the Admin
API (password_reset.py, superadmin_web.py, auth_api.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from superadmin_panel import config, db, email_notifications, password_reset, repositories as repo, security


@pytest.fixture
def sent_codes(monkeypatch):
    """SMTP "configured", and every code captured instead of emailed."""
    codes = {}

    def _capture(*, email, name, code, where):
        codes[email.lower()] = code

    monkeypatch.setattr(config, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(email_notifications, "send_password_reset_code", _capture)
    return codes


def _make_admin(sa_app, username="abc", email="abc@example.com", password="Old-Pass-1"):
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.create_admin(
            conn,
            name="ABC",
            username=username,
            password_hash=security.hash_password(password),
            expiry_date="2030-01-01",
            allowed_installations=1,
            email=email,
            must_change_password=False,
        )
        device = repo.register_or_touch_device(conn, admin["id"], f"fp-{username}")
        session_row = repo.create_session(
            conn, admin_id=admin["id"], device_id=device["id"], token_hash=f"th-{username}", ttl_seconds=3600
        )
    return admin, session_row


def _admin(sa_app, admin_id):
    with sa_app.app_context():
        return repo.get_admin(db.get_db(), admin_id)


# --------------------------------------------------------------------------
# Admin - JSON API used by the Admin app
# --------------------------------------------------------------------------


def _admin_reset(client, sent_codes, email="abc@example.com", new="Brand-New-1", confirm=None):
    resp = client.post("/api/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.get_json()
    resp = client.post("/api/auth/forgot-password/verify", json={"email": email, "code": sent_codes[email.lower()]})
    assert resp.status_code == 200, resp.get_json()
    token = resp.get_json()["reset_token"]
    return client.post(
        "/api/auth/forgot-password/reset",
        json={"email": email, "reset_token": token, "new_password": new, "confirm_password": confirm or new},
    )


def test_admin_full_reset_sets_password_and_logs_out(client, sa_app, sent_codes):
    admin, session_row = _make_admin(sa_app)

    resp = _admin_reset(client, sent_codes, email="ABC@example.com")  # case-insensitive
    assert resp.status_code == 200

    updated = _admin(sa_app, admin["id"])
    assert security.verify_password("Brand-New-1", updated["password_hash"])
    assert updated["must_change_password"] == 0
    with sa_app.app_context():
        assert not repo.is_session_valid(repo.get_session(db.get_db(), session_row["id"]))

    # New password works for a real login.
    login = client.post(
        "/api/auth/login", json={"username": "abc", "password": "Brand-New-1", "device_fingerprint": "fp-abc"}
    )
    assert login.status_code == 200


def test_unknown_email_gets_same_answer_and_no_email(client, sa_app, sent_codes):
    _make_admin(sa_app)
    resp = client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert resp.get_json()["message"] == password_reset.SENT_MESSAGE
    assert sent_codes == {}


def test_wrong_code_counts_down_then_locks(client, sa_app, sent_codes, monkeypatch):
    monkeypatch.setattr(config, "PASSWORD_RESET_CODE_MAX_ATTEMPTS", 2)
    admin, _ = _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    real = sent_codes["abc@example.com"]
    wrong = "000000" if real != "000000" else "111111"

    resp = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": wrong})
    assert resp.status_code == 400
    assert "1 try left" in resp.get_json()["message"]
    assert resp.get_json()["retryable"] is True

    resp = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": wrong})
    assert resp.get_json()["message"] == password_reset.TOO_MANY_CODES_MESSAGE
    assert resp.get_json()["retryable"] is False

    # Even the real code is dead now.
    resp = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": real})
    assert resp.status_code == 400
    assert security.verify_password("Old-Pass-1", _admin(sa_app, admin["id"])["password_hash"])


def test_expired_code_rejected(client, sa_app, sent_codes):
    _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    with sa_app.app_context():
        conn = db.get_db()
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE password_reset_codes SET expires_at = ?", (past,))
        conn.commit()
    resp = client.post(
        "/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": sent_codes["abc@example.com"]}
    )
    assert resp.get_json()["message"] == password_reset.CODE_EXPIRED_MESSAGE


def test_code_and_token_are_single_use(client, sa_app, sent_codes):
    _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    code = sent_codes["abc@example.com"]
    token = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": code}).get_json()[
        "reset_token"
    ]
    # Code can't be confirmed twice.
    again = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": code})
    assert again.status_code == 400

    body = {"email": "abc@example.com", "reset_token": token, "new_password": "Brand-New-1", "confirm_password": "Brand-New-1"}
    assert client.post("/api/auth/forgot-password/reset", json=body).status_code == 200
    assert client.post("/api/auth/forgot-password/reset", json=body).status_code == 400


def test_bad_token_rejected(client, sa_app, sent_codes):
    admin, _ = _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    client.post(
        "/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": sent_codes["abc@example.com"]}
    )
    resp = client.post(
        "/api/auth/forgot-password/reset",
        json={"email": "abc@example.com", "reset_token": "forged", "new_password": "Brand-New-1", "confirm_password": "Brand-New-1"},
    )
    assert resp.status_code == 400
    assert security.verify_password("Old-Pass-1", _admin(sa_app, admin["id"])["password_hash"])


def test_short_or_mismatched_password_keeps_step_open(client, sa_app, sent_codes):
    admin, _ = _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    token = client.post(
        "/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": sent_codes["abc@example.com"]}
    ).get_json()["reset_token"]

    def _reset(new, confirm):
        return client.post(
            "/api/auth/forgot-password/reset",
            json={"email": "abc@example.com", "reset_token": token, "new_password": new, "confirm_password": confirm},
        )

    short = _reset("short", "short")
    assert "at least 8" in short.get_json()["message"] and short.get_json()["retryable"] is True
    mismatch = _reset("Brand-New-1", "Brand-New-2")
    assert "do not match" in mismatch.get_json()["message"]
    # Same token still works after fixing the typo.
    assert _reset("Brand-New-1", "Brand-New-1").status_code == 200
    assert security.verify_password("Brand-New-1", _admin(sa_app, admin["id"])["password_hash"])


def test_shared_email_refused(client, sa_app, sent_codes):
    _make_admin(sa_app, username="one", email="shared@example.com")
    _make_admin(sa_app, username="two", email="shared@example.com")
    resp = client.post("/api/auth/forgot-password", json={"email": "shared@example.com"})
    assert resp.status_code == 400
    assert "More than one account" in resp.get_json()["message"]
    assert sent_codes == {}


def test_smtp_not_configured(client, sa_app, monkeypatch):
    monkeypatch.setattr(config, "smtp_is_configured", lambda: False)
    _make_admin(sa_app)
    resp = client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    assert resp.status_code == 400
    assert "Email is not set up" in resp.get_json()["message"]


def test_resend_cooldown_does_not_send_again(client, sa_app, sent_codes):
    _make_admin(sa_app)
    client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    first = sent_codes["abc@example.com"]
    sent_codes.clear()
    resp = client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    assert resp.status_code == 200
    assert resp.get_json()["resend_wait"] > 0
    assert sent_codes == {}
    # The first code still works.
    ok = client.post("/api/auth/forgot-password/verify", json={"email": "abc@example.com", "code": first})
    assert ok.status_code == 200


def test_send_failure_reports_error_and_leaves_no_code(client, sa_app, monkeypatch):
    monkeypatch.setattr(config, "smtp_is_configured", lambda: True)

    def _boom(**kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_notifications, "send_password_reset_code", _boom)
    _make_admin(sa_app)
    resp = client.post("/api/auth/forgot-password", json={"email": "abc@example.com"})
    assert resp.status_code == 400
    assert "couldn't send" in resp.get_json()["message"]
    with sa_app.app_context():
        assert repo.get_password_reset_code(db.get_db(), "admin", "abc@example.com") is None


def test_admin_codes_cannot_reset_superadmin(client, sa_app, sent_codes):
    """An Admin and the SuperAdmin sharing an email keep separate requests."""
    config.save_superadmin_credentials("owner", security.hash_password("Owner-Old-1"))
    with sa_app.app_context():
        repo.set_setting(db.get_db(), repo.SETTING_OWNER_NOTIFICATION_EMAIL, "abc@example.com")
    _make_admin(sa_app)
    assert _admin_reset(client, sent_codes).status_code == 200
    assert security.verify_password("Owner-Old-1", config.load_superadmin_credentials()[1])


# --------------------------------------------------------------------------
# SuperAdmin - panel pages
# --------------------------------------------------------------------------


def _setup_owner(sa_app, email="owner@example.com"):
    config.save_superadmin_credentials("owner", security.hash_password("Owner-Old-1"))
    with sa_app.app_context():
        repo.set_setting(db.get_db(), repo.SETTING_OWNER_NOTIFICATION_EMAIL, email)


def test_login_page_links_to_forgot_password(client):
    assert b"/forgot-password" in client.get("/login").data


def test_superadmin_full_reset(client, sa_app, sent_codes):
    _setup_owner(sa_app)

    resp = client.post("/forgot-password", data={"email": "Owner@Example.com"})
    assert b"Enter Your Code" in resp.data
    resp = client.post("/forgot-password/verify", data={"code": sent_codes["owner@example.com"]})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/forgot-password/new")

    page = client.get("/forgot-password/new")
    assert b"New Password" in page.data and b"Confirm Password" in page.data

    resp = client.post(
        "/forgot-password/new",
        data={"new_password": "Owner-New-1", "confirm_password": "Owner-New-1"},
        follow_redirects=True,
    )
    assert b"Password changed" in resp.data

    username, password_hash = config.load_superadmin_credentials()
    assert username == "owner"
    assert security.verify_password("Owner-New-1", password_hash)

    login = client.post("/login", data={"username": "owner", "password": "Owner-New-1"})
    assert login.status_code == 302


def test_superadmin_wrong_email_sends_nothing(client, sa_app, sent_codes):
    _setup_owner(sa_app)
    resp = client.post("/forgot-password", data={"email": "someone@example.com"})
    assert b"Enter Your Code" in resp.data  # same screen - doesn't reveal the right email
    assert sent_codes == {}


def test_superadmin_new_password_page_needs_confirmed_code(client, sa_app, sent_codes):
    _setup_owner(sa_app)
    resp = client.get("/forgot-password/new")
    assert resp.status_code == 302
    client.post("/forgot-password", data={"email": "owner@example.com"})
    resp = client.get("/forgot-password/new")  # code sent but not confirmed yet
    assert resp.status_code == 302


def test_superadmin_mismatch_keeps_old_password(client, sa_app, sent_codes):
    _setup_owner(sa_app)
    client.post("/forgot-password", data={"email": "owner@example.com"})
    client.post("/forgot-password/verify", data={"code": sent_codes["owner@example.com"]})
    resp = client.post("/forgot-password/new", data={"new_password": "Owner-New-1", "confirm_password": "Owner-New-2"})
    assert b"do not match" in resp.data
    assert security.verify_password("Owner-Old-1", config.load_superadmin_credentials()[1])


def test_superadmin_env_credentials_cannot_be_rewritten(client, sa_app, sent_codes, monkeypatch):
    monkeypatch.setenv("SUPERADMIN_USERNAME", "owner")
    monkeypatch.setenv("SUPERADMIN_PASSWORD_HASH", security.hash_password("Owner-Old-1"))
    with sa_app.app_context():
        repo.set_setting(db.get_db(), repo.SETTING_OWNER_NOTIFICATION_EMAIL, "owner@example.com")
    client.post("/forgot-password", data={"email": "owner@example.com"})
    client.post("/forgot-password/verify", data={"code": sent_codes["owner@example.com"]})
    resp = client.post("/forgot-password/new", data={"new_password": "Owner-New-1", "confirm_password": "Owner-New-1"})
    assert b"environment variables" in resp.data
    assert not config.CONFIG_PATH.exists()
