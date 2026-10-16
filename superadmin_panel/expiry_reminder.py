"""Background check: email Admins (and the owner) ~24h before a license
expires. See email_notifications.py for the actual send, repositories.py
for the due-for-reminder query.

Started as a daemon thread from app.py's __main__ guard only - never from
create_app() itself, so importing/testing the app doesn't spin up a thread
that tries to send real email on every test run.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config, db, email_notifications, repositories

logger = logging.getLogger(__name__)


def check_and_send_due_reminders() -> int:
    """Runs one check pass. Returns how many reminder emails were sent.

    Safe to call repeatedly (e.g. every hour) - list_admins_due_for_expiry_reminder
    only returns Admins not already reminded for their *current* expiry_date,
    so re-running this doesn't re-send. A per-admin send failure (e.g. one bad
    address) is logged and does not stop the rest of the batch.
    """
    conn = db.get_connection()
    try:
        due = repositories.list_admins_due_for_expiry_reminder(conn)
        if not due:
            return 0

        owner_email = (
            repositories.get_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
            or config.OWNER_NOTIFICATION_EMAIL
        )

        sent = 0
        for admin in due:
            try:
                email_notifications.send_expiry_reminder(admin, owner_email=owner_email)
                repositories.mark_expiry_reminder_sent(conn, admin["id"], admin["expiry_date"])
                sent += 1
            except Exception:
                logger.exception(
                    "Failed to send expiry reminder for admin %s (%s)", admin["id"], admin.get("name")
                )
        return sent
    finally:
        conn.close()


def run_forever(interval_seconds: int | None = None) -> None:
    interval = interval_seconds or config.EXPIRY_REMINDER_CHECK_INTERVAL_SECONDS
    logger.info("Expiry reminder loop starting (checking every %ss).", interval)
    while True:
        try:
            sent = check_and_send_due_reminders()
            if sent:
                logger.info("Sent %d expiry reminder email(s).", sent)
        except Exception:
            logger.exception("Expiry reminder check pass failed.")
        time.sleep(interval)


_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def start_background_thread(interval_seconds: int | None = None) -> threading.Thread:
    """Idempotent: calling this twice (e.g. create_app() under waitress *and*
    app.py's __main__ block) returns the one running thread rather than
    starting a second loop that would double-send every reminder."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(
            target=run_forever, args=(interval_seconds,), name="expiry-reminder", daemon=True
        )
        _thread.start()
        return _thread
