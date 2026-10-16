"""SuperAdmin fetches an Admin's App Log: request -> the app picks it up ->
uploads -> SuperAdmin downloads. (The StakBridge app's own side is tested
with the app's licensing tests.)"""

import base64
import gzip
from datetime import date, datetime, timedelta, timezone

import pytest

from superadmin_panel import db, log_requests, repositories as repo, security

from .test_superadmin_web import _login
from .test_telemetry_api import _create_admin_and_login


@pytest.fixture
def admin_app(client, sa_app):
    """An Admin logged in from one server, and the SuperAdmin logged in to the panel."""
    admin, token = _create_admin_and_login(client, sa_app)
    with sa_app.app_context():
        device = repo.list_devices(db.get_db(), admin["id"])[0]
    _login(client)
    return admin, device, token


def _request(client, admin, device, date_from=None, date_to=None):
    today = date.today().isoformat()
    return client.post(
        f"/admins/{admin['id']}/logs/request",
        json={"device_id": device["id"], "date_from": date_from or today, "date_to": date_to or today},
    )


def _upload(client, token, request_id, text="2026-10-08 10:00:00.000 [INFO] hello\n", line_count=1):
    return client.post(
        "/api/logs/upload",
        json={
            "token": token,
            "request_id": request_id,
            "content_gzip_b64": base64.b64encode(gzip.compress(text.encode())).decode(),
            "line_count": line_count,
        },
    )


def test_validate_range():
    today = date(2026, 10, 8)
    assert log_requests.validate_range("2026-10-02", "2026-10-08", today) is None  # 7 days
    assert "at most 7 days" in log_requests.validate_range("2026-10-01", "2026-10-08", today)
    assert "on or before" in log_requests.validate_range("2026-10-08", "2026-10-07", today)
    assert "valid" in log_requests.validate_range("bad", "2026-10-07", today)
    assert "future" in log_requests.validate_range("2026-10-12", "2026-10-12", today)


def test_full_flow_request_pickup_upload_download(client, sa_app, admin_app):
    admin, device, token = admin_app
    resp = _request(client, admin, device)
    assert resp.status_code == 202
    item = resp.get_json()
    assert item["status"] == "pending" and not item["done"]

    pending = client.post("/api/logs/pending", json={"token": token}).get_json()
    assert pending["request"]["id"] == item["id"]
    assert pending["poll_seconds"] == log_requests.POLL_SECONDS
    # Picked up once only.
    assert client.post("/api/logs/pending", json={"token": token}).get_json()["request"] is None

    assert client.get(item["status_url"]).get_json()["status"] == "picked_up"
    assert _upload(client, token, item["id"]).status_code == 200

    status = client.get(item["status_url"]).get_json()
    assert status["status"] == "ready" and status["done"] and status["downloadable"]
    download = client.get(status["download_url"])
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert f"app_log_abc_admin_{item['date_from']}_to_{item['date_to']}.txt" in download.headers["Content-Disposition"]
    body = download.get_data(as_text=True)
    assert body.startswith("# StakBridge - App Log of ABC Trading (abc_admin)")
    assert "2026-10-08 10:00:00.000 [INFO] hello" in body

    page = client.get(f"/admins/{admin['id']}").get_data(as_text=True)
    assert "App Logs" in page and "Download" in page


def test_range_over_7_days_rejected(client, admin_app):
    admin, device, _ = admin_app
    today = date.today()
    resp = _request(client, admin, device, (today - timedelta(days=7)).isoformat(), today.isoformat())
    assert resp.status_code == 400
    assert "7 days" in resp.get_json()["message"]


def test_one_open_request_per_server(client, admin_app):
    admin, device, _ = admin_app
    assert _request(client, admin, device).status_code == 202
    assert _request(client, admin, device).status_code == 409


def test_device_of_another_admin_or_revoked_rejected(client, sa_app, admin_app):
    admin, device, _ = admin_app
    with sa_app.app_context():
        other = repo.create_admin(
            db.get_db(),
            name="Other",
            username="other_admin",
            password_hash=security.hash_password("Temp-Pass-1"),
            expiry_date=(date.today() + timedelta(days=30)).isoformat(),
            allowed_installations=1,
        )
    assert _request(client, other, device).status_code == 400
    with sa_app.app_context():
        repo.revoke_device(db.get_db(), device["id"])
    assert _request(client, admin, device).status_code == 400


def test_request_needs_superadmin_login(client, sa_app):
    admin, token = _create_admin_and_login(client, sa_app)
    resp = client.post(f"/admins/{admin['id']}/logs/request", json={})
    assert resp.status_code == 302


def test_app_endpoints_need_a_valid_session(client):
    assert client.post("/api/logs/pending", json={"token": "bogus"}).status_code == 401
    assert client.post("/api/logs/upload", json={"token": "bogus", "request_id": "x"}).status_code == 401


def test_upload_rejects_wrong_request_bad_content_and_repeat(client, admin_app):
    admin, device, token = admin_app
    item = _request(client, admin, device).get_json()
    assert _upload(client, token, "not-a-request").status_code == 404
    bad = client.post(
        "/api/logs/upload",
        json={"token": token, "request_id": item["id"], "content_gzip_b64": base64.b64encode(b"plain").decode()},
    )
    assert bad.status_code == 400
    assert _upload(client, token, item["id"]).status_code == 200
    assert _upload(client, token, item["id"]).status_code == 409


def test_app_reported_error_marks_failed(client, admin_app):
    admin, device, token = admin_app
    item = _request(client, admin, device).get_json()
    client.post("/api/logs/upload", json={"token": token, "request_id": item["id"], "error": "disk gone"})
    status = client.get(item["status_url"]).get_json()
    assert status["status"] == "failed" and "disk gone" in status["message"]
    assert not status["downloadable"]


def _age(sa_app, request_id, **fields):
    with sa_app.app_context():
        conn = db.get_db()
        for name, seconds in fields.items():
            moment = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
            conn.execute(f"UPDATE log_requests SET {name} = ? WHERE id = ?", (moment, request_id))
        conn.commit()


def test_unanswered_request_expires(client, sa_app, admin_app):
    admin, device, _ = admin_app
    item = _request(client, admin, device).get_json()
    _age(sa_app, item["id"], created_at=log_requests.PICKUP_TIMEOUT_SECONDS + 5)
    status = client.get(item["status_url"]).get_json()
    assert status["status"] == "expired" and status["done"]
    assert "open and logged in" in status["message"]
    # A new request can be made right away.
    assert _request(client, admin, device).status_code == 202


def test_old_uploads_are_deleted(client, sa_app, admin_app):
    admin, device, token = admin_app
    item = _request(client, admin, device).get_json()
    client.post("/api/logs/pending", json={"token": token})
    _upload(client, token, item["id"])
    assert log_requests.file_path(item["id"]).exists()
    _age(sa_app, item["id"], created_at=log_requests.RETENTION_HOURS * 3600 + 5)
    client.get(f"/admins/{admin['id']}")  # listing purges
    assert not log_requests.file_path(item["id"]).exists()
    assert client.get(item["status_url"]).status_code == 404
