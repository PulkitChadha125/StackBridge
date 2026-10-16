from datetime import datetime, timedelta, timezone

from superadmin_panel import repositories as repo
from superadmin_panel import security


def _make_admin(conn, **overrides):
    defaults = dict(
        name="ABC Trading",
        username="abc_admin",
        password_hash=security.hash_password("temp-pass"),
        expiry_date="2030-01-01",
        allowed_installations=1,
    )
    defaults.update(overrides)
    return repo.create_admin(conn, **defaults)


def test_create_and_get_admin(conn):
    admin = _make_admin(conn)
    fetched = repo.get_admin(conn, admin["id"])
    assert fetched["username"] == "abc_admin"
    assert fetched["status"] == "active"
    assert fetched["must_change_password"] == 1


def test_get_admin_by_username(conn):
    admin = _make_admin(conn)
    fetched = repo.get_admin_by_username(conn, "abc_admin")
    assert fetched["id"] == admin["id"]
    assert repo.get_admin_by_username(conn, "nonexistent") is None


def test_list_admins(conn):
    _make_admin(conn, username="a1")
    _make_admin(conn, username="a2")
    admins = repo.list_admins(conn)
    assert len(admins) == 2


def test_update_admin_rejects_unknown_field(conn):
    admin = _make_admin(conn)
    try:
        repo.update_admin(conn, admin["id"], password_hash="nope")
        assert False, "should have raised"
    except ValueError:
        pass


def test_update_admin_extend_expiry(conn):
    admin = _make_admin(conn)
    updated = repo.update_admin(conn, admin["id"], expiry_date="2031-06-30")
    assert updated["expiry_date"] == "2031-06-30"


def test_set_admin_password_and_must_change_flag(conn):
    admin = _make_admin(conn)
    new_hash = security.hash_password("new-permanent-password")
    updated = repo.set_admin_password(conn, admin["id"], new_hash, must_change_password=False)
    assert updated["password_hash"] == new_hash
    assert updated["must_change_password"] == 0


def test_set_admin_status_revoke_and_reactivate(conn):
    admin = _make_admin(conn)
    revoked = repo.set_admin_status(conn, admin["id"], "revoked")
    assert revoked["status"] == "revoked"
    reactivated = repo.set_admin_status(conn, admin["id"], "active")
    assert reactivated["status"] == "active"


def test_is_admin_expired():
    future_admin = {"expiry_date": "2999-01-01"}
    past_admin = {"expiry_date": "2000-01-01"}
    assert not repo.is_admin_expired(future_admin)
    assert repo.is_admin_expired(past_admin)


def test_device_registration_and_touch(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fingerprint-1")
    assert device["status"] == "active"
    first_seen = device["first_seen_at"]

    device_again = repo.register_or_touch_device(conn, admin["id"], "fingerprint-1")
    assert device_again["id"] == device["id"]
    assert device_again["first_seen_at"] == first_seen  # unchanged, only last_seen bumps


def test_count_active_devices_excludes_revoked(conn):
    admin = _make_admin(conn, allowed_installations=2)
    d1 = repo.register_or_touch_device(conn, admin["id"], "fp1")
    repo.register_or_touch_device(conn, admin["id"], "fp2")
    assert repo.count_active_devices(conn, admin["id"]) == 2

    repo.revoke_device(conn, d1["id"])
    assert repo.count_active_devices(conn, admin["id"]) == 1


def test_revoke_device_also_revokes_its_sessions(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fp1")
    session_row = repo.create_session(
        conn, admin_id=admin["id"], device_id=device["id"], token_hash="hash1", ttl_seconds=3600
    )
    assert repo.is_session_valid(session_row)

    repo.revoke_device(conn, device["id"])
    refreshed = repo.get_session(conn, session_row["id"])
    assert not repo.is_session_valid(refreshed)


def test_session_lifecycle(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fp1")
    session_row = repo.create_session(
        conn, admin_id=admin["id"], device_id=device["id"], token_hash="hash-x", ttl_seconds=3600
    )
    assert repo.is_session_valid(session_row)

    fetched = repo.get_session_by_token_hash(conn, "hash-x")
    assert fetched["id"] == session_row["id"]

    repo.revoke_session(conn, session_row["id"])
    revoked = repo.get_session(conn, session_row["id"])
    assert not repo.is_session_valid(revoked)


def test_session_expiry_detected(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fp1")
    session_row = repo.create_session(
        conn, admin_id=admin["id"], device_id=device["id"], token_hash="hash-exp", ttl_seconds=1
    )
    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert not repo.is_session_valid(session_row, as_of=future)


def test_revoke_all_sessions_for_admin(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fp1")
    s1 = repo.create_session(conn, admin_id=admin["id"], device_id=device["id"], token_hash="h1", ttl_seconds=3600)
    s2 = repo.create_session(conn, admin_id=admin["id"], device_id=device["id"], token_hash="h2", ttl_seconds=3600)

    repo.revoke_all_sessions_for_admin(conn, admin["id"])

    assert not repo.is_session_valid(repo.get_session(conn, s1["id"]))
    assert not repo.is_session_valid(repo.get_session(conn, s2["id"]))


def test_create_admin_email_defaults_to_none(conn):
    admin = _make_admin(conn)
    assert admin["email"] is None


def test_create_and_update_admin_email(conn):
    admin = _make_admin(conn, email="owner@example.com")
    assert admin["email"] == "owner@example.com"
    updated = repo.update_admin(conn, admin["id"], email="new@example.com")
    assert updated["email"] == "new@example.com"


def test_delete_admin_removes_row(conn):
    admin = _make_admin(conn)
    repo.delete_admin(conn, admin["id"])
    assert repo.get_admin(conn, admin["id"]) is None


def test_delete_admin_cascades_devices_sessions_telemetry(conn):
    admin = _make_admin(conn)
    device = repo.register_or_touch_device(conn, admin["id"], "fp1")
    repo.create_session(conn, admin_id=admin["id"], device_id=device["id"], token_hash="h1", ttl_seconds=3600)
    repo.upsert_telemetry(
        conn,
        admin_id=admin["id"],
        device_id=device["id"],
        app_version="1.0",
        copier_status="running",
        copier_running=True,
        client_count=2,
        enabled_client_count=1,
        recent_trades=[],
    )

    repo.delete_admin(conn, admin["id"])

    assert repo.list_devices(conn, admin["id"]) == []
    assert repo.get_telemetry(conn, admin["id"]) is None
    assert conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE admin_id = ?", (admin["id"],)).fetchone()["n"] == 0


def test_delete_admin_is_a_noop_for_unknown_id(conn):
    repo.delete_admin(conn, "does-not-exist")  # must not raise


def test_list_admins_due_for_expiry_reminder(conn):
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)

    due_tomorrow = _make_admin(conn, username="due", expiry_date="2026-06-02", email="a@example.com")
    _make_admin(conn, username="not-due-yet", expiry_date="2026-06-10", email="b@example.com")
    _make_admin(conn, username="already-expired", expiry_date="2026-05-20", email="c@example.com")

    due = repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of)
    assert [a["id"] for a in due] == [due_tomorrow["id"]]


def test_list_admins_due_for_expiry_reminder_excludes_already_reminded(conn):
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    admin = _make_admin(conn, expiry_date="2026-06-02")

    assert len(repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of)) == 1

    repo.mark_expiry_reminder_sent(conn, admin["id"], admin["expiry_date"])
    assert repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of) == []


def test_list_admins_due_for_expiry_reminder_rearms_after_expiry_date_changes(conn):
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    admin = _make_admin(conn, expiry_date="2026-06-02")
    repo.mark_expiry_reminder_sent(conn, admin["id"], admin["expiry_date"])
    assert repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of) == []

    repo.update_admin(conn, admin["id"], expiry_date="2026-06-02")  # unchanged - still suppressed
    assert repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of) == []

    extended = repo.update_admin(conn, admin["id"], expiry_date="2026-07-01")
    due_later = repo.list_admins_due_for_expiry_reminder(
        conn, as_of=datetime(2026, 6, 30, tzinfo=timezone.utc)
    )
    assert [a["id"] for a in due_later] == [extended["id"]]


def test_list_admins_due_for_expiry_reminder_excludes_revoked(conn):
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    admin = _make_admin(conn, expiry_date="2026-06-02")
    repo.set_admin_status(conn, admin["id"], "revoked")
    assert repo.list_admins_due_for_expiry_reminder(conn, as_of=as_of) == []


def test_get_admin_by_email_is_case_insensitive(conn):
    admin = _make_admin(conn, email="Jane@Example.com")
    assert repo.get_admin_by_email(conn, "jane@example.com")["id"] == admin["id"]
    assert repo.get_admin_by_email(conn, "someone.else@example.com") is None


def test_get_admin_by_email_ignores_admins_without_one(conn):
    _make_admin(conn, username="no-email", email=None)
    assert repo.get_admin_by_email(conn, "") is None


def test_create_admin_records_signup_source(conn):
    trial = _make_admin(conn, username="t1", signup_source="trial_signup")
    manual = _make_admin(conn, username="m1")
    assert trial["signup_source"] == "trial_signup"
    assert manual["signup_source"] is None


def test_trial_device_recorded_and_found(conn):
    admin = _make_admin(conn)
    assert repo.has_trial_device_been_used(conn, "machine-1") is False

    repo.record_trial_device(conn, "machine-1", admin["id"])

    assert repo.has_trial_device_been_used(conn, "machine-1") is True
    assert repo.get_trial_device_owner(conn, "machine-1") == admin["id"]


def test_admin_deletion_clears_trial_device_and_leaves_tombstone(conn):
    admin = _make_admin(conn)
    repo.record_trial_device(conn, "machine-1", admin["id"])

    repo.delete_admin(conn, admin["id"])

    # Deletion is a full reset - this machine can take a new trial.
    assert repo.has_trial_device_been_used(conn, "machine-1") is False
    assert repo.is_admin_deleted(conn, admin["id"]) is True


def test_record_trial_device_is_idempotent(conn):
    first = _make_admin(conn, username="a1")
    second = _make_admin(conn, username="a2")

    repo.record_trial_device(conn, "machine-1", first["id"])
    repo.record_trial_device(conn, "machine-1", second["id"])

    # First writer wins - the device belongs to whoever burned the trial.
    assert repo.get_trial_device_owner(conn, "machine-1") == first["id"]


def test_trial_email_recorded_and_found_case_insensitively(conn):
    admin = _make_admin(conn)
    assert repo.has_trial_email_been_used(conn, "jane@example.com") is False

    repo.record_trial_email(conn, "Jane@Example.com", admin["id"])

    assert repo.has_trial_email_been_used(conn, "jane@example.com") is True
    assert repo.has_trial_email_been_used(conn, "JANE@EXAMPLE.COM") is True


def test_admin_deletion_clears_trial_email(conn):
    admin = _make_admin(conn)
    repo.record_trial_email(conn, "jane@example.com", admin["id"])

    repo.delete_admin(conn, admin["id"])

    assert repo.has_trial_email_been_used(conn, "jane@example.com") is False
