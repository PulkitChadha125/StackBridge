"""The client limit is one pool shared by all of an Admin's servers: each
server reports its own count, adding is checked against the total, and a
revoked server's clients stop counting."""

from datetime import date, timedelta

from superadmin_panel import config, db, repositories as repo, security


def _create_admin(sa_app, **overrides):
    defaults = dict(
        name="ABC Trading",
        username="abc_admin",
        password_hash=security.hash_password("Temp-Pass-1"),
        expiry_date=(date.today() + timedelta(days=30)).isoformat(),
        allowed_installations=5,
        max_client_accounts=20,
    )
    defaults.update(overrides)
    with sa_app.app_context():
        return repo.create_admin(db.get_db(), **defaults)


def _login(client, fingerprint, username="abc_admin"):
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Temp-Pass-1", "device_fingerprint": fingerprint},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _reserve(client, token, count):
    return client.post("/api/clients/reserve", json={"token": token, "client_count": count})


def test_two_servers_share_one_pool(client, sa_app):
    """The user's own example: limit 20, server 1 grows 5 -> 11, server 2
    grows 7 -> 8 (19 total), one more fits anywhere, then both are full."""
    _create_admin(sa_app, max_client_accounts=20)
    vps1 = _login(client, "vps-1")["token"]
    vps2 = _login(client, "vps-2")["token"]

    assert _reserve(client, vps1, 5).status_code == 200
    assert _reserve(client, vps2, 7).status_code == 200
    assert _reserve(client, vps1, 11).get_json()["client_usage_total"] == 18
    resp = _reserve(client, vps2, 8)
    assert resp.status_code == 200
    assert resp.get_json()["client_usage_total"] == 19

    assert _reserve(client, vps1, 12).status_code == 200  # 20 / 20

    refused = _reserve(client, vps2, 9)
    assert refused.status_code == 409
    body = refused.get_json()
    assert body["error"] == "client_limit_reached"
    assert body["allowed"] is False
    assert body["max_client_accounts"] == 20
    assert body["client_usage_total"] == 20
    assert body["support_contact"] == {
        "email": "hello@thinkstak.com",
        "whatsapp_url": "https://wa.me/918273366104",
    }
    assert _reserve(client, vps1, 13).status_code == 409


def test_refused_reservation_records_nothing(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=3)
    token = _login(client, "vps-1")["token"]
    _reserve(client, token, 3)

    assert _reserve(client, token, 4).status_code == 409

    with sa_app.app_context():
        assert repo.total_client_usage(db.get_db(), admin["id"]) == 3


def test_deleting_frees_slots_for_every_server(client, sa_app):
    _create_admin(sa_app, max_client_accounts=5)
    vps1 = _login(client, "vps-1")["token"]
    vps2 = _login(client, "vps-2")["token"]
    _reserve(client, vps1, 5)
    assert _reserve(client, vps2, 1).status_code == 409

    assert _reserve(client, vps1, 3).status_code == 200  # deleted 2 on vps-1

    assert _reserve(client, vps2, 2).status_code == 200


def test_shrinking_is_allowed_even_when_over_the_limit(client, sa_app):
    """If a limit is ever lowered below usage, deletes must still go through."""
    admin = _create_admin(sa_app, max_client_accounts=10)
    token = _login(client, "vps-1")["token"]
    _reserve(client, token, 8)
    with sa_app.app_context():
        repo.update_admin(db.get_db(), admin["id"], max_client_accounts=5)

    assert _reserve(client, token, 9).status_code == 409
    assert _reserve(client, token, 8).status_code == 200
    assert _reserve(client, token, 6).status_code == 200


def test_unlimited_admin_is_never_refused(client, sa_app):
    _create_admin(sa_app, max_client_accounts=None)
    token = _login(client, "vps-1")["token"]

    resp = _reserve(client, token, 500)

    assert resp.status_code == 200
    assert resp.get_json()["max_client_accounts"] is None
    assert resp.get_json()["client_usage_total"] == 500


def test_revoking_a_server_releases_its_slots(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=5)
    vps1 = _login(client, "vps-1")
    vps2 = _login(client, "vps-2")["token"]
    _reserve(client, vps1["token"], 4)
    assert _reserve(client, vps2, 2).status_code == 409

    with sa_app.app_context():
        repo.revoke_device(db.get_db(), vps1["device_id"])

    assert _reserve(client, vps2, 5).status_code == 200
    with sa_app.app_context():
        assert repo.total_client_usage(db.get_db(), admin["id"]) == 5


def test_reserve_rejects_bad_requests(client, sa_app):
    _create_admin(sa_app)
    token = _login(client, "vps-1")["token"]

    assert client.post("/api/clients/reserve", json={"token": "bogus", "client_count": 1}).status_code == 401
    assert _reserve(client, token, "abc").status_code == 400
    assert _reserve(client, token, -1).status_code == 400
    assert client.post("/api/clients/reserve", json={"token": token}).status_code == 400


def test_telemetry_count_updates_this_servers_usage(client, sa_app):
    """The periodic report is the app's own truth for that server, so it
    repairs a stale count (e.g. a delete made while offline) - and it is
    never refused, even above the limit."""
    admin = _create_admin(sa_app, max_client_accounts=5)
    token = _login(client, "vps-1")["token"]
    _reserve(client, token, 5)

    resp = client.post("/api/telemetry/sync", json={"token": token, "client_count": 2})

    assert resp.status_code == 200
    assert resp.get_json()["client_usage_total"] == 2
    assert resp.get_json()["max_client_accounts"] == 5
    with sa_app.app_context():
        assert repo.total_client_usage(db.get_db(), admin["id"]) == 2


def test_login_and_refresh_report_total_and_support_contact(client, sa_app):
    _create_admin(sa_app, max_client_accounts=20)
    vps1 = _login(client, "vps-1")["token"]
    _reserve(client, vps1, 6)

    login = _login(client, "vps-2")
    assert login["client_usage_total"] == 6
    assert login["support_contact"]["email"] == "hello@thinkstak.com"

    _reserve(client, login["token"], 3)
    refreshed = client.post("/api/auth/refresh", json={"token": vps1}).get_json()
    assert refreshed["client_usage_total"] == 9


def test_support_contact_is_editable_in_settings(client, sa_app):
    _create_admin(sa_app)
    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})

    page = client.get("/settings").data.decode()
    assert "Support Contact" in page
    assert "hello@thinkstak.com" in page

    client.post(
        "/settings",
        data={
            "form_name": "support_contact",
            "support_email": "help@example.com",
            "support_whatsapp_url": "https://wa.me/911234567890",
        },
    )
    login = _login(client, "vps-1")
    assert login["support_contact"] == {
        "email": "help@example.com",
        "whatsapp_url": "https://wa.me/911234567890",
    }

    resp = client.post(
        "/settings",
        data={"form_name": "support_contact", "support_email": "not-an-email", "support_whatsapp_url": ""},
        follow_redirects=True,
    )
    assert b"not saved" in resp.data

    client.post("/settings", data={"form_name": "support_contact", "support_email": "", "support_whatsapp_url": ""})
    token = login["token"]
    refreshed = client.post("/api/auth/refresh", json={"token": token}).get_json()
    assert refreshed["support_contact"]["email"] == "hello@thinkstak.com"


def test_dashboard_and_detail_sum_all_servers(client, sa_app):
    admin = _create_admin(sa_app, max_client_accounts=20)
    vps1 = _login(client, "vps-1")["token"]
    vps2 = _login(client, "vps-2")["token"]
    _reserve(client, vps1, 11)
    _reserve(client, vps2, 8)
    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})

    assert "19 / 20" in client.get("/").data.decode()
    detail = client.get(f"/admins/{admin['id']}").data.decode()
    assert "19 / 20" in detail
    assert "<td>11</td>" in detail and "<td>8</td>" in detail


def test_upgrade_seeds_usage_from_last_telemetry(tmp_path, sa_app, client):
    """A server upgraded with Admins already reporting: their last reported
    count becomes their starting usage."""
    admin = _create_admin(sa_app)
    token = _login(client, "vps-1")["token"]
    client.post("/api/telemetry/sync", json={"token": token, "client_count": 4})
    with sa_app.app_context():
        conn = db.get_db()
        conn.execute("DELETE FROM client_usage")
        conn.commit()

    db.init_db()

    with sa_app.app_context():
        assert repo.total_client_usage(db.get_db(), admin["id"]) == 4
