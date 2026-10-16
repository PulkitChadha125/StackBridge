"""Forgot password by emailed code, for both the SuperAdmin and Admins.

Three steps, the same for both account types:

1. request_code(email)  - emails a 6-digit code if the email belongs to an
   account. Unknown emails get the same answer as known ones, so the form
   can't be used to find out which emails have accounts.
2. verify_code(email, code) - a right code is used up and swapped for a
   one-time reset token; wrong guesses are limited.
3. complete_reset(email, token, new, confirm) - sets the new password. For
   an Admin every app session is logged out too.

Which email belongs to which account:

- An Admin: the email on their Admin page (admins.email). If several
  Admins share one email, no code is sent - the SuperAdmin must reset it.
- The SuperAdmin: the "Your notification email" on the Settings page (or
  the OWNER_NOTIFICATION_EMAIL env var). Their login lives in
  superadmin_config.json; one supplied by env vars can't be rewritten here.

Routes live in superadmin_web.py (SuperAdmin pages) and auth_api.py (the
JSON API the Admin app calls).
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import datetime, timedelta, timezone

from . import config, email_notifications, repositories, security

logger = logging.getLogger(__name__)

ADMIN = "admin"
SUPERADMIN = "superadmin"

SENT_MESSAGE = (
    "If an account uses this email, a 6-digit code has been sent to it. "
    "Check your inbox (and spam folder)."
)
CODE_EXPIRED_MESSAGE = "Your code has expired. Please request a new one."
TOO_MANY_CODES_MESSAGE = "Too many incorrect codes. Please request a new one."
RESET_EXPIRED_MESSAGE = "This password reset has expired. Please start again."


class ResetError(Exception):
    """Shown to the user as-is. `retryable` errors keep them on the same step."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at() -> str:
    return (_now() + timedelta(seconds=config.PASSWORD_RESET_CODE_TTL_SECONDS)).isoformat()


def _is_expired(row) -> bool:
    return datetime.fromisoformat(row["expires_at"]) < _now()


def resend_wait_seconds(row) -> int:
    if row is None:
        return 0
    elapsed = (_now() - datetime.fromisoformat(row["sent_at"])).total_seconds()
    return max(0, math.ceil(config.PASSWORD_RESET_RESEND_COOLDOWN_SECONDS - elapsed))


def superadmin_recovery_email(conn) -> str | None:
    return (
        repositories.get_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
        or config.OWNER_NOTIFICATION_EMAIL
    )


def _find_account(conn, account_type: str, email: str) -> dict | None:
    """{"admin_id", "name", "where"} for the account this email resets, or None."""
    if account_type == SUPERADMIN:
        recovery = superadmin_recovery_email(conn)
        creds = config.load_superadmin_credentials()
        if not recovery or creds is None or recovery.strip().lower() != email.lower():
            return None
        return {"admin_id": None, "name": creds[0], "where": "StakBridge SuperAdmin panel"}

    admin = repositories.get_admin_by_email(conn, email)
    if admin is None:
        return None
    if repositories.count_admins_by_email(conn, email) > 1:
        raise ResetError(
            "More than one account uses this email, so it can't be reset here. "
            "Please ask your provider to reset your password."
        )
    return {"admin_id": admin["id"], "name": admin["name"], "where": "StakBridge"}


def request_code(conn, account_type: str, email: str) -> int:
    """Step 1. Returns seconds until another code may be requested."""
    email = (email or "").strip()
    if not email or not security.is_plausible_email(email):
        raise ResetError("Please enter a valid email address.", retryable=True)
    if not config.smtp_is_configured():
        raise ResetError(
            "Email is not set up on the server, so a code can't be sent. "
            "Please contact your provider."
        )

    account = _find_account(conn, account_type, email)
    if account is None:
        logger.info("Password reset requested for unknown %s email %s", account_type, email)
        return config.PASSWORD_RESET_RESEND_COOLDOWN_SECONDS

    existing = repositories.get_password_reset_code(conn, account_type, email)
    wait = resend_wait_seconds(existing)
    if wait > 0:
        # Double-click or impatient resend - the code just sent still works.
        return wait

    code = f"{secrets.randbelow(10**6):06d}"
    repositories.save_password_reset_code(
        conn,
        account_type=account_type,
        email=email,
        admin_id=account["admin_id"],
        code_hash=security.hash_password(code),
        expires_at=_expires_at(),
    )
    try:
        email_notifications.send_password_reset_code(
            email=email, name=account["name"], code=code, where=account["where"]
        )
    except Exception:
        logger.exception("Failed to send password reset code to %s", email)
        repositories.delete_password_reset_code(conn, account_type, email)
        raise ResetError("We couldn't send the code right now. Please try again in a few minutes.")
    return config.PASSWORD_RESET_RESEND_COOLDOWN_SECONDS


def verify_code(conn, account_type: str, email: str, code: str) -> str:
    """Step 2. Returns the one-time reset token for complete_reset()."""
    email = (email or "").strip()
    code = (code or "").strip()
    row = repositories.get_password_reset_code(conn, account_type, email) if email else None
    if row is None or not row["code_hash"]:
        raise ResetError(CODE_EXPIRED_MESSAGE)
    if _is_expired(row):
        repositories.delete_password_reset_code(conn, account_type, email)
        raise ResetError(CODE_EXPIRED_MESSAGE)
    if row["failed_attempts"] >= config.PASSWORD_RESET_CODE_MAX_ATTEMPTS:
        repositories.delete_password_reset_code(conn, account_type, email)
        raise ResetError(TOO_MANY_CODES_MESSAGE)

    if not code or not security.verify_password(code, row["code_hash"]):
        repositories.record_failed_password_reset_code(conn, account_type, email)
        left = config.PASSWORD_RESET_CODE_MAX_ATTEMPTS - row["failed_attempts"] - 1
        if left <= 0:
            repositories.delete_password_reset_code(conn, account_type, email)
            raise ResetError(TOO_MANY_CODES_MESSAGE)
        raise ResetError(
            f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.", retryable=True
        )

    token = security.generate_token()
    repositories.mark_password_reset_code_confirmed(
        conn, account_type, email, reset_token_hash=security.hash_token(token), expires_at=_expires_at()
    )
    return token


def complete_reset(
    conn, account_type: str, email: str, reset_token: str, new_password: str, confirm_password: str
) -> None:
    """Step 3. Sets the new password (and ends every app session, for an Admin)."""
    email = (email or "").strip()
    row = repositories.get_password_reset_code(conn, account_type, email) if email else None
    if (
        row is None
        or not row["reset_token_hash"]
        or not reset_token
        or not security.constant_time_eq(security.hash_token(reset_token), row["reset_token_hash"])
    ):
        raise ResetError(RESET_EXPIRED_MESSAGE)
    if _is_expired(row):
        repositories.delete_password_reset_code(conn, account_type, email)
        raise ResetError(RESET_EXPIRED_MESSAGE)

    if len(new_password or "") < 8:
        raise ResetError("New password must be at least 8 characters.", retryable=True)
    if new_password != confirm_password:
        raise ResetError("New password and confirmation do not match.", retryable=True)

    password_hash = security.hash_password(new_password)
    if account_type == SUPERADMIN:
        creds = config.load_superadmin_credentials()
        recovery = superadmin_recovery_email(conn)
        if creds is None or not recovery or recovery.strip().lower() != email.lower():
            repositories.delete_password_reset_code(conn, account_type, email)
            raise ResetError(RESET_EXPIRED_MESSAGE)
        if config.superadmin_credentials_from_env():
            raise ResetError(
                "This server takes the SuperAdmin login from the SUPERADMIN_USERNAME / "
                "SUPERADMIN_PASSWORD_HASH environment variables, so it can't be changed here. "
                "Update those variables on the server instead."
            )
        config.save_superadmin_credentials(creds[0], password_hash)
    else:
        admin = repositories.get_admin(conn, row["admin_id"]) if row["admin_id"] else None
        # The Admin may have been deleted, or their email changed, since the code went out.
        if admin is None or (admin["email"] or "").strip().lower() != email.lower():
            repositories.delete_password_reset_code(conn, account_type, email)
            raise ResetError(RESET_EXPIRED_MESSAGE)
        repositories.set_admin_password(conn, admin["id"], password_hash, must_change_password=False)
        repositories.revoke_all_sessions_for_admin(conn, admin["id"])

    repositories.delete_password_reset_code(conn, account_type, email)
