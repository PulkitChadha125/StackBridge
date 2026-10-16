from datetime import datetime, timezone

import pytest

from superadmin_panel import db, email_notifications, expiry_reminder, repositories, security


def _make_admin(conn, **overrides):
    defaults = dict(
        name="ABC Trading",
        username="abc_admin",
        password_hash=security.hash_password("temp-pass"),
        expiry_date="2026-06-02",
        allowed_installations=1,
    )
    defaults.update(overrides)
    return repositories.create_admin(conn, **defaults)


@pytest.fixture
def frozen_now(monkeypatch):
    fixed = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(repositories, "datetime", _FrozenDatetime)
    return fixed


def test_check_and_send_due_reminders_sends_and_marks(conn, frozen_now, monkeypatch):
    admin = _make_admin(conn, email="admin@example.com")
    sent = []
    monkeypatch.setattr(
        email_notifications,
        "send_expiry_reminder",
        lambda a, owner_email: sent.append((a["id"], owner_email)),
    )

    count = expiry_reminder.check_and_send_due_reminders()

    assert count == 1
    assert sent == [(admin["id"], None)]
    reloaded = repositories.get_admin(db.get_connection(), admin["id"])
    assert reloaded["last_expiry_reminder_sent_for"] == "2026-06-02"


def test_check_and_send_due_reminders_uses_saved_owner_email(conn, frozen_now, monkeypatch):
    _make_admin(conn, email="admin@example.com")
    repositories.set_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL, "owner@example.com")
    sent = []
    monkeypatch.setattr(
        email_notifications,
        "send_expiry_reminder",
        lambda a, owner_email: sent.append(owner_email),
    )

    expiry_reminder.check_and_send_due_reminders()

    assert sent == ["owner@example.com"]


def test_check_and_send_due_reminders_is_noop_when_nothing_due(conn, frozen_now, monkeypatch):
    _make_admin(conn, expiry_date="2026-12-31", email="admin@example.com")
    calls = []
    monkeypatch.setattr(
        email_notifications, "send_expiry_reminder", lambda a, owner_email: calls.append(a)
    )

    count = expiry_reminder.check_and_send_due_reminders()

    assert count == 0
    assert calls == []


def test_check_and_send_due_reminders_continues_after_one_failure(conn, frozen_now, monkeypatch):
    _make_admin(conn, username="fails", email="fails@example.com")
    _make_admin(conn, username="works", email="works@example.com")

    def _flaky(admin, owner_email):
        if admin["username"] == "fails":
            raise email_notifications.EmailSendError("boom")

    monkeypatch.setattr(email_notifications, "send_expiry_reminder", _flaky)

    count = expiry_reminder.check_and_send_due_reminders()

    assert count == 1  # only "works" got marked as sent
