from datetime import date, timedelta

from superadmin_panel import db, repositories as repo, security


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


def test_login_success_issues_token_and_registers_device(client, sa_app):
    _create_admin(sa_app)
    resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["token"]
    assert payload["must_change_password"] is True
    assert payload["license_valid"] is True

    with sa_app.app_context():
        devices = repo.list_devices(db.get_db(), payload["admin_id"])
    assert len(devices) == 1
    assert devices[0]["device_fingerprint"] == "device-A"


def test_login_incorrect_password_rejected(client, sa_app):
    _create_admin(sa_app)
    resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "wrong", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_credentials"


def test_login_unknown_admin_rejected_generically(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_credentials"


def test_login_revoked_admin_rejected(client, sa_app):
    admin = _create_admin(sa_app)
    with sa_app.app_context():
        repo.set_admin_status(db.get_db(), admin["id"], "revoked")
    resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "admin_revoked"


def test_login_expired_admin_rejected(client, sa_app):
    _create_admin(sa_app, expiry_date="2000-01-01")
    resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "license_expired"


def test_login_first_device_then_reuse_same_device_ok(client, sa_app):
    _create_admin(sa_app, allowed_installations=1)
    resp1 = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp2.status_code == 200  # same device reused, not counted twice


def test_login_installation_limit_exceeded(client, sa_app):
    _create_admin(sa_app, allowed_installations=1)
    resp1 = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-B"},
    )
    assert resp2.status_code == 403
    assert resp2.get_json()["error"] == "installation_limit_reached"


def test_login_revoked_device_rejected(client, sa_app):
    admin = _create_admin(sa_app, allowed_installations=2)
    client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    with sa_app.app_context():
        conn = db.get_db()
        device = repo.get_device_by_fingerprint(conn, admin["id"], "device-A")
        repo.revoke_device(conn, device["id"])

    resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "device_revoked"


def test_refresh_valid_session(client, sa_app):
    _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]

    resp = client.post("/api/auth/refresh", json={"token": token})
    assert resp.status_code == 200
    assert resp.get_json()["license_valid"] is True


def test_refresh_invalid_session_rejected(client, sa_app):
    _create_admin(sa_app)
    resp = client.post("/api/auth/refresh", json={"token": "not-a-real-token"})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_session"


def test_refresh_after_logout_is_rejected(client, sa_app):
    _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]

    logout_resp = client.post("/api/auth/logout", json={"token": token})
    assert logout_resp.status_code == 200

    refresh_resp = client.post("/api/auth/refresh", json={"token": token})
    assert refresh_resp.status_code == 401
    assert refresh_resp.get_json()["error"] == "invalid_session"


def test_logout_is_idempotent_on_unknown_token(client):
    resp = client.post("/api/auth/logout", json={"token": "does-not-exist"})
    assert resp.status_code == 200


def test_refresh_reflects_expired_license_without_killing_session(client, sa_app):
    admin = _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]

    with sa_app.app_context():
        repo.update_admin(db.get_db(), admin["id"], expiry_date="2000-01-01")

    resp = client.post("/api/auth/refresh", json={"token": token})
    assert resp.status_code == 200  # session itself still valid - identity intact
    assert resp.get_json()["license_valid"] is False


def test_revoking_admin_invalidates_existing_session_on_refresh(client, sa_app):
    admin = _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]

    with sa_app.app_context():
        conn = db.get_db()
        repo.set_admin_status(conn, admin["id"], "revoked")
        repo.revoke_all_sessions_for_admin(conn, admin["id"])

    resp = client.post("/api/auth/refresh", json={"token": token})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_session"


def test_change_password_forces_and_clears_must_change_flag(client, sa_app):
    _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]
    assert login_resp.get_json()["must_change_password"] is True

    resp = client.post(
        "/api/auth/change-password",
        json={"token": token, "current_password": "Temp-Pass-1", "new_password": "New-Permanent-Pass-1"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["must_change_password"] is False
    new_token = payload["token"]
    assert new_token != token  # rotated

    # Old token must now be dead.
    old_check = client.post("/api/auth/refresh", json={"token": token})
    assert old_check.status_code == 401

    # New login must use the new password.
    relogin = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "New-Permanent-Pass-1", "device_fingerprint": "device-A"},
    )
    assert relogin.status_code == 200


def test_change_password_rejects_wrong_current_password(client, sa_app):
    _create_admin(sa_app)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "abc_admin", "password": "Temp-Pass-1", "device_fingerprint": "device-A"},
    )
    token = login_resp.get_json()["token"]

    resp = client.post(
        "/api/auth/change-password",
        json={"token": token, "current_password": "wrong", "new_password": "New-Permanent-Pass-1"},
    )
    assert resp.status_code == 401


def test_login_missing_device_fingerprint_rejected(client, sa_app):
    _create_admin(sa_app)
    resp = client.post("/api/auth/login", json={"username": "abc_admin", "password": "Temp-Pass-1"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# One free trial per device - the guard against signing up again under a new
# email on the same machine (see trial_signup.py's module docstring).
# --------------------------------------------------------------------------


def test_second_trial_on_same_device_rejected(client, sa_app):
    _create_admin(sa_app, username="trial_one", signup_source="trial_signup")
    _create_admin(sa_app, username="trial_two", signup_source="trial_signup")

    first = client.post(
        "/api/auth/login",
        json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/auth/login",
        json={"username": "trial_two", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert second.status_code == 403
    assert second.get_json()["error"] == "trial_already_used"


def test_trial_admin_can_still_log_back_into_their_own_device(client, sa_app):
    _create_admin(sa_app, username="trial_one", signup_source="trial_signup")

    for _ in range(2):
        resp = client.post(
            "/api/auth/login",
            json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
        )
        assert resp.status_code == 200


def test_second_trial_on_a_different_device_is_allowed(client, sa_app):
    _create_admin(sa_app, username="trial_one", signup_source="trial_signup")
    _create_admin(sa_app, username="trial_two", signup_source="trial_signup")

    client.post(
        "/api/auth/login",
        json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"username": "trial_two", "password": "Temp-Pass-1", "device_fingerprint": "machine-2"},
    )
    assert resp.status_code == 200


def test_paid_admin_unaffected_by_a_used_trial_device(client, sa_app):
    """A trial on this machine must not lock a real paying customer out of
    the same machine - the check is scoped to trial-sourced admins only."""
    _create_admin(sa_app, username="trial_one", signup_source="trial_signup")
    _create_admin(sa_app, username="paying", signup_source=None)

    client.post(
        "/api/auth/login",
        json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"username": "paying", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert resp.status_code == 200


def test_deleting_trial_admin_reopens_trial_for_device_and_email(client, sa_app):
    """Deleting an Admin is a full reset: the same machine and email can
    take a new trial afterwards."""
    admin = _create_admin(sa_app, username="trial_one", signup_source="trial_signup")
    _create_admin(sa_app, username="trial_two", signup_source="trial_signup")

    client.post(
        "/api/auth/login",
        json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    with sa_app.app_context():
        conn = db.get_db()
        repo.record_trial_email(conn, "someone@example.com", admin["id"])
        repo.delete_admin(conn, admin["id"])
        assert not repo.has_trial_email_been_used(conn, "someone@example.com")
        assert not repo.has_trial_device_been_used(conn, "machine-1")

    resp = client.post(
        "/api/auth/login",
        json={"username": "trial_two", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert resp.status_code == 200


def test_refresh_reports_deleted_admin(client, sa_app):
    admin = _create_admin(sa_app, username="gone")
    login = client.post(
        "/api/auth/login",
        json={"username": "gone", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    token = login.get_json()["token"]
    with sa_app.app_context():
        repo.delete_admin(db.get_db(), admin["id"])

    resp = client.post("/api/auth/refresh", json={"token": token, "admin_id": admin["id"]})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "admin_deleted"

    # Without the admin_id it's indistinguishable from any other dead token.
    resp = client.post("/api/auth/refresh", json={"token": token})
    assert resp.get_json()["error"] == "invalid_session"


def test_settings_allow_new_trial_unblocks_the_device(client, sa_app):
    """Testing a trial twice on one PC: the SuperAdmin clears that PC's
    trial record from Settings, and the next trial account can log in there.
    The first trial account is left as it was."""
    from superadmin_panel import config

    first_admin = _create_admin(sa_app, username="trial_one", signup_source="trial_signup")
    _create_admin(sa_app, username="trial_two", signup_source="trial_signup")
    client.post(
        "/api/auth/login",
        json={"username": "trial_one", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    blocked = client.post(
        "/api/auth/login",
        json={"username": "trial_two", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert blocked.get_json()["error"] == "trial_already_used"

    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})
    page = client.get("/settings").data.decode()
    assert "PCs that have used a free trial" in page
    assert "Allow new trial" in page and "machine-1" in page

    resp = client.post(
        "/settings", data={"form_name": "allow_trial_device", "fingerprint": "machine-1"}, follow_redirects=True
    )
    assert b"can now start a new free trial" in resp.data

    allowed = client.post(
        "/api/auth/login",
        json={"username": "trial_two", "password": "Temp-Pass-1", "device_fingerprint": "machine-1"},
    )
    assert allowed.status_code == 200
    with sa_app.app_context():
        assert repo.get_admin(db.get_db(), first_admin["id"])["status"] == "active"

    missing = client.post(
        "/settings", data={"form_name": "allow_trial_device", "fingerprint": "nope"}, follow_redirects=True
    )
    assert b"not found" in missing.data
