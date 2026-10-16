"""Public, unauthenticated self-serve trial signup.

Unlike every other blueprint in this package, routes here require no
SuperAdmin session and no Admin device/token - anyone with the URL can
POST to /trial. That's the point (a prospect signs up with just their
name/email), so the abuse surface is handled deliberately narrow:

- The email is verified first: POST /trial only emails a 6-digit code
  (trial_pending_signups in db.py), and no Admin exists until /trial/verify
  gets the right code - so nobody can claim a trial on someone else's
  email. Codes expire (TRIAL_CODE_TTL_SECONDS), allow a few wrong guesses
  (TRIAL_CODE_MAX_ATTEMPTS) and are stored hashed.
- IP rate-limiting on code emails sent (config.TRIAL_SIGNUP_RATE_LIMIT_*),
  same mechanism auth_api.py/superadmin_web.py already use for login.
- This is a one-time offer per email, permanently - see trial_emails in
  db.py. Any email already used (a live or expired trial, a trial the owner
  has since deleted, or a normal Admin's address) is refused outright and
  no existing account is touched. The only exception is a signup whose
  welcome email could not be sent: it is undone, since that person never
  received the password.
- No new EXE is built per signup (see exe_builder.py: builds are slow and
  serialized). Every trial hands out one shared EXE that the server builds
  for itself (exe_builder.ensure_trial_exe) - no Admin has to exist and the
  owner configures nothing. It is rebuilt automatically when the server
  address changes. Identity comes entirely from the username/password
  issued here, exactly like every other Admin - the binary itself carries
  nothing account-specific.
- The device-fingerprint reuse check that stops "sign up again under a new
  email on the same PC" lives in auth_api.py's login handler, not here -
  it can only run once the prospect has actually installed and logged in.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, render_template, request, send_file, url_for

from . import config, db, email_notifications, exe_builder, rate_limit, repositories, security
from .superadmin_web import delivery_server_url

trial_signup_bp = Blueprint("trial_signup", __name__, url_prefix="/trial")

logger = logging.getLogger(__name__)

_USERNAME_SANITIZE_RE = re.compile(r"[^a-z0-9_.-]+")

# Two signups whose emails share a local part ("john@a.com", "john@b.com")
# can pick the same free username in the same instant; the loser retries.
_USERNAME_CLAIM_ATTEMPTS = 5

ALREADY_USED_MESSAGE = "You have already used the trial from this particular email."
CODE_EXPIRED_MESSAGE = "Your code has expired. Please request a new one."
TOO_MANY_CODES_MESSAGE = "Too many incorrect codes. Please request a new one."


def _derive_username(conn, email: str) -> str:
    local_part = email.split("@", 1)[0].lower()
    base = _USERNAME_SANITIZE_RE.sub("", local_part)[:40] or "trial"

    candidate = base
    while repositories.get_admin_by_username(conn, candidate) is not None:
        candidate = f"{base}{secrets.randbelow(9000) + 1000}"
    return candidate


def _warm_trial_exe() -> None:
    """Get the shared trial EXE building as early as possible, so it is
    usually ready by the time a prospect clicks the link in their email.
    Never lets a build problem break the public page itself."""
    try:
        exe_builder.ensure_trial_exe(delivery_server_url())
    except Exception:
        logger.exception("Could not start the trial EXE build")


def _rate_limit_key() -> str:
    return f"trial_signup:{request.remote_addr}"


def _seconds_since(iso_timestamp: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_timestamp)).total_seconds()


def _resend_wait_seconds(pending) -> int:
    """Seconds until another code may be sent - drives the page's countdown."""
    if pending is None:
        return 0
    left = config.TRIAL_CODE_RESEND_COOLDOWN_SECONDS - _seconds_since(pending["sent_at"])
    return max(0, math.ceil(left))


def _render_verify(name: str, email: str, resend_wait: int, *, resend_too_soon: bool = False):
    return render_template(
        "trial_verify.html",
        name=name,
        email=email,
        resend_wait=resend_wait,
        resend_too_soon=resend_too_soon,
    )


def _signup_refusal(conn, email: str) -> str | None:
    """Why this email can't start a trial right now, or None."""
    _warm_trial_exe()
    build_problems = exe_builder.preflight()
    if build_problems and exe_builder.find_trial_exe() is None:
        # Nothing to hand out and this server can't build one - don't create
        # an account whose download link could never work.
        logger.error("Trial signup refused - trial EXE cannot be built: %s", build_problems[0])
        return "Trials aren't available right now - please contact us directly."

    if (
        repositories.get_admin_by_email(conn, email) is not None
        or repositories.has_trial_email_been_used(conn, email)
    ):
        # Any earlier use blocks - a live or expired trial, a trial since
        # deleted, or a normal Admin's address. One message for all of them,
        # so the form can't be used to probe which emails exist.
        return ALREADY_USED_MESSAGE
    return None


def _create_trial_admin(conn, *, name: str, email: str):
    temp_password = security.generate_temp_password()
    expiry_date = (
        datetime.now(timezone.utc) + timedelta(days=config.TRIAL_LENGTH_DAYS)
    ).date().isoformat()
    password_hash = security.hash_password(temp_password)
    for attempt in range(_USERNAME_CLAIM_ATTEMPTS):
        username = _derive_username(conn, email)
        try:
            admin = repositories.create_admin(
                conn,
                name=name,
                username=username,
                password_hash=password_hash,
                expiry_date=expiry_date,
                allowed_installations=1,
                max_client_accounts=config.TRIAL_MAX_CLIENT_ACCOUNTS,
                email=email,
                signup_source="trial_signup",
                must_change_password=True,
                status="active",
            )
            break
        except sqlite3.IntegrityError as exc:
            # Only a lost race for the username is retryable.
            conn.rollback()
            if "admins.username" not in str(exc) or attempt == _USERNAME_CLAIM_ATTEMPTS - 1:
                raise
    repositories.record_trial_email(conn, email, admin["id"])
    return admin, temp_password


@trial_signup_bp.route("", methods=["GET", "POST"])
def start():
    """Step 1: take name + email, email a verification code."""
    if request.method == "GET":
        _warm_trial_exe()
        return render_template("trial_signup.html", form=None)

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()

    def _rerender(error: str):
        flash(error, "error")
        return render_template("trial_signup.html", form={"name": name, "email": email})

    # Counts verification-code emails sent from this IP - the one thing this
    # public form costs us.
    rate_limit_key = _rate_limit_key()
    if rate_limit.is_rate_limited(
        rate_limit_key,
        max_attempts=config.TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=config.TRIAL_SIGNUP_RATE_LIMIT_WINDOW_SECONDS,
    ):
        return _rerender("Too many attempts. Please try again later.")

    def _refuse(error: str):
        # A refused attempt sends nothing, so it doesn't count.
        rate_limit.forget_last_attempt(rate_limit_key)
        return _rerender(error)

    if not name:
        return _refuse("Name is required.")
    if not email or not security.is_plausible_email(email):
        return _refuse("A valid email address is required.")

    conn = db.get_db()
    refusal = _signup_refusal(conn, email)
    if refusal:
        return _refuse(refusal)

    pending = repositories.get_pending_trial_signup(conn, email)
    wait = _resend_wait_seconds(pending)
    if wait > 0:
        # Double-click or impatient resend - the code just sent is still good.
        # Nothing is sent, so say so rather than implying a new email went out.
        # The notice is rendered by the template so its countdown can tick live.
        rate_limit.forget_last_attempt(rate_limit_key)
        return _render_verify(name, email, wait, resend_too_soon=True)

    code = f"{secrets.randbelow(10**6):06d}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=config.TRIAL_CODE_TTL_SECONDS)
    ).isoformat()
    repositories.save_pending_trial_signup(
        conn, email=email, name=name, code_hash=security.hash_password(code), expires_at=expires_at
    )
    try:
        email_notifications.send_trial_verification_code(email=email, name=name, code=code)
    except Exception:
        logger.exception("Failed to send trial verification code to %s", email)
        repositories.delete_pending_trial_signup(conn, email)
        return _refuse(
            "We couldn't send your verification code right now. Please try again in a few minutes."
        )

    flash(f"We've sent a 6-digit code to {email}.", "success")
    return _render_verify(name, email, config.TRIAL_CODE_RESEND_COOLDOWN_SECONDS)


@trial_signup_bp.route("/verify", methods=["POST"])
def verify():
    """Step 2: check the code; only now is the trial Admin created."""
    email = (request.form.get("email") or "").strip()
    code = (request.form.get("code") or "").strip()
    conn = db.get_db()
    pending = repositories.get_pending_trial_signup(conn, email) if email else None

    def _start_over(error: str):
        flash(error, "error")
        form = {"name": pending["name"] if pending else "", "email": email}
        return render_template("trial_signup.html", form=form)

    if pending is None:
        return _start_over(CODE_EXPIRED_MESSAGE)
    name = pending["name"]
    if datetime.fromisoformat(pending["expires_at"]) < datetime.now(timezone.utc):
        repositories.delete_pending_trial_signup(conn, email)
        return _start_over(CODE_EXPIRED_MESSAGE)
    if pending["failed_attempts"] >= config.TRIAL_CODE_MAX_ATTEMPTS:
        repositories.delete_pending_trial_signup(conn, email)
        return _start_over(TOO_MANY_CODES_MESSAGE)

    if not security.verify_password(code, pending["code_hash"]):
        repositories.record_failed_trial_code(conn, email)
        left = config.TRIAL_CODE_MAX_ATTEMPTS - pending["failed_attempts"] - 1
        if left <= 0:
            repositories.delete_pending_trial_signup(conn, email)
            return _start_over(TOO_MANY_CODES_MESSAGE)
        flash(f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.", "error")
        return _render_verify(name, email, _resend_wait_seconds(pending))

    repositories.delete_pending_trial_signup(conn, email)

    # Checked again: the email, or this server's ability to hand out an EXE,
    # may have changed while the code sat in their inbox.
    refusal = _signup_refusal(conn, email)
    if refusal:
        return _start_over(refusal)

    admin, temp_password = _create_trial_admin(conn, name=name, email=email)

    owner_email = repositories.get_setting(
        conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL
    ) or config.OWNER_NOTIFICATION_EMAIL
    download_url = url_for("trial_signup.download_trial_exe", _external=True)

    try:
        email_notifications.send_trial_welcome_email(
            admin,
            download_url=download_url,
            temp_password=temp_password,
            owner_email=owner_email,
        )
    except Exception:
        logger.exception("Failed to send trial welcome email for admin %s", admin["id"])
        # The password only ever travels in that email, so an account whose
        # email didn't go out is unusable - undo it, or the one-trial-per-email
        # rule would lock this person out of a trial they never received.
        repositories.delete_admin(conn, admin["id"])
        repositories.delete_trial_email(conn, email)
        # Our failure, not theirs - give back the code email it cost them.
        rate_limit.forget_last_attempt(_rate_limit_key())
        return _start_over(
            "We couldn't send your login details right now. Please try again in a few minutes."
        )

    return render_template("trial_signup_thanks.html", email=email, download_url=download_url)


@trial_signup_bp.route("/download")
def download_trial_exe():
    server_url = delivery_server_url()
    path = exe_builder.find_trial_exe(server_url)
    if path is None:
        job = exe_builder.ensure_trial_exe(server_url)
        if job is not None:
            # First download after a fresh install or an address change -
            # the build takes a few minutes; this page refreshes itself.
            return render_template("trial_download_preparing.html"), 202
        # Can't produce a current build right now; an EXE built for an
        # earlier address beats turning the prospect away.
        path = exe_builder.find_trial_exe()
    if path is None:
        return render_template("trial_download_unavailable.html"), 503
    return send_file(str(path), as_attachment=True, download_name=path.name)
