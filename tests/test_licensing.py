"""Licence: StakBridge signing in to the real SuperAdmin panel (superadmin_panel/).

The panel runs in this process; StakBridge reaches it through the Flask test client instead of
the network (one test also uses a real HTTP socket). Both sides are the real code.
"""

import json
import threading
import gzip
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from stakbridge.api import LicenceOptions, create_app
from stakbridge.config import load_settings
from stakbridge.licensing import paths
from stakbridge.licensing.client import requests_transport
from superadmin_panel import config as sa_config
from superadmin_panel import db as sa_db
from superadmin_panel import license_signing, log_requests, rate_limit
from superadmin_panel import repositories as repo
from superadmin_panel import security as sa_security

from conftest import UI, wait_until

PASSWORD = "Temp-Pass-1"
SERVER = "http://licence.test"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """The SuperAdmin panel with its own database and signing keys in a temp folder."""
    sa_db.set_db_path_override(tmp_path / "panel.db")
    monkeypatch.setattr(sa_config, "CONFIG_PATH", tmp_path / "superadmin_config.json")
    monkeypatch.setattr(sa_config, "SECRET_KEY_PATH", tmp_path / "flask_secret_key.txt")
    monkeypatch.setattr(sa_config, "EXE_OUTPUT_DIR", str(tmp_path / "built_exes"))
    monkeypatch.setattr(sa_config, "LOG_UPLOAD_DIR", str(tmp_path / "log_uploads"))
    license_signing.set_key_paths_override(tmp_path / "keys" / "private.pem", tmp_path / "keys" / "public.pem")
    rate_limit.reset_all()
    from superadmin_panel.app import create_app as create_panel

    app = create_panel()
    app.config.update(TESTING=True)
    with app.app_context():
        license_signing.load_or_create_private_key()
    yield Panel(app, tmp_path / "keys" / "public.pem")
    sa_db.set_db_path_override(None)
    license_signing.set_key_paths_override(None, None)
    rate_limit.reset_all()


class Panel:
    def __init__(self, app, public_key):
        self.app, self.public_key = app, public_key
        self.client = app.test_client()
        self.online = True

    def transport(self, url, payload, timeout):
        if not self.online:
            raise OSError("no route to host")
        response = self.client.post(url.removeprefix(SERVER), json=payload)
        return response.status_code, response.get_json(silent=True) or {}

    def run(self, fn, *args, **kwargs):
        with self.app.app_context():
            return fn(sa_db.get_db(), *args, **kwargs)

    def create_admin(self, username="abc_admin", must_change=False, max_clients=5, installs=1, **extra):
        admin = self.run(repo.create_admin, name=username.upper(), username=username,
                         password_hash=sa_security.hash_password(PASSWORD),
                         expiry_date=(date.today() + timedelta(days=30)).isoformat(),
                         allowed_installations=installs, max_client_accounts=max_clients, **extra)
        if not must_change:
            self.run(repo.clear_must_change_password, admin["id"])
        return admin


class App:
    """StakBridge started on a device folder, signed in as nobody yet."""

    def __init__(self, client: TestClient):
        self.client = client
        self.licensing = client.app.state.licensing

    def get(self, path):
        return self.client.get(f"/v1{path}")

    def post(self, path, body=None):
        return self.client.post(f"/v1{path}", json=body, headers=UI)

    def patch(self, path, body):
        return self.client.patch(f"/v1{path}", json=body, headers=UI)

    def delete(self, path):
        return self.client.delete(f"/v1{path}", headers=UI)

    def login(self, username="abc_admin", password=PASSWORD):
        return self.post("/session", {"username": username, "password": password})

    def add_account(self, account_id, login):
        return self.post("/accounts", {"id": account_id, "name": account_id, "login": login,
                                       "server": "Demo", "password": "x"})


@pytest.fixture
def start_app(panel, tmp_path):
    started = []

    def start(device="pc1", fingerprint="fingerprint-pc1", public_key=None):
        settings = load_settings(data_dir=tmp_path / device / "data", backend="fake", status_interval=0.2,
                                 copy_retry_interval=0.3)
        licence = LicenceOptions(SERVER, tmp_path / device, public_key or panel.public_key,
                                 transport=panel.transport, fingerprint=lambda: fingerprint)
        client = TestClient(create_app(settings, licence))
        client.__enter__()
        started.append(client)
        return App(client)

    yield start
    for client in started:
        client.__exit__(None, None, None)


def test_sign_in_with_the_licence_and_change_the_first_password(panel, start_app):
    panel.create_admin(must_change=True)
    app = start_app()
    assert app.get("/session").json()["licensed"] is True
    assert app.get("/accounts").status_code == 401
    assert app.post("/session/setup", {"password": "whatever-123"}).status_code == 409

    res = app.login(password="wrong-pass")
    assert res.status_code == 401 and res.json()["error"]["code"] == "INVALID_CREDENTIALS"

    res = app.login()
    assert res.status_code == 200 and res.json()["mustChangePassword"] is True
    res = app.get("/accounts")
    assert res.status_code == 403 and res.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"

    res = app.post("/session/password", {"currentPassword": "nope-nope", "newPassword": "New-Pass-22"})
    assert res.status_code == 401
    assert app.post("/session/password", {"currentPassword": PASSWORD, "newPassword": "New-Pass-22"}).status_code == 200
    assert app.get("/accounts").json() == {"accounts": []}

    licence = app.get("/license").json()
    assert licence["valid"] and licence["username"] == "abc_admin" and licence["clients"]["limit"] == 5
    # each Admin's data is in their own folder
    assert app.licensing.data_dir().parent == app.licensing.folder / paths.ADMINS_DIR
    assert (app.licensing.data_dir() / "stakbridge.db").exists()


def test_refused_sign_ins(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    panel.run(repo.set_admin_status, admin["id"], "revoked")
    assert app.login().json()["error"]["code"] == "ADMIN_REVOKED"
    panel.run(repo.set_admin_status, admin["id"], "active")
    panel.run(repo.update_admin, admin["id"], expiry_date=(date.today() - timedelta(days=1)).isoformat())
    assert app.login().json()["error"]["code"] == "LICENSE_EXPIRED"

    panel.create_admin("one_pc")
    assert start_app("pc1", "fp-1").login("one_pc").status_code == 200
    res = start_app("pc2", "fp-2").login("one_pc")
    assert res.status_code == 403 and res.json()["error"]["code"] == "INSTALLATION_LIMIT_REACHED"

    panel.online = False
    res = app.login()
    assert res.status_code == 503 and res.json()["error"]["code"] == "LICENSE_SERVER_UNREACHABLE"


def test_restart_keeps_the_sign_in_and_the_desktop_link_signs_the_window_in(panel, start_app):
    panel.create_admin()
    first = start_app()
    assert first.login().status_code == 200
    assert first.add_account("acct_a", 5001).status_code == 201
    first.client.__exit__(None, None, None)

    again = start_app()
    assert again.licensing.signed_in  # session.bin read back (DPAPI on Windows)
    assert again.get("/session").json()["loggedIn"] is False  # this browser has no cookie yet
    link = again.client.app.state.make_launch_link()
    res = again.client.get(link, follow_redirects=False)
    assert res.status_code == 303 and "stakbridge_session" in res.cookies
    assert [a["id"] for a in again.get("/accounts").json()["accounts"]] == ["acct_a"]
    assert again.client.get(link, follow_redirects=False).cookies.get("stakbridge_session") is None  # one use only


def test_two_admins_on_one_pc_never_see_each_others_accounts(panel, start_app):
    panel.create_admin("alice", installs=2)
    panel.create_admin("bob", installs=2)
    app = start_app()
    app.login("alice")
    app.add_account("alice_master", 5001)
    app.login("bob")
    assert app.get("/accounts").json() == {"accounts": []}
    app.add_account("bob_master", 6001)
    app.login("alice")
    assert [a["id"] for a in app.get("/accounts").json()["accounts"]] == ["alice_master"]


def test_signing_out_stops_copying_on_this_pc(panel, start_app):
    panel.create_admin()
    app = start_app()
    app.login()
    app.add_account("acct_a", 5001)
    workers = app.client.app.state.workers
    wait_until(lambda: workers.status("acct_a")["status"] == "connected")
    assert app.delete("/session").status_code == 200
    assert not app.licensing.signed_in and app.client.app.state.workers is None
    assert workers.get("acct_a") is None  # its worker was stopped
    assert app.get("/accounts").status_code == 401


def copy_setup(app):
    for account_id, login in (("acct_a", 5001), ("acct_b", 5002)):
        assert app.add_account(account_id, login).status_code == 201
        assert app.patch(f"/accounts/{account_id}", {"tradingOn": True}).status_code == 200
    state = app.client.app.state
    wait_until(lambda: all(state.workers.status(a)["status"] == "connected" for a in ("acct_a", "acct_b")))
    assert app.client.put("/v1/copier", json={"masterId": "acct_a", "clients": []}, headers=UI).status_code == 200
    wait_until(lambda: app.get("/copier").json()["watching"])


def master_open(app):
    result = app.client.app.state.workers.get("acct_a").send("open", order={
        "symbol": "XAUUSD", "side": "buy", "type": "market", "volume": 0.1, "sl": None, "tp": None,
        "magic": 0, "comment": "hand"}).result(timeout=5)
    return result["ticket"]


def positions(app, account_id):
    return app.get(f"/accounts/{account_id}/positions").json()["positions"]


def test_a_revoked_licence_stops_new_copies_but_closes_still_follow(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    app.login()
    copy_setup(app)
    ticket = master_open(app)
    wait_until(lambda: len(positions(app, "acct_b")) == 1)

    panel.run(repo.set_admin_status, admin["id"], "revoked")
    check = app.licensing.check()
    assert check == {"valid": False, "reason": "admin_revoked", "message": check["message"]}
    assert app.get("/license").json()["valid"] is False

    master_open(app)
    wait_until(lambda: any(r["reason"] == "LICENSE_INACTIVE" for r in app.get("/copier").json()["copies"]))
    assert len(positions(app, "acct_b")) == 1
    app.client.app.state.workers.get("acct_a").send("close_position", ticket=ticket).result(timeout=5)
    wait_until(lambda: positions(app, "acct_b") == [])  # the client still follows the master out

    res = app.patch("/accounts/acct_b", {"tradingOn": True})
    assert res.status_code == 403 and res.json()["error"]["code"] == "LICENSE_INACTIVE"


def test_offline_the_signed_licence_is_used_until_its_grace_time(panel, start_app, tmp_path):
    panel.create_admin()
    app = start_app()
    app.login()
    panel.online = False
    check = app.licensing.check()
    assert check["valid"] and check["reason"] == "offline_grace"

    cache_file = app.licensing.cache.path
    signed = json.loads(cache_file.read_text())
    signed["payload"]["grace_until"] = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    cache_file.write_text(json.dumps(signed))  # an edited licence fails its signature
    assert app.licensing.check()["reason"] == "no_cache"
    assert app.get("/license").json()["valid"] is False

    panel.online = True
    assert app.licensing.check()["valid"]
    with panel.app.app_context():  # a correctly signed licence whose grace time is over
        payload = {**json.loads(cache_file.read_text())["payload"],
                   "grace_until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
        cache_file.write_text(json.dumps(license_signing.sign_license_payload(payload)))
    panel.online = False
    assert app.licensing.check()["reason"] == "grace_expired"


def test_a_licence_signed_by_another_server_is_not_trusted(panel, start_app, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = tmp_path / "other_public.pem"
    other.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    panel.create_admin()
    app = start_app(public_key=other)
    app.login()
    panel.online = False
    assert app.licensing.check()["reason"] == "no_cache"


def test_a_deleted_admin_is_signed_out_and_their_data_erased(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    app.login()
    app.add_account("acct_a", 5001)
    folder = app.licensing.data_dir()
    assert folder.exists()

    panel.run(repo.delete_admin, admin["id"])
    assert app.licensing.check()["reason"] == "admin_deleted"
    assert not folder.exists() and not app.licensing.signed_in
    assert not app.licensing.session_file.path.exists()
    assert app.get("/accounts").status_code == 401


def test_the_client_limit_is_asked_before_an_account_is_added(panel, start_app):
    admin = panel.create_admin(max_clients=1)
    app = start_app()
    app.login()
    assert app.add_account("acct_a", 5001).status_code == 201  # the master's place is free
    assert app.add_account("acct_b", 5002).status_code == 201  # client 1 of 1
    res = app.add_account("acct_c", 5003)
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "CLIENT_LIMIT_REACHED" and error["details"]["limit"] == 1
    assert error["details"]["support"]["email"]
    assert len(app.get("/accounts").json()["accounts"]) == 2

    assert app.delete("/accounts/acct_b").status_code == 200
    wait_until(lambda: panel.run(repo.total_client_usage, admin["id"]) == 0)
    assert app.add_account("acct_c", 5003).status_code == 201
    assert app.get("/license").json()["clients"]["total"] == 1

    panel.online = False  # adding needs the server; removing does not
    assert app.add_account("acct_d", 5004).json()["error"]["code"] == "LICENSE_SERVER_UNREACHABLE"
    assert app.delete("/accounts/acct_c").status_code == 200


def test_telemetry_reports_counts_only(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    app.login()
    copy_setup(app)
    master_open(app)
    wait_until(lambda: app.get("/orders").json()["orders"])
    assert app.licensing.sync_telemetry()["ok"]
    telemetry = panel.run(repo.get_telemetry, admin["id"])
    assert telemetry["client_count"] == 1 and telemetry["copier_running"]
    sent = json.dumps(telemetry)
    assert "Demo" not in sent and "5002" not in sent  # no servers or logins
    assert '"XAUUSD"' in sent


def test_the_superadmin_can_download_this_pcs_log(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    app.login()
    logs = app.licensing.logs_folder
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"stakbridge-{date.today().isoformat()}.log").write_text("line one\nline two\n", encoding="utf-8")
    device = panel.run(repo.list_devices, admin["id"])[0]
    request = panel.run(log_requests.create_request, admin["id"], device["id"], date.today().isoformat(),
                        date.today().isoformat())
    assert app.licensing.check_log_requests()["ok"]
    done = panel.run(log_requests.get_request, request["id"])
    assert done["status"] == "ready" and done["line_count"] == 2 + 3  # then "", the trades header, "No master trades"
    with gzip.open(log_requests.file_path(request["id"]), "rt", encoding="utf-8") as file:
        assert file.read().startswith("line one\nline two\n\n==== Trades:")


def test_sign_in_over_a_real_http_connection(panel, tmp_path):
    from werkzeug.serving import make_server

    panel.create_admin()
    server = make_server("127.0.0.1", 0, panel.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = load_settings(data_dir=tmp_path / "http" / "data", backend="fake")
        licence = LicenceOptions(f"http://127.0.0.1:{server.port}", tmp_path / "http", panel.public_key,
                                 transport=requests_transport, fingerprint=lambda: "fp-http")
        with TestClient(create_app(settings, licence)) as client:
            app = App(client)
            assert app.login().status_code == 200
            assert app.get("/license").json()["valid"]
    finally:
        server.shutdown()


def test_an_ended_sign_in_asks_to_sign_in_again_while_copying_keeps_following(panel, start_app):
    """The panel's Revoke and Reset password end every session of the Admin."""
    admin = panel.create_admin()
    app = start_app()
    app.login()
    app.add_account("acct_a", 5001)
    panel.run(repo.revoke_all_sessions_for_admin, admin["id"])

    check = app.licensing.check()
    assert check["reason"] == "invalid_session" and "Sign in again" in check["message"]
    assert app.get("/accounts").status_code == 401
    session = app.get("/session").json()
    assert session["loggedIn"] is False and session["licence"]["reason"] == "invalid_session"
    assert app.client.app.state.workers.get("acct_a") is not None  # still running: closes keep being copied

    assert app.login().status_code == 200  # same Admin: the same data, nothing restarted
    assert [a["id"] for a in app.get("/accounts").json()["accounts"]] == ["acct_a"]
    assert app.get("/license").json()["valid"]


def test_telemetry_and_logs_show_which_clients_got_each_trade(panel, start_app):
    admin = panel.create_admin()
    app = start_app()
    app.login()
    copy_setup(app)
    ticket = master_open(app)
    wait_until(lambda: [c for o in app.get("/orders").json()["orders"] for c in o["copies"] if c["status"] == "open"])

    assert app.licensing.sync_telemetry()["ok"]
    trade = panel.run(repo.get_telemetry, admin["id"])["recent_trades"][0]
    assert trade["clients"] == [{"name": "acct_b", "status": "open", "quantity": 0.1, "reason": None}]

    device = panel.run(repo.list_devices, admin["id"])[0]
    request = panel.run(log_requests.create_request, admin["id"], device["id"], date.today().isoformat(),
                        date.today().isoformat())
    assert app.licensing.check_log_requests()["ok"]
    assert panel.run(log_requests.get_request, request["id"])["status"] == "ready"
    with gzip.open(log_requests.file_path(request["id"]), "rt", encoding="utf-8") as file:
        text = file.read()
    assert "==== Trades:" in text and f"Master trade #{ticket} on acct_a: buy market 0.1 XAUUSD" in text
    assert "  -> acct_b - OPEN - 0.1 lots XAUUSD" in text
