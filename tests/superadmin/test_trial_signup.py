import json
from datetime import date, timedelta

import pytest

from superadmin_panel import config, db, email_notifications, exe_builder
from superadmin_panel import repositories as repo
from superadmin_panel import security


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture welcome emails instead of hitting a real SMTP server."""
    sent = []

    def _capture(admin, *, download_url, temp_password, owner_email):
        sent.append(
            {
                "admin_id": admin["id"],
                "email": admin.get("email"),
                "username": admin["username"],
                "expiry_date": admin["expiry_date"],
                "temp_password": temp_password,
                "download_url": download_url,
                "owner_email": owner_email,
            }
        )

    monkeypatch.setattr(email_notifications, "send_trial_welcome_email", _capture)
    return sent


def _place_trial_exe(server_url="http://localhost", content=b"MZ fake exe"):
    """Drop a finished shared trial EXE where the server keeps it, recorded as
    built for server_url (what a test request resolves to by default)."""
    trial_dir = config.exe_output_dir() / exe_builder.TRIAL_BUILD_ID
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "StakBridge.exe").write_bytes(content)
    (trial_dir / exe_builder.BUILD_INFO_FILENAME).write_text(
        json.dumps({"server_url": server_url, "built_at": "2026-10-03T00:00:00+00:00"}),
        encoding="utf-8",
    )


@pytest.fixture
def trial_exe_ready():
    _place_trial_exe()


@pytest.fixture
def can_build(monkeypatch):
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])


@pytest.fixture
def cannot_build(monkeypatch):
    monkeypatch.setattr(exe_builder, "preflight", lambda: ["PyInstaller is not installed"])


# Verification codes "emailed" during the current test, by email.
_sent_codes: dict[str, str] = {}


@pytest.fixture(autouse=True)
def sent_codes(monkeypatch):
    """Capture verification codes instead of emailing them."""
    _sent_codes.clear()

    def _capture(*, email, name, code):
        _sent_codes[email.lower()] = code

    monkeypatch.setattr(email_notifications, "send_trial_verification_code", _capture)
    return _sent_codes


def _request_code(client, name="Jane Trader", email="jane@example.com"):
    return client.post("/trial", data={"name": name, "email": email}, follow_redirects=True)


def _verify(client, email="jane@example.com", code=None):
    if code is None:
        code = _sent_codes[email.lower()]
    return client.post("/trial/verify", data={"email": email, "code": code}, follow_redirects=True)


def _signup(client, name="Jane Trader", email="jane@example.com"):
    """The whole flow: request a code, then enter it. Returns the step-1
    page when no code screen was shown (refused, rate limited, ...)."""
    resp = _request_code(client, name=name, email=email)
    if b"Verify Your Email" not in resp.data:
        return resp
    return _verify(client, email=email)


def test_signup_creates_seven_day_trial_admin(client, sa_app, trial_exe_ready, sent_emails):
    resp = _signup(client)

    assert resp.status_code == 200
    assert b"Your Trial Is Ready" in resp.data

    with sa_app.app_context():
        admin = repo.get_admin_by_email(db.get_db(), "jane@example.com")
    assert admin is not None
    assert admin["signup_source"] == "trial_signup"
    assert admin["status"] == "active"
    assert admin["must_change_password"] == 1
    assert admin["allowed_installations"] == 1
    assert admin["max_client_accounts"] == config.TRIAL_MAX_CLIENT_ACCOUNTS == 5
    expected_expiry = (date.today() + timedelta(days=config.TRIAL_LENGTH_DAYS)).isoformat()
    assert admin["expiry_date"] == expected_expiry

    assert len(sent_emails) == 1
    assert sent_emails[0]["email"] == "jane@example.com"
    assert sent_emails[0]["temp_password"]


def test_signup_requires_name_and_valid_email(client, trial_exe_ready, sent_emails):
    resp = _signup(client, name="", email="jane@example.com")
    assert b"Name is required" in resp.data

    resp = _signup(client, email="not-an-email")
    assert b"valid email" in resp.data
    assert sent_emails == []


def test_second_signup_same_email_does_not_create_a_second_trial(
    client, sa_app, trial_exe_ready, sent_emails
):
    _signup(client)
    with sa_app.app_context():
        first = repo.get_admin_by_email(db.get_db(), "jane@example.com")

    sent_emails.clear()
    resp = _signup(client)

    assert b"You have already used the trial from this particular email." in resp.data
    with sa_app.app_context():
        conn = db.get_db()
        trial_admins = [a for a in repo.list_admins(conn) if a["signup_source"] == "trial_signup"]
        second = repo.get_admin(conn, first["id"])

    assert len(trial_admins) == 1  # no duplicate account
    assert second["expiry_date"] == first["expiry_date"]  # clock NOT restarted
    assert second["password_hash"] == first["password_hash"]  # nothing re-issued
    assert sent_emails == []


def test_second_signup_same_email_is_case_insensitive(
    client, sa_app, trial_exe_ready, sent_emails
):
    _signup(client, email="jane@example.com")
    _signup(client, email="JANE@Example.com")

    with sa_app.app_context():
        trial_admins = [
            a for a in repo.list_admins(db.get_db()) if a["signup_source"] == "trial_signup"
        ]
    assert len(trial_admins) == 1


def test_signup_allowed_again_after_trial_admin_was_deleted(
    client, sa_app, trial_exe_ready, sent_emails
):
    """Deleting the Admin is a full reset - the same email can trial again."""
    _signup(client)
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_email(conn, "jane@example.com")
        repo.delete_admin(conn, admin["id"])

    resp = _signup(client)

    assert b"already used the trial from this particular email" not in resp.data
    with sa_app.app_context():
        new_admin = repo.get_admin_by_email(db.get_db(), "jane@example.com")
        assert new_admin is not None
        assert new_admin["id"] != admin["id"]


def test_signup_never_touches_a_non_trial_admins_account(
    client, sa_app, trial_exe_ready, sent_emails
):
    """A stranger typing a paying customer's email into the public form must
    not reset that customer's password or kill their sessions."""
    with sa_app.app_context():
        paid = repo.create_admin(
            db.get_db(),
            name="Paying Customer",
            username="paying",
            password_hash=security.hash_password("Real-Password-1"),
            expiry_date="2099-01-01",
            email="paid@example.com",
        )

    resp = _signup(client, email="paid@example.com")

    assert b"already used the trial from this particular email" in resp.data
    with sa_app.app_context():
        after = repo.get_admin(db.get_db(), paid["id"])
    assert after["password_hash"] == paid["password_hash"]  # untouched
    assert sent_emails == []


def test_signup_refused_for_revoked_trial_admin(client, sa_app, trial_exe_ready, sent_emails):
    _signup(client)
    with sa_app.app_context():
        conn = db.get_db()
        admin = repo.get_admin_by_email(conn, "jane@example.com")
        repo.set_admin_status(conn, admin["id"], "revoked")

    sent_emails.clear()
    resp = _signup(client)

    assert b"already used the trial from this particular email" in resp.data
    assert sent_emails == []


def test_signup_rate_limited_by_ip(client, trial_exe_ready, sent_emails, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS", 2)

    _signup(client, email="a@example.com")
    _signup(client, email="b@example.com")
    resp = _signup(client, email="c@example.com")

    assert b"Too many attempts" in resp.data
    assert len(sent_emails) == 2


def test_signup_works_with_no_admins_and_no_setup(client, sa_app, can_build, sent_emails, build_calls):
    """The owner's actual scenario: a fresh server, zero Admins, nothing
    configured. A prospect must still get an account and a download link,
    and the shared EXE starts building on its own."""
    resp = _signup(client)

    assert b"Your Trial Is Ready" in resp.data
    with sa_app.app_context():
        admins = repo.list_admins(db.get_db())
    assert [a["email"] for a in admins] == ["jane@example.com"]
    assert len(sent_emails) == 1
    assert sent_emails[0]["download_url"].endswith("/trial/download")
    assert any(c["admin_id"] == exe_builder.TRIAL_BUILD_ID for c in build_calls)


def test_signup_declines_only_when_no_exe_exists_and_none_can_be_built(
    client, sa_app, cannot_build, sent_emails
):
    resp = _signup(client)
    assert b"aren&#39;t available right now" in resp.data or b"aren't available right now" in resp.data
    assert sent_emails == []
    with sa_app.app_context():
        assert repo.list_admins(db.get_db()) == []


def test_signup_still_works_on_a_server_that_cannot_build_if_an_exe_exists(
    client, cannot_build, trial_exe_ready, sent_emails
):
    resp = _signup(client)
    assert b"Your Trial Is Ready" in resp.data
    assert len(sent_emails) == 1


def test_opening_the_signup_page_starts_the_trial_build(client, can_build, build_calls):
    client.get("/trial")
    assert build_calls == [{"admin_id": exe_builder.TRIAL_BUILD_ID, "server_url": "http://localhost"}]


def test_signup_page_does_not_rebuild_a_current_exe(client, can_build, trial_exe_ready, build_calls):
    client.get("/trial")
    assert build_calls == []


def test_trial_download_serves_the_shared_exe(client, trial_exe_ready):
    resp = client.get("/trial/download")
    assert resp.status_code == 200
    assert resp.data == b"MZ fake exe"


def test_trial_download_shows_preparing_page_while_building(client, can_build, build_calls):
    resp = client.get("/trial/download")
    assert resp.status_code == 202
    assert b"Preparing Your Download" in resp.data
    assert build_calls[0]["admin_id"] == exe_builder.TRIAL_BUILD_ID


def test_trial_download_rebuilds_when_server_address_changed(client, can_build, build_calls):
    _place_trial_exe(server_url="http://old-server.example")
    resp = client.get("/trial/download")
    assert resp.status_code == 202
    assert build_calls[0]["server_url"] == "http://localhost"


def test_trial_download_falls_back_to_older_exe_when_it_cannot_rebuild(client, cannot_build):
    _place_trial_exe(server_url="http://old-server.example", content=b"MZ older exe")
    resp = client.get("/trial/download")
    assert resp.status_code == 200
    assert resp.data == b"MZ older exe"


def test_trial_download_unavailable_when_nothing_can_be_served(client, cannot_build):
    resp = client.get("/trial/download")
    assert resp.status_code == 503
    assert b"Download Unavailable" in resp.data


def test_trial_signup_page_needs_no_login(client):
    """The whole point - a prospect has no SuperAdmin session."""
    resp = client.get("/trial")
    assert resp.status_code == 200
    assert b"Start Your Free" in resp.data


def test_signup_retries_when_username_is_taken_concurrently(
    client, sa_app, trial_exe_ready, sent_emails, monkeypatch
):
    """Two signups sharing an email local part can pick the same free
    username at once. Simulate losing that race: the first pick is a name
    another request has just inserted. The signup must retry, not 500."""
    from superadmin_panel import trial_signup

    with sa_app.app_context():
        repo.create_admin(
            db.get_db(),
            name="Other John",
            username="john",
            password_hash=security.hash_password("x"),
            expiry_date="2099-01-01",
        )

    real_derive = trial_signup._derive_username
    calls = []

    def stale_first_pick(conn, email):
        calls.append(email)
        return "john" if len(calls) == 1 else real_derive(conn, email)

    monkeypatch.setattr(trial_signup, "_derive_username", stale_first_pick)

    resp = _signup(client, name="John", email="john@example.com")

    assert resp.status_code == 200
    assert b"Your Trial Is Ready" in resp.data
    assert len(calls) == 2
    with sa_app.app_context():
        admin = repo.get_admin_by_email(db.get_db(), "john@example.com")
    assert admin is not None
    assert admin["username"].startswith("john") and admin["username"] != "john"


def test_signup_shows_download_button(client, trial_exe_ready, sent_emails):
    resp = _signup(client)

    assert b"Download StakBridge" in resp.data
    assert sent_emails[0]["download_url"].encode() in resp.data


def test_failed_welcome_email_undoes_the_signup_so_it_can_be_retried(
    client, sa_app, trial_exe_ready, monkeypatch
):
    def _fail(*args, **kwargs):
        raise email_notifications.EmailSendError("SMTP down")

    monkeypatch.setattr(email_notifications, "send_trial_welcome_email", _fail)
    resp = _signup(client)

    assert b"send your login details" in resp.data
    with sa_app.app_context():
        conn = db.get_db()
        assert repo.get_admin_by_email(conn, "jane@example.com") is None
        assert not repo.has_trial_email_been_used(conn, "jane@example.com")

    sent = []
    monkeypatch.setattr(
        email_notifications, "send_trial_welcome_email", lambda admin, **kw: sent.append(admin)
    )
    resp = _signup(client)

    assert b"Your Trial Is Ready" in resp.data
    assert len(sent) == 1


def test_failed_welcome_email_does_not_use_up_the_rate_limit(
    client, sa_app, trial_exe_ready, monkeypatch
):
    monkeypatch.setattr(config, "TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS", 2)

    def _fail(*args, **kwargs):
        raise email_notifications.EmailSendError("SMTP down")

    monkeypatch.setattr(email_notifications, "send_trial_welcome_email", _fail)
    for _ in range(3):
        resp = _signup(client)
        assert b"send your login details" in resp.data

    monkeypatch.setattr(email_notifications, "send_trial_welcome_email", lambda admin, **kw: None)
    resp = _signup(client)
    assert b"Your Trial Is Ready" in resp.data


def test_refused_signups_do_not_use_up_the_rate_limit(client, trial_exe_ready, sent_emails, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS", 2)

    _signup(client, email="a@example.com")
    for _ in range(3):
        resp = _signup(client, email="a@example.com")
        assert b"already used the trial from this particular email" in resp.data
    _signup(client, email="not-an-email")

    resp = _signup(client, email="b@example.com")
    assert b"Your Trial Is Ready" in resp.data
    assert len(sent_emails) == 2


# --------------------------------------------------------------------------
# Email verification code
# --------------------------------------------------------------------------

def _trial_admin_count(sa_app):
    with sa_app.app_context():
        return len([a for a in repo.list_admins(db.get_db()) if a["signup_source"] == "trial_signup"])


def test_no_account_exists_until_the_code_is_entered(client, sa_app, trial_exe_ready, sent_emails, sent_codes):
    resp = _request_code(client)

    assert b"Verify Your Email" in resp.data
    assert len(sent_codes["jane@example.com"]) == 6
    assert _trial_admin_count(sa_app) == 0
    assert sent_emails == []  # no login details before verification

    resp = _verify(client)

    assert b"Your Trial Is Ready" in resp.data
    assert _trial_admin_count(sa_app) == 1
    assert len(sent_emails) == 1


def test_wrong_code_is_rejected_and_counts_down(client, sa_app, trial_exe_ready, sent_emails, sent_codes):
    _request_code(client)
    wrong = "000000" if sent_codes["jane@example.com"] != "000000" else "111111"

    resp = _verify(client, code=wrong)

    assert b"isn&#39;t right" in resp.data
    assert f"{config.TRIAL_CODE_MAX_ATTEMPTS - 1} tries left".encode() in resp.data
    assert _trial_admin_count(sa_app) == 0

    resp = _verify(client)  # the right code still works afterwards
    assert b"Your Trial Is Ready" in resp.data


def test_too_many_wrong_codes_kills_the_code(client, sa_app, trial_exe_ready, sent_emails, sent_codes, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_CODE_MAX_ATTEMPTS", 2)
    _request_code(client)
    real = sent_codes["jane@example.com"]
    wrong = "000000" if real != "000000" else "111111"

    _verify(client, code=wrong)
    resp = _verify(client, code=wrong)
    assert b"Too many incorrect codes" in resp.data

    resp = _verify(client, code=real)  # even the right code is dead now
    assert b"expired" in resp.data
    assert _trial_admin_count(sa_app) == 0


def test_expired_code_is_rejected(client, sa_app, trial_exe_ready, sent_emails, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_CODE_TTL_SECONDS", -1)
    _request_code(client)

    resp = _verify(client)

    assert b"expired" in resp.data
    assert _trial_admin_count(sa_app) == 0


def test_code_for_one_email_does_not_verify_another(client, sa_app, trial_exe_ready, sent_emails, sent_codes):
    _request_code(client, email="jane@example.com")

    resp = _verify(client, email="victim@example.com", code=sent_codes["jane@example.com"])

    assert b"Your Trial Is Ready" not in resp.data
    with sa_app.app_context():
        assert repo.get_admin_by_email(db.get_db(), "victim@example.com") is None


def test_code_is_not_stored_in_plain_text(client, sa_app, trial_exe_ready, sent_codes):
    _request_code(client)
    with sa_app.app_context():
        pending = repo.get_pending_trial_signup(db.get_db(), "jane@example.com")
    assert pending["code_hash"] != sent_codes["jane@example.com"]
    assert sent_codes["jane@example.com"] not in pending["code_hash"]


def test_quick_resend_reuses_the_code_without_another_email(client, trial_exe_ready, sent_codes, monkeypatch):
    calls = []
    monkeypatch.setattr(
        email_notifications, "send_trial_verification_code", lambda **kw: calls.append(kw)
    )
    _request_code(client)
    resp = _request_code(client)

    assert b"Verify Your Email" in resp.data
    assert len(calls) == 1


def test_resend_after_cooldown_sends_a_new_code(client, sa_app, trial_exe_ready, sent_emails, sent_codes, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_CODE_RESEND_COOLDOWN_SECONDS", 0)
    calls = []
    real_capture = email_notifications.send_trial_verification_code

    def _count(**kw):
        calls.append(kw)
        real_capture(**kw)

    monkeypatch.setattr(email_notifications, "send_trial_verification_code", _count)
    _request_code(client)
    _request_code(client)

    assert len(calls) == 2
    resp = _verify(client)  # the latest code works
    assert b"Your Trial Is Ready" in resp.data


def test_already_used_email_gets_no_code(client, sa_app, trial_exe_ready, sent_emails, sent_codes):
    _signup(client)
    sent_codes.clear()

    resp = _request_code(client)

    assert b"You have already used the trial from this particular email." in resp.data
    assert sent_codes == {}


def test_failed_code_email_creates_nothing(client, sa_app, trial_exe_ready, monkeypatch):
    def _fail(**kwargs):
        raise email_notifications.EmailSendError("SMTP down")

    monkeypatch.setattr(email_notifications, "send_trial_verification_code", _fail)
    resp = _request_code(client)

    assert b"send your verification code" in resp.data
    with sa_app.app_context():
        assert repo.get_pending_trial_signup(db.get_db(), "jane@example.com") is None
    assert _trial_admin_count(sa_app) == 0
