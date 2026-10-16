from superadmin_panel import config, db, repositories as repo, security


def _bootstrap_creds(username="owner", password="S3cret!123"):
    config.save_superadmin_credentials(username, security.hash_password(password))
    return username, password


def test_login_rejected_when_no_credentials_configured(client):
    resp = client.post("/login", data={"username": "x", "password": "y"})
    assert resp.status_code == 200
    assert b"not been configured" in resp.data or b"not configured" in resp.data


def test_login_success_and_dashboard_access(client):
    _bootstrap_creds()
    resp = client.post(
        "/login", data={"username": "owner", "password": "S3cret!123"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"Admins" in resp.data


def test_login_wrong_password_rejected(client):
    _bootstrap_creds()
    resp = client.post("/login", data={"username": "owner", "password": "wrong"})
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data


def test_dashboard_requires_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def _login(client):
    _bootstrap_creds()
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})


def test_create_admin_flow_shows_temp_password_once(client, sa_app):
    _login(client)

    resp = client.post(
        "/admins/new",
        data={
            "name": "ABC Trading",
            "username": "abc_admin",
            "expiry_date": "2030-09-30",
            "allowed_installations": "1",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Admin Created Successfully" in resp.data
    assert b"abc_admin" in resp.data

    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "abc_admin")
    assert admin is not None
    assert admin["must_change_password"] == 1
    # only the hash is stored, never plaintext
    assert "$argon2id$" in admin["password_hash"]

    # Revisiting the "created" page a second time must not re-show the password.
    admin_id = admin["id"]
    resp2 = client.get(f"/admins/{admin_id}/created", follow_redirects=True)
    assert b"already been used" in resp2.data


def test_create_admin_rejects_duplicate_username(client):
    _login(client)
    payload = {
        "name": "ABC",
        "username": "dupe",
        "expiry_date": "2030-01-01",
        "allowed_installations": "1",
        "notes": "",
    }
    client.post("/admins/new", data=payload, follow_redirects=True)
    resp = client.post("/admins/new", data=payload, follow_redirects=True)
    assert b"already taken" in resp.data


def test_edit_admin_extends_expiry(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u1", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "u1")

    client.post(
        f"/admins/{admin['id']}/edit",
        data={"name": "N", "expiry_date": "2031-12-31", "allowed_installations": "3", "notes": "extended"},
        follow_redirects=True,
    )

    with sa_app.app_context():
        updated = repo.get_admin(db.get_db(), admin["id"])
    assert updated["expiry_date"] == "2031-12-31"
    assert updated["allowed_installations"] == 3


def test_revoke_admin_revokes_sessions(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u2", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_username(conn, "u2")
        device = repo.register_or_touch_device(conn, admin["id"], "fp-1")
        session_row = repo.create_session(
            conn, admin_id=admin["id"], device_id=device["id"], token_hash="th1", ttl_seconds=3600
        )

    client.post(f"/admins/{admin['id']}/revoke", follow_redirects=True)

    with sa_app.app_context():
        conn = db.get_db()
        refreshed_admin = repo.get_admin(conn, admin["id"])
        refreshed_session = repo.get_session(conn, session_row["id"])
    assert refreshed_admin["status"] == "revoked"
    assert not repo.is_session_valid(refreshed_session)


def test_reset_password_generates_new_temp_password(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u3", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "u3")
    original_hash = admin["password_hash"]

    resp = client.post(f"/admins/{admin['id']}/reset-password", follow_redirects=True)
    assert b"Password Reset" in resp.data

    with sa_app.app_context():
        updated = repo.get_admin(db.get_db(), admin["id"])
    assert updated["password_hash"] != original_hash
    assert updated["must_change_password"] == 1


def _create_admin_with_session(client, sa_app, username):
    client.post(
        "/admins/new",
        data={"name": "N", "username": username, "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_username(conn, username)
        # Simulate the Admin having already set their own password.
        repo.set_admin_password(conn, admin["id"], security.hash_password("forgotten-pw"), must_change_password=False)
        device = repo.register_or_touch_device(conn, admin["id"], "fp-set")
        session_row = repo.create_session(
            conn, admin_id=admin["id"], device_id=device["id"], token_hash="th-set", ttl_seconds=3600
        )
    return admin, session_row


def test_set_password_uses_chosen_password_and_revokes_sessions(client, sa_app):
    _login(client)
    admin, session_row = _create_admin_with_session(client, sa_app, "u4")

    resp = client.post(
        f"/admins/{admin['id']}/set-password",
        data={"new_password": "BrandNew#2026", "confirm_password": "BrandNew#2026", "must_change_password": "1"},
        follow_redirects=True,
    )
    assert b"Password for &#39;u4&#39; updated" in resp.data

    with sa_app.app_context():
        conn = db.get_db()
        updated = repo.get_admin(conn, admin["id"])
        refreshed_session = repo.get_session(conn, session_row["id"])
    assert security.verify_password("BrandNew#2026", updated["password_hash"])
    assert not security.verify_password("forgotten-pw", updated["password_hash"])
    assert updated["must_change_password"] == 1
    assert not repo.is_session_valid(refreshed_session)


def test_set_password_without_forced_change(client, sa_app):
    _login(client)
    admin, _ = _create_admin_with_session(client, sa_app, "u5")

    client.post(
        f"/admins/{admin['id']}/set-password",
        data={"new_password": "BrandNew#2026", "confirm_password": "BrandNew#2026"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        updated = repo.get_admin(db.get_db(), admin["id"])
    assert security.verify_password("BrandNew#2026", updated["password_hash"])
    assert updated["must_change_password"] == 0


def test_set_password_rejects_short_or_mismatched(client, sa_app):
    _login(client)
    admin, _ = _create_admin_with_session(client, sa_app, "u6")

    resp = client.post(
        f"/admins/{admin['id']}/set-password",
        data={"new_password": "short", "confirm_password": "short"},
        follow_redirects=True,
    )
    assert b"at least 8 characters" in resp.data

    resp = client.post(
        f"/admins/{admin['id']}/set-password",
        data={"new_password": "LongEnough1", "confirm_password": "LongEnough2"},
        follow_redirects=True,
    )
    assert b"do not match" in resp.data

    with sa_app.app_context():
        unchanged = repo.get_admin(db.get_db(), admin["id"])
    assert security.verify_password("forgotten-pw", unchanged["password_hash"])


def test_create_admin_rejects_malformed_expiry_date(client, sa_app):
    _login(client)
    resp = client.post(
        "/admins/new",
        data={
            "name": "N",
            "username": "u-bad-date",
            "expiry_date": "not-a-date",
            "allowed_installations": "1",
        },
        follow_redirects=True,
    )
    assert b"must be a valid date" in resp.data
    with sa_app.app_context():
        created = repo.get_admin_by_username(db.get_db(), "u-bad-date")
    assert created is None  # never created


def test_edit_admin_rejects_malformed_expiry_date(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u-edit-date", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "u-edit-date")

    resp = client.post(
        f"/admins/{admin['id']}/edit",
        data={"name": "N", "expiry_date": "13/45/9999", "allowed_installations": "1"},
        follow_redirects=True,
    )
    assert b"must be a valid date" in resp.data

    with sa_app.app_context():
        unchanged = repo.get_admin(db.get_db(), admin["id"])
    assert unchanged["expiry_date"] == "2030-01-01"  # not corrupted


def test_device_reactivate_blocked_when_at_installation_limit(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u4", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_username(conn, "u4")
        d1 = repo.register_or_touch_device(conn, admin["id"], "fp-a")
        d2_id = "manually-inserted"
        repo.register_or_touch_device(conn, admin["id"], "fp-b")

    # Revoke d1, then re-register a device to hit the limit=1 boundary via UI actions.
    client.post(f"/admins/{admin['id']}/devices/{d1['id']}/revoke", follow_redirects=True)

    with sa_app.app_context():
        conn = db.get_db()
        devices = repo.list_devices(conn, admin["id"])
    active = [d for d in devices if d["status"] == "active"]
    assert len(active) == 1  # fp-b remains active, at the limit of 1

    # Reactivating the revoked one should be blocked (would exceed the limit of 1).
    resp = client.post(f"/admins/{admin['id']}/devices/{d1['id']}/reactivate", follow_redirects=True)
    assert b"installation limit already reached" in resp.data


def test_create_admin_persists_email(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={
            "name": "N",
            "username": "u-email",
            "expiry_date": "2030-01-01",
            "allowed_installations": "1",
            "email": "admin@example.com",
        },
        follow_redirects=True,
    )
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "u-email")
    assert admin["email"] == "admin@example.com"


def test_create_admin_rejects_malformed_email(client):
    _login(client)
    resp = client.post(
        "/admins/new",
        data={
            "name": "N",
            "username": "u-bad-email",
            "expiry_date": "2030-01-01",
            "allowed_installations": "1",
            "email": "not-an-email",
        },
    )
    assert b"valid email" in resp.data


def test_edit_admin_updates_email(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u-edit-email", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        admin = repo.get_admin_by_username(db.get_db(), "u-edit-email")

    client.post(
        f"/admins/{admin['id']}/edit",
        data={"name": "N", "expiry_date": "2030-01-01", "allowed_installations": "1", "email": "new@example.com"},
        follow_redirects=True,
    )

    with sa_app.app_context():
        updated = repo.get_admin(db.get_db(), admin["id"])
    assert updated["email"] == "new@example.com"


def test_delete_admin_removes_admin_and_cascades(client, sa_app):
    _login(client)
    client.post(
        "/admins/new",
        data={"name": "N", "username": "u-delete", "expiry_date": "2030-01-01", "allowed_installations": "1"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_username(conn, "u-delete")
        repo.register_or_touch_device(conn, admin["id"], "fp-delete")

    resp = client.post(f"/admins/{admin['id']}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"deleted" in resp.data

    with sa_app.app_context():
        conn = db.get_db()
        assert repo.get_admin(conn, admin["id"]) is None
        assert repo.list_devices(conn, admin["id"]) == []


def test_delete_admin_unknown_id_flashes_not_found(client):
    _login(client)
    resp = client.post("/admins/does-not-exist/delete", follow_redirects=True)
    assert b"Admin not found" in resp.data


def test_settings_page_saves_owner_notification_email(client, sa_app):
    _login(client)
    client.post(
        "/settings",
        data={"form_name": "owner_notification_email", "owner_notification_email": "owner@example.com"},
        follow_redirects=True,
    )
    with sa_app.app_context():
        stored = repo.get_setting(db.get_db(), repo.SETTING_OWNER_NOTIFICATION_EMAIL)
    assert stored == "owner@example.com"


def test_settings_send_test_email_goes_to_owner(client, monkeypatch):
    from superadmin_panel import email_notifications

    sent = []
    monkeypatch.setattr(config, "OWNER_NOTIFICATION_EMAIL", "owner@example.com")
    monkeypatch.setattr(email_notifications, "send_email", lambda **kw: sent.append(kw))
    _login(client)
    resp = client.post("/settings", data={"form_name": "test_email"}, follow_redirects=True)
    assert b"Test email sent to owner@example.com" in resp.data
    assert sent and sent[0]["to_addrs"] == ["owner@example.com"]


def test_settings_send_test_email_reports_failure(client, monkeypatch):
    from superadmin_panel import email_notifications

    def boom(**kw):
        raise email_notifications.EmailSendError("login refused")

    monkeypatch.setattr(config, "OWNER_NOTIFICATION_EMAIL", "owner@example.com")
    monkeypatch.setattr(email_notifications, "send_email", boom)
    _login(client)
    resp = client.post("/settings", data={"form_name": "test_email"}, follow_redirects=True)
    assert b"could not be sent: login refused" in resp.data


def test_settings_page_rejects_malformed_owner_email(client):
    _login(client)
    resp = client.post(
        "/settings",
        data={"form_name": "owner_notification_email", "owner_notification_email": "not-an-email"},
        follow_redirects=True,
    )
    assert b"valid email" in resp.data
