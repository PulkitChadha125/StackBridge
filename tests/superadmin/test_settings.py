"""Editable delivery server address.

The address compiled into built EXEs must be changeable from the panel, not
only via an environment variable - the license server can move (a different
VPS, a customer hosting their own), and the operator should not have to touch
the machine's environment or restart anything to follow it.
"""

import pytest

from superadmin_panel import config, db, repositories as repo, security


def _login(client):
    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})


def test_settings_page_requires_login(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_settings_page_renders(client):
    _login(client)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"Server Address" in resp.data


def test_saving_an_address_persists_it(client, sa_app):
    _login(client)
    client.post("/settings", data={"public_server_url": "https://license.example.com"})
    with sa_app.app_context():
        stored = repo.get_setting(db.get_db(), repo.SETTING_PUBLIC_SERVER_URL)
    assert stored == "https://license.example.com"


def test_trailing_slash_is_stripped_on_save(client, sa_app):
    _login(client)
    client.post("/settings", data={"public_server_url": "https://license.example.com/"})
    with sa_app.app_context():
        assert repo.get_setting(db.get_db(), repo.SETTING_PUBLIC_SERVER_URL) == "https://license.example.com"


@pytest.mark.parametrize("bad", ["not-a-url", "ftp://x.example", "example.com", "https://"])
def test_malformed_addresses_are_rejected(client, sa_app, bad):
    _login(client)
    resp = client.post("/settings", data={"public_server_url": bad}, follow_redirects=True)
    assert b"does not look like a URL" in resp.data
    with sa_app.app_context():
        assert repo.get_setting(db.get_db(), repo.SETTING_PUBLIC_SERVER_URL) is None


def test_empty_value_clears_the_setting(client, sa_app):
    _login(client)
    client.post("/settings", data={"public_server_url": "https://license.example.com"})
    client.post("/settings", data={"public_server_url": "   "})
    with sa_app.app_context():
        assert repo.get_setting(db.get_db(), repo.SETTING_PUBLIC_SERVER_URL) is None


def test_saved_address_is_what_gets_baked_into_a_build(client, sa_app, monkeypatch):
    """The whole point: what the operator typed is what reaches the builder."""
    from superadmin_panel import exe_builder

    _login(client)
    client.post("/settings", data={"public_server_url": "https://vps.example.net"})

    admin = _make_admin(sa_app)
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    seen = {}

    def fake_start(admin_row, server_url):
        seen["url"] = server_url
        return exe_builder.BuildJob(id="j", admin_id=admin_row["id"], admin_name=admin_row["name"])

    monkeypatch.setattr(exe_builder, "start_build", fake_start)
    client.post(f"/admins/{admin['id']}/build-exe")
    assert seen["url"] == "https://vps.example.net"


def test_saved_address_overrides_the_environment_variable(client, sa_app, monkeypatch):
    _login(client)
    monkeypatch.setattr(config, "PUBLIC_SERVER_URL", "https://from-env.example")
    client.post("/settings", data={"public_server_url": "https://from-panel.example"})
    resp = client.get("/settings")
    assert b"https://from-panel.example" in resp.data


def test_admin_page_shows_the_address_that_will_be_used(client, sa_app):
    _login(client)
    client.post("/settings", data={"public_server_url": "https://shown.example"})
    admin = _make_admin(sa_app)
    resp = client.get(f"/admins/{admin['id']}")
    assert b"https://shown.example" in resp.data


def _make_admin(sa_app):
    from datetime import date, timedelta

    with sa_app.app_context():
        return repo.create_admin(
            db.get_db(),
            name="Test One",
            username="test_admin",
            password_hash=security.hash_password("Temp-Pass-1"),
            expiry_date=(date.today() + timedelta(days=30)).isoformat(),
            allowed_installations=1,
        )


def test_rebuild_trial_exe_button_forces_a_build(client, monkeypatch, build_calls):
    from superadmin_panel import exe_builder

    _login(client)
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    resp = client.post("/settings", data={"form_name": "rebuild_trial_exe"}, follow_redirects=True)
    assert b"Trial EXE build started" in resp.data
    assert build_calls[-1]["admin_id"] == exe_builder.TRIAL_BUILD_ID


def test_saving_an_address_repoints_the_trial_exe(client, monkeypatch, build_calls):
    from superadmin_panel import exe_builder

    _login(client)
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    client.post("/settings", data={"public_server_url": "https://new.example"})
    assert build_calls == [{"admin_id": exe_builder.TRIAL_BUILD_ID, "server_url": "https://new.example"}]


def test_settings_page_explains_trial_exe_status(client):
    _login(client)
    resp = client.get("/settings")
    assert b"Free Trial Signups" in resp.data
    assert b"Rebuild Trial EXE" in resp.data
