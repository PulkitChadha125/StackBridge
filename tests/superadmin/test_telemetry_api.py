from datetime import date, timedelta

from superadmin_panel import db, repositories as repo, security


def _create_admin_and_login(client, sa_app, **overrides):
    defaults = dict(
        name="ABC Trading",
        username="abc_admin",
        password_hash=security.hash_password("Temp-Pass-1"),
        expiry_date=(date.today() + timedelta(days=30)).isoformat(),
        allowed_installations=1,
    )
    defaults.update(overrides)
    with sa_app.app_context():
        admin = repo.create_admin(db.get_db(), **defaults)

    login_resp = client.post(
        "/api/auth/login",
        json={"username": defaults["username"], "password": "Temp-Pass-1", "device_fingerprint": "fp-1"},
    )
    assert login_resp.status_code == 200
    return admin, login_resp.get_json()["token"]


def test_sync_stores_snapshot(client, sa_app):
    admin, token = _create_admin_and_login(client, sa_app)

    resp = client.post(
        "/api/telemetry/sync",
        json={
            "token": token,
            "app_version": "1.2.3",
            "copier_status": "running",
            "copier_running": True,
            "client_count": 5,
            "enabled_client_count": 4,
            "recent_trades": [{"symbol": "EURUSD", "side": "BUY", "quantity": 0.1, "status": "success"}],
        },
    )
    assert resp.status_code == 200

    with sa_app.app_context():
        telemetry = repo.get_telemetry(db.get_db(), admin["id"])
    assert telemetry["app_version"] == "1.2.3"
    assert telemetry["copier_running"] is True
    assert telemetry["client_count"] == 5
    assert len(telemetry["recent_trades"]) == 1


def test_sync_rejects_invalid_token(client, sa_app):
    resp = client.post("/api/telemetry/sync", json={"token": "bogus", "client_count": 1})
    assert resp.status_code == 401


def test_sync_caps_recent_trades_at_20(client, sa_app):
    admin, token = _create_admin_and_login(client, sa_app)
    trades = [{"symbol": "EURUSD", "side": "BUY", "quantity": 0.1, "status": "success"} for _ in range(50)]

    client.post("/api/telemetry/sync", json={"token": token, "recent_trades": trades})

    with sa_app.app_context():
        telemetry = repo.get_telemetry(db.get_db(), admin["id"])
    assert len(telemetry["recent_trades"]) == 20


def test_sync_upserts_not_duplicates(client, sa_app):
    admin, token = _create_admin_and_login(client, sa_app)

    client.post("/api/telemetry/sync", json={"token": token, "client_count": 1})
    client.post("/api/telemetry/sync", json={"token": token, "client_count": 2})

    with sa_app.app_context():
        conn = db.get_db()
        all_rows = repo.list_all_telemetry(conn)
        telemetry = repo.get_telemetry(conn, admin["id"])
    assert len(all_rows) == 1  # one row per admin, not a growing history
    assert telemetry["client_count"] == 2


def test_telemetry_does_not_leak_across_admins(client, sa_app):
    admin_a, token_a = _create_admin_and_login(client, sa_app, username="admin_a")
    admin_b, token_b = _create_admin_and_login(client, sa_app, username="admin_b")

    client.post("/api/telemetry/sync", json={"token": token_a, "client_count": 111})
    client.post("/api/telemetry/sync", json={"token": token_b, "client_count": 222})

    with sa_app.app_context():
        conn = db.get_db()
        telemetry_a = repo.get_telemetry(conn, admin_a["id"])
        telemetry_b = repo.get_telemetry(conn, admin_b["id"])
    assert telemetry_a["client_count"] == 111
    assert telemetry_b["client_count"] == 222


def test_sync_response_never_echoes_a_secret_field(client, sa_app):
    """Sanity check: the request payload the Admin app sends never needs to
    (and must not) include MetaApi tokens or passwords - the endpoint has no
    field for them, so there's nothing to accidentally store/leak."""
    admin, token = _create_admin_and_login(client, sa_app)
    resp = client.post(
        "/api/telemetry/sync",
        json={"token": token, "client_count": 1, "meta_token": "should-be-ignored-if-sent"},
    )
    assert resp.status_code == 200

    with sa_app.app_context():
        telemetry = repo.get_telemetry(db.get_db(), admin["id"])
    # No column exists to store an arbitrary "meta_token" field - it's simply
    # dropped. This documents the intent and would fail loudly (KeyError) if
    # such a field were ever accidentally added to the schema/upsert call.
    assert "meta_token" not in telemetry
