import pytest

from superadmin_panel import config, email_notifications


class _FakeSMTP:
    sent_messages = []
    login_calls = []
    started_tls = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        _FakeSMTP.started_tls = True

    def login(self, username, password):
        _FakeSMTP.login_calls.append((username, password))

    def send_message(self, message):
        _FakeSMTP.sent_messages.append(message)


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.sent_messages = []
    _FakeSMTP.login_calls = []
    _FakeSMTP.started_tls = False
    yield


@pytest.fixture
def _configured_smtp(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.hostinger.com")
    monkeypatch.setattr(config, "SMTP_PORT", 465)
    monkeypatch.setattr(config, "SMTP_USERNAME", "hello@thinkstak.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(config, "SMTP_FROM_ADDRESS", "hello@thinkstak.com")
    monkeypatch.setattr(config, "SMTP_SECURITY", "SSL")
    monkeypatch.setattr(email_notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_notifications.smtplib, "SMTP", _FakeSMTP)


def test_send_email_raises_when_smtp_not_configured(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", None)
    with pytest.raises(email_notifications.EmailSendError):
        email_notifications.send_email(to_addrs=["a@example.com"], subject="s", body="b")


def test_send_email_uses_ssl_client_on_port_465(_configured_smtp):
    email_notifications.send_email(to_addrs=["a@example.com"], subject="Subject", body="Body")
    assert len(_FakeSMTP.sent_messages) == 1
    assert _FakeSMTP.login_calls == [("hello@thinkstak.com", "secret")]
    assert not _FakeSMTP.started_tls


def test_send_email_uses_starttls_when_configured(monkeypatch, _configured_smtp):
    monkeypatch.setattr(config, "SMTP_SECURITY", "STARTTLS")
    email_notifications.send_email(to_addrs=["a@example.com"], subject="Subject", body="Body")
    assert _FakeSMTP.started_tls
    assert len(_FakeSMTP.sent_messages) == 1


def test_send_email_dedupes_and_skips_blank_recipients(_configured_smtp):
    email_notifications.send_email(
        to_addrs=["a@example.com", " a@example.com ", "", None], subject="s", body="b"
    )
    message = _FakeSMTP.sent_messages[0]
    assert message["To"] == "a@example.com"


def test_send_email_noop_when_no_recipients(_configured_smtp):
    email_notifications.send_email(to_addrs=[], subject="s", body="b")
    assert _FakeSMTP.sent_messages == []


def test_send_expiry_reminder_sends_to_admin_and_owner(_configured_smtp):
    admin = {"id": "1", "name": "ABC Trading", "username": "abc", "expiry_date": "2026-06-02", "email": "admin@example.com"}
    email_notifications.send_expiry_reminder(admin, owner_email="owner@example.com")
    message = _FakeSMTP.sent_messages[0]
    assert "admin@example.com" in message["To"]
    assert "owner@example.com" in message["To"]
    assert "ABC Trading" in message["Subject"]


def test_send_expiry_reminder_skips_when_no_recipients(_configured_smtp):
    admin = {"id": "1", "name": "ABC Trading", "username": "abc", "expiry_date": "2026-06-02", "email": None}
    email_notifications.send_expiry_reminder(admin, owner_email=None)
    assert _FakeSMTP.sent_messages == []


def test_send_expiry_reminder_admin_only(_configured_smtp):
    admin = {"id": "1", "name": "ABC Trading", "username": "abc", "expiry_date": "2026-06-02", "email": "admin@example.com"}
    email_notifications.send_expiry_reminder(admin, owner_email=None)
    message = _FakeSMTP.sent_messages[0]
    assert message["To"] == "admin@example.com"
