"""Per-Admin "max client accounts" limit, SuperAdmin side: stored per Admin,
editable in the panel (a number or Unlimited), sent to the app in every
login/refresh answer and inside the signed license cache, and shown on the
dashboard against the client count the app reports."""

import base64
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization

from superadmin_panel import config, db, license_signing, repositories as repo, security
from superadmin_panel.superadmin_web import time_ago


def _create_admin(sa_app, **overrides):
    defaults = dict(
        name="ABC Trading",
        username="abc_admin",
        password_hash=security.hash_password("Temp-Pass-1"),
        expiry_date=(date.today() + timedelta(days=30)).isoformat(),
        allowed_installations=1,
    )
    defaults.update(overrides)
    with sa_app.app_context():
        return repo.create_admin(db.get_db(), **defaults)


def _get_admin(sa_app, admin_id):
    with sa_app.app_context():
        return repo.get_admin(db.get_db(), admin_id)


def _login_api(client, username="abc_admin", fingerprint="device-A"):
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Temp-Pass-1", "device_fingerprint": fingerprint},
    )
    assert resp.status_code == 200
    return resp.get_json()


def _login_panel(client):
    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})


def _verified_cache_payload(signed: dict) -> dict:
    public_key = serialization.load_pem_public_key(license_signing.get_public_key_pem())
    public_key.verify(base64.b64decode(signed["signature"]), license_signing.canonical_json(signed["payload"]))
    return signed["payload"]


def _create_form(**overrides):
    form = {
        "name": "ABC Trading",
        "username": "abc_admin",
        "expiry_date": "2030-09-30",
        "allowed_installations": "1",
        "max_client_accounts": "5",
        "notes": "",
    }
    form.update(overrides)
    return form


def _edit_form(admin, **overrides):
    form = {
        "name": admin["name"],
        "expiry_date": admin["expiry_date"],
        "allowed_installations": str(admin["allowed_installations"]),
        "max_client_accounts": "5",
        "notes": "",
        "email": "",
    }
    form.update(overrides)
    return form


# --- storage ------------------------------------------------------------------


def test_new_admin_defaults_to_five_client_accounts(sa_app):
    admin = _create_admin(sa_app)
    assert admin["max_client_accounts"] == 5


def test_unlimited_is_stored_as_null(sa_app):
    admin = _create_admin(sa_app, max_client_accounts=None)
    assert admin["max_client_accounts"] is None


def test_existing_admins_get_five_when_the_column_is_added(tmp_path):
    """A server DB from before client limits: upgrading must give every
    existing Admin a limit of 5, not unlimited and not 0."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE admins (id TEXT PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, must_change_password INTEGER NOT NULL DEFAULT 1, "
        "expiry_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
        "allowed_installations INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, notes TEXT)"
    )
    old.execute(
        "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at) "
        "VALUES ('a1', 'Old Admin', 'old', 'hash', '2030-01-01', '2026-01-01')"
    )
    old.commit()
    old.close()

    db.init_db(path)

    conn = db.get_connection(path)
    try:
        row = conn.execute("SELECT max_client_accounts FROM admins WHERE id = 'a1'").fetchone()
    finally:
        conn.close()
    assert row["max_client_accounts"] == 5


# --- auth API -----------------------------------------------------------------


def test_login_sends_the_limit_and_signs_it_into_the_license_cache(client, sa_app):
    _create_admin(sa_app, max_client_accounts=12)

    payload = _login_api(client)

    assert payload["max_client_accounts"] == 12
    assert _verified_cache_payload(payload["license_cache"])["max_client_accounts"] == 12


def test_login_sends_null_for_unlimited(client, sa_app):
    _create_admin(sa_app, max_client_accounts=None)

    payload = _login_api(client)

    assert payload["max_client_accounts"] is None
    assert _verified_cache_payload(payload["license_cache"])["max_client_accounts"] is None


def test_edited_cache_limit_fails_signature_check(client, sa_app):
    """An Admin raising their own limit in license_cache.json must break the
    signature - the app's verifier then rejects the whole cache."""
    _create_admin(sa_app, max_client_accounts=5)
    signed = _login_api(client)["license_cache"]
    signed["payload"]["max_client_accounts"] = 500

    public_key = serialization.load_pem_public_key(license_signing.get_public_key_pem())
    try:
        public_key.verify(base64.b64decode(signed["signature"]), license_signing.canonical_json(signed["payload"]))
    except Exception:  # noqa: BLE001 - InvalidSignature
        return
    raise AssertionError("tampered limit still verified")


def test_refresh_picks_up_a_raised_limit(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=5)
    login = _login_api(client)
    with sa_app.app_context():
        repo.update_admin(db.get_db(), admin["id"], max_client_accounts=20)

    resp = client.post("/api/auth/refresh", json={"token": login["token"]})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["max_client_accounts"] == 20
    assert _verified_cache_payload(body["license_cache"])["max_client_accounts"] == 20


# --- panel: create / edit ------------------------------------------------------------


def test_create_admin_with_a_client_limit(client, sa_app):
    _login_panel(client)

    resp = client.post("/admins/new", data=_create_form(max_client_accounts="8"), follow_redirects=True)

    assert b"Admin Created Successfully" in resp.data
    assert b"Max Client Accounts" in resp.data
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "abc_admin")
    assert admin["max_client_accounts"] == 8


def test_create_admin_unlimited(client, sa_app):
    _login_panel(client)

    resp = client.post(
        "/admins/new",
        data=_create_form(max_client_accounts="", unlimited_client_accounts="1"),
        follow_redirects=True,
    )

    assert b"Admin Created Successfully" in resp.data
    assert b"Unlimited" in resp.data
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "abc_admin")
    assert admin["max_client_accounts"] is None


def test_create_admin_rejects_zero_or_blank_limit(client, sa_app):
    _login_panel(client)

    for bad in ("0", "", "abc"):
        resp = client.post("/admins/new", data=_create_form(max_client_accounts=bad), follow_redirects=True)
        assert b"Max client accounts must be" in resp.data

    with sa_app.app_context():
        assert repo.get_admin_by_username(db.get_db(), "abc_admin") is None


def test_create_form_prefills_five(client):
    _login_panel(client)
    resp = client.get("/admins/new")
    assert b'name="max_client_accounts"' in resp.data
    assert b'value="5"' in resp.data


def test_edit_admin_raises_lowers_and_sets_unlimited(client, sa_app):
    admin = _create_admin(sa_app)
    _login_panel(client)

    client.post(f"/admins/{admin['id']}/edit", data=_edit_form(admin, max_client_accounts="15"))
    assert _get_admin(sa_app, admin["id"])["max_client_accounts"] == 15

    client.post(f"/admins/{admin['id']}/edit", data=_edit_form(admin, max_client_accounts="3"))
    assert _get_admin(sa_app, admin["id"])["max_client_accounts"] == 3

    client.post(
        f"/admins/{admin['id']}/edit",
        data=_edit_form(admin, max_client_accounts="", unlimited_client_accounts="1"),
    )
    assert _get_admin(sa_app, admin["id"])["max_client_accounts"] is None

    client.post(f"/admins/{admin['id']}/edit", data=_edit_form(admin, max_client_accounts="7"))
    assert _get_admin(sa_app, admin["id"])["max_client_accounts"] == 7


def test_edit_admin_with_invalid_limit_saves_nothing(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=5)
    _login_panel(client)

    resp = client.post(
        f"/admins/{admin['id']}/edit",
        data=_edit_form(admin, name="Renamed", max_client_accounts="0"),
        follow_redirects=True,
    )

    assert b"not saved" in resp.data
    saved = _get_admin(sa_app, admin["id"])
    assert saved["max_client_accounts"] == 5
    assert saved["name"] == "ABC Trading"


def test_form_without_the_limit_field_uses_default_on_create_and_keeps_it_on_edit(client, sa_app):
    """An older form or script that doesn't send the field at all must not
    be rejected, and must not wipe a limit already set."""
    _login_panel(client)
    form = _create_form()
    del form["max_client_accounts"]
    client.post("/admins/new", data=form)
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "abc_admin")
    assert admin["max_client_accounts"] == config.DEFAULT_MAX_CLIENT_ACCOUNTS

    with sa_app.app_context():
        repo.update_admin(db.get_db(), admin["id"], max_client_accounts=9)
    edit = _edit_form(admin, name="Renamed")
    del edit["max_client_accounts"]
    client.post(f"/admins/{admin['id']}/edit", data=edit)
    saved = _get_admin(sa_app, admin["id"])
    assert saved["name"] == "Renamed"
    assert saved["max_client_accounts"] == 9


# --- panel: usage display --------------------------------------------------------


def test_dashboard_and_detail_show_reported_usage(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=5)
    token = _login_api(client)["token"]
    client.post("/api/telemetry/sync", json={"token": token, "client_count": 2, "enabled_client_count": 1})
    _login_panel(client)

    dashboard = client.get("/").data.decode()
    assert "2 / 5" in dashboard
    assert "just now" in dashboard

    detail = client.get(f"/admins/{admin['id']}").data.decode()
    assert "2 / 5" in detail
    assert "last updated by their app just now" in detail


def test_dashboard_before_any_report_and_for_unlimited(client, sa_app):
    _create_admin(sa_app, max_client_accounts=None)
    _login_panel(client)

    dashboard = client.get("/").data.decode()
    assert "0 / Unlimited" in dashboard


def test_detail_page_shows_unlimited_ticked(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=None)
    _login_panel(client)

    detail = client.get(f"/admins/{admin['id']}").data.decode()
    assert "not reported yet" in detail
    toggle = detail.split('name="unlimited_client_accounts"', 1)[1].split("/>", 1)[0]
    assert "checked" in toggle


def test_time_ago():
    now = datetime.now(timezone.utc)
    assert time_ago(None) == "never"
    assert time_ago(now.isoformat()) == "just now"
    assert time_ago((now - timedelta(minutes=3, seconds=5)).isoformat()) == "3 min ago"
    assert time_ago((now - timedelta(hours=5)).isoformat()) == "5 h ago"
    assert time_ago((now - timedelta(days=2)).isoformat()) == "2 d ago"
