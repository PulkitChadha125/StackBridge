"""Outgoing email for the SuperAdmin Control Plane (expiry reminders).

Plain smtplib against whatever SMTP server is configured via env vars (see
config.py) - no third-party mail library, matching this project's general
preference for stdlib over new dependencies where it's a small job.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from . import config

logger = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    pass


def send_email(*, to_addrs: list[str], subject: str, body: str) -> None:
    to_addrs = [addr for addr in dict.fromkeys(a.strip() for a in to_addrs if a and a.strip())]
    if not to_addrs:
        return

    if not config.smtp_is_configured():
        raise EmailSendError(
            "SMTP is not configured - set SMTP_HOST, SMTP_USERNAME, and SMTP_PASSWORD."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM_ADDRESS
    message["To"] = ", ".join(to_addrs)
    message.set_content(body)

    if config.SMTP_SECURITY == "STARTTLS":
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)
    else:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)


def build_expiry_reminder_email(admin: dict[str, Any]) -> tuple[str, str]:
    subject = f"[StakBridge] {admin['name']}'s license expires tomorrow ({admin['expiry_date']})"
    body = (
        f"This is a reminder that the StakBridge license for \"{admin['name']}\" "
        f"(username: {admin['username']}) expires on {admin['expiry_date']} - "
        f"about 24 hours from now.\n\n"
        f"After that date the app will stop working for them once the 72-hour "
        f"offline grace period (if any) runs out.\n\n"
        f"To extend it, open this Admin's page in the SuperAdmin panel and set a "
        f"new expiry date.\n"
    )
    return subject, body


def send_expiry_reminder(admin: dict[str, Any], *, owner_email: str | None) -> None:
    """Emails the Admin (if they have an address on file) and the owner
    (if configured) that this Admin's license expires in ~24 hours.

    Raises EmailSendError if SMTP isn't configured or the send fails - the
    caller decides whether that's fatal (it isn't, for the background loop).
    """
    subject, body = build_expiry_reminder_email(admin)
    recipients = []
    if admin.get("email"):
        recipients.append(admin["email"])
    if owner_email:
        recipients.append(owner_email)

    if not recipients:
        logger.info(
            "Skipping expiry reminder for admin %s: no admin email on file and no owner email configured.",
            admin["id"],
        )
        return

    send_email(to_addrs=recipients, subject=subject, body=body)


def send_trial_verification_code(*, email: str, name: str, code: str) -> None:
    minutes = max(1, config.TRIAL_CODE_TTL_SECONDS // 60)
    send_email(
        to_addrs=[email],
        subject=f"Your StakBridge verification code: {code}",
        body=(
            f"Hi {name},\n\n"
            f"Your verification code is: {code}\n\n"
            f"Enter it on the signup page to start your free trial. "
            f"It expires in {minutes} minutes.\n\n"
            f"If you didn't request this, you can ignore this email.\n"
        ),
    )


def send_password_reset_code(*, email: str, name: str, code: str, where: str) -> None:
    minutes = max(1, config.PASSWORD_RESET_CODE_TTL_SECONDS // 60)
    send_email(
        to_addrs=[email],
        subject=f"Your StakBridge password reset code: {code}",
        body=(
            f"Hi {name},\n\n"
            f"Someone asked to reset the password for your {where} account.\n\n"
            f"Your code is: {code}\n\n"
            f"Enter it on the Forgot Password screen, then choose your new password. "
            f"It expires in {minutes} minutes.\n\n"
            f"If you didn't ask for this, ignore this email - your password stays the same.\n"
        ),
    )


def send_trial_welcome_email(
    admin: dict[str, Any], *, download_url: str, temp_password: str, owner_email: str | None
) -> None:
    """Emails the trial signer their login + download link, and (separately,
    with no password in it) notifies the owner a new trial came in - the
    same "you get a copy" pattern as send_expiry_reminder, split into two
    sends here because the trial user's email carries a real credential
    that has no reason to also sit in the owner's inbox.
    """
    subject = f"Your {config.TRIAL_LENGTH_DAYS}-day StakBridge trial is ready"
    body = (
        f"Hi {admin['name']},\n\n"
        f"Your trial is active until {admin['expiry_date']} ({config.TRIAL_LENGTH_DAYS} days).\n\n"
        f"1. Download the app: {download_url}\n"
        f"2. Log in with:\n"
        f"   Username: {admin['username']}\n"
        f"   Password: {temp_password}\n"
        f"   (you'll be asked to set a new password on first login)\n\n"
        f"Questions? Just reply to this email.\n"
    )
    if admin.get("email"):
        send_email(to_addrs=[admin["email"]], subject=subject, body=body)

    if owner_email:
        owner_subject = f"[StakBridge] New trial signup: {admin['name']}"
        owner_body = (
            f"A new trial account was created automatically:\n\n"
            f"Name: {admin['name']}\n"
            f"Email: {admin.get('email') or '(none)'}\n"
            f"Username: {admin['username']}\n"
            f"Expires: {admin['expiry_date']}\n"
        )
        try:
            send_email(to_addrs=[owner_email], subject=owner_subject, body=owner_body)
        except Exception:
            # The prospect already has their credentials at this point (the
            # send above succeeded) - a failed owner-copy notification is not
            # their problem and must not turn a successful signup into an error.
            logger.exception("Failed to send owner trial-signup notification for admin %s", admin["id"])
