"""Data-access functions for admins/devices/sessions.

Every function takes an open sqlite3.Connection as its first argument
(dependency injection) - callers are responsible for obtaining one (either
db.get_db() inside a Flask request, or db.get_connection() in scripts/tests)
and committing/closing it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# Admins
# --------------------------------------------------------------------------

def create_admin(
    conn: sqlite3.Connection,
    *,
    name: str,
    username: str,
    password_hash: str,
    expiry_date: str,
    allowed_installations: int = 1,
    max_client_accounts: int | None = 5,
    notes: str | None = None,
    email: str | None = None,
    signup_source: str | None = None,
    must_change_password: bool = True,
    status: str = "active",
) -> dict[str, Any]:
    admin_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO admins
            (id, name, username, password_hash, must_change_password,
             expiry_date, status, allowed_installations, max_client_accounts,
             created_at, notes, email, signup_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            admin_id,
            name,
            username,
            password_hash,
            int(must_change_password),
            expiry_date,
            status,
            allowed_installations,
            max_client_accounts,
            now_iso(),
            notes,
            email,
            signup_source,
        ),
    )
    conn.commit()
    return get_admin(conn, admin_id)


def get_admin(conn: sqlite3.Connection, admin_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
    return _row_to_dict(row)


def get_admin_by_username(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
    return _row_to_dict(row)


def get_admin_by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    """Case-insensitive - prospects retyping their email shouldn't get a
    second trial just because they capitalized it differently this time."""
    row = conn.execute(
        "SELECT * FROM admins WHERE email IS NOT NULL AND LOWER(email) = LOWER(?) ORDER BY created_at ASC LIMIT 1",
        (email,),
    ).fetchone()
    return _row_to_dict(row)


def list_admins(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM admins ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


_UPDATABLE_ADMIN_FIELDS = {
    "name",
    "expiry_date",
    "status",
    "allowed_installations",
    "max_client_accounts",
    "notes",
    "email",
    "last_expiry_reminder_sent_for",
}


def update_admin(conn: sqlite3.Connection, admin_id: str, **fields: Any) -> dict[str, Any] | None:
    unknown = set(fields) - _UPDATABLE_ADMIN_FIELDS
    if unknown:
        raise ValueError(f"Cannot update fields: {sorted(unknown)}")
    if not fields:
        return get_admin(conn, admin_id)

    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [admin_id]
    conn.execute(f"UPDATE admins SET {assignments} WHERE id = ?", values)
    conn.commit()
    return get_admin(conn, admin_id)


def set_admin_password(
    conn: sqlite3.Connection, admin_id: str, password_hash: str, *, must_change_password: bool
) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE admins SET password_hash = ?, must_change_password = ? WHERE id = ?",
        (password_hash, int(must_change_password), admin_id),
    )
    conn.commit()
    return get_admin(conn, admin_id)


def clear_must_change_password(conn: sqlite3.Connection, admin_id: str) -> None:
    conn.execute("UPDATE admins SET must_change_password = 0 WHERE id = ?", (admin_id,))
    conn.commit()


def set_admin_status(conn: sqlite3.Connection, admin_id: str, status: str) -> dict[str, Any] | None:
    if status not in ("active", "revoked"):
        raise ValueError("status must be 'active' or 'revoked'")
    return update_admin(conn, admin_id, status=status)


def is_admin_expired(admin: dict[str, Any], *, as_of: datetime | None = None) -> bool:
    as_of = as_of or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(admin["expiry_date"])
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return as_of.date() > expiry.date()


def delete_admin(conn: sqlite3.Connection, admin_id: str) -> None:
    """Full reset of the Admin. devices/sessions/telemetry cascade via FK
    (ON DELETE CASCADE, PRAGMA foreign_keys = ON - see db.get_connection()).

    Their trial records go too, so the same email/PC can take a new trial.

    Client data (their trading clients, order logs, ticket mappings) lives on
    the Admin's own machine under %LOCALAPPDATA%. The tombstone written here
    tells that app, the next time it checks in, that this Admin was deleted,
    and the app then erases that Admin's local folder itself.
    """
    conn.execute("DELETE FROM trial_devices WHERE admin_id = ?", (admin_id,))
    conn.execute("DELETE FROM trial_emails WHERE admin_id = ?", (admin_id,))
    conn.execute(
        "INSERT OR REPLACE INTO deleted_admins (id, deleted_at) VALUES (?, ?)",
        (admin_id, now_iso()),
    )
    conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
    conn.commit()


def is_admin_deleted(conn: sqlite3.Connection, admin_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM deleted_admins WHERE id = ?", (admin_id,)).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Expiry email reminders
# --------------------------------------------------------------------------

def list_admins_due_for_expiry_reminder(
    conn: sqlite3.Connection, *, as_of: datetime | None = None
) -> list[dict[str, Any]]:
    """Admins whose expiry is exactly one day (~24h) away and who have not
    already gotten a reminder for that specific expiry_date.

    Comparing against last_expiry_reminder_sent_for (rather than a boolean
    flag) means changing expiry_date - e.g. reactivating a returning Admin -
    automatically re-arms the reminder for the new date without any extra
    reset step.
    """
    as_of = as_of or datetime.now(timezone.utc)
    target_date = (as_of.date() + timedelta(days=1)).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM admins
        WHERE status = 'active'
          AND expiry_date = ?
          AND (last_expiry_reminder_sent_for IS NULL OR last_expiry_reminder_sent_for != expiry_date)
        """,
        (target_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_expiry_reminder_sent(conn: sqlite3.Connection, admin_id: str, expiry_date: str) -> None:
    conn.execute(
        "UPDATE admins SET last_expiry_reminder_sent_for = ? WHERE id = ?",
        (expiry_date, admin_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

def get_device(conn: sqlite3.Connection, device_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _row_to_dict(row)


def get_device_by_fingerprint(
    conn: sqlite3.Connection, admin_id: str, fingerprint: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM devices WHERE admin_id = ? AND device_fingerprint = ?",
        (admin_id, fingerprint),
    ).fetchone()
    return _row_to_dict(row)


def list_devices(conn: sqlite3.Connection, admin_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM devices WHERE admin_id = ? ORDER BY first_seen_at ASC", (admin_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def count_active_devices(conn: sqlite3.Connection, admin_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM devices WHERE admin_id = ? AND status = 'active'",
        (admin_id,),
    ).fetchone()
    return int(row["n"])


def register_or_touch_device(
    conn: sqlite3.Connection, admin_id: str, fingerprint: str
) -> dict[str, Any]:
    """Insert a new device row, or bump last_seen_at on an existing one.

    Caller must check the installation limit (count_active_devices vs.
    allowed_installations) *before* calling this for a brand-new fingerprint.
    """
    existing = get_device_by_fingerprint(conn, admin_id, fingerprint)
    if existing is not None:
        conn.execute(
            "UPDATE devices SET last_seen_at = ? WHERE id = ?",
            (now_iso(), existing["id"]),
        )
        conn.commit()
        return get_device(conn, existing["id"])

    device_id = str(uuid.uuid4())
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO devices (id, admin_id, device_fingerprint, first_seen_at, last_seen_at, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        (device_id, admin_id, fingerprint, timestamp, timestamp),
    )
    conn.commit()
    return get_device(conn, device_id)


def revoke_device(conn: sqlite3.Connection, device_id: str) -> dict[str, Any] | None:
    conn.execute("UPDATE devices SET status = 'revoked' WHERE id = ?", (device_id,))
    # A revoked device's sessions must die immediately, not linger until expiry.
    conn.execute(
        "UPDATE sessions SET revoked = 1 WHERE device_id = ? AND revoked = 0", (device_id,)
    )
    conn.commit()
    return get_device(conn, device_id)


def reactivate_device(conn: sqlite3.Connection, device_id: str) -> dict[str, Any] | None:
    conn.execute("UPDATE devices SET status = 'active' WHERE id = ?", (device_id,))
    conn.commit()
    return get_device(conn, device_id)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def create_session(
    conn: sqlite3.Connection,
    *,
    admin_id: str,
    device_id: str,
    token_hash: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    conn.execute(
        """
        INSERT INTO sessions (id, token_hash, admin_id, device_id, issued_at, expires_at, revoked)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (session_id, token_hash, admin_id, device_id, issued_at.isoformat(), expires_at.isoformat()),
    )
    conn.commit()
    return get_session(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_dict(row)


def get_session_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    return _row_to_dict(row)


def is_session_valid(session_row: dict[str, Any], *, as_of: datetime | None = None) -> bool:
    if session_row["revoked"]:
        return False
    as_of = as_of or datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(session_row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return as_of < expires_at


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,))
    conn.commit()


def revoke_all_sessions_for_admin(conn: sqlite3.Connection, admin_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET revoked = 1 WHERE admin_id = ? AND revoked = 0", (admin_id,)
    )
    conn.commit()


def extend_session(conn: sqlite3.Connection, session_id: str, ttl_seconds: int) -> dict[str, Any] | None:
    new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (new_expiry, session_id))
    conn.commit()
    return get_session(conn, session_id)


# --------------------------------------------------------------------------
# Telemetry (Phase 10) - one row per Admin, always the latest snapshot.
# Push model: the Admin app uploads this periodically; the SuperAdmin never
# reaches into the Admin's machine to pull it.
# --------------------------------------------------------------------------

def _telemetry_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["recent_trades"] = json.loads(result.pop("recent_trades_json") or "[]")
    result["copier_running"] = bool(result["copier_running"])
    return result


def upsert_telemetry(
    conn: sqlite3.Connection,
    *,
    admin_id: str,
    device_id: str | None,
    app_version: str,
    copier_status: str,
    copier_running: bool,
    client_count: int,
    enabled_client_count: int,
    recent_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    received_at = now_iso()
    conn.execute(
        """
        INSERT INTO telemetry
            (admin_id, device_id, received_at, app_version, copier_status,
             copier_running, client_count, enabled_client_count, recent_trades_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(admin_id) DO UPDATE SET
            device_id = excluded.device_id,
            received_at = excluded.received_at,
            app_version = excluded.app_version,
            copier_status = excluded.copier_status,
            copier_running = excluded.copier_running,
            client_count = excluded.client_count,
            enabled_client_count = excluded.enabled_client_count,
            recent_trades_json = excluded.recent_trades_json
        """,
        (
            admin_id,
            device_id,
            received_at,
            app_version,
            copier_status,
            int(copier_running),
            client_count,
            enabled_client_count,
            json.dumps(recent_trades),
        ),
    )
    conn.commit()
    return get_telemetry(conn, admin_id)


def get_telemetry(conn: sqlite3.Connection, admin_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM telemetry WHERE admin_id = ?", (admin_id,)).fetchone()
    return _telemetry_row_to_dict(row)


def list_all_telemetry(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM telemetry ORDER BY received_at DESC").fetchall()
    return [_telemetry_row_to_dict(row) for row in rows]


# --------------------------------------------------------------------------
# Client usage - how many client accounts each of an Admin's servers holds.
# The Admin's total is summed over *active* devices only, so revoking a
# device releases that server's slots.
# --------------------------------------------------------------------------

def set_client_usage(conn: sqlite3.Connection, admin_id: str, device_id: str, client_count: int) -> None:
    conn.execute(
        """INSERT INTO client_usage (admin_id, device_id, client_count, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(admin_id, device_id) DO UPDATE SET
               client_count = excluded.client_count,
               updated_at = excluded.updated_at""",
        (admin_id, device_id, max(0, int(client_count)), now_iso()),
    )
    conn.commit()


def total_client_usage(
    conn: sqlite3.Connection, admin_id: str, *, exclude_device_id: str | None = None
) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(u.client_count), 0) AS n
           FROM client_usage u JOIN devices d ON d.id = u.device_id
           WHERE u.admin_id = ? AND d.status = 'active' AND u.device_id IS NOT ?""",
        (admin_id, exclude_device_id),
    ).fetchone()
    return int(row["n"])


def client_usage_by_device(conn: sqlite3.Connection, admin_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT device_id, client_count, updated_at FROM client_usage WHERE admin_id = ?",
        (admin_id,),
    ).fetchall()
    return {row["device_id"]: dict(row) for row in rows}


def client_usage_summaries(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per Admin: {"total": int, "updated_at": newest report} - for the dashboard."""
    rows = conn.execute(
        """SELECT u.admin_id, SUM(u.client_count) AS total, MAX(u.updated_at) AS updated_at
           FROM client_usage u JOIN devices d ON d.id = u.device_id
           WHERE d.status = 'active'
           GROUP BY u.admin_id"""
    ).fetchall()
    return {row["admin_id"]: {"total": int(row["total"]), "updated_at": row["updated_at"]} for row in rows}


def reserve_client_slots(
    conn: sqlite3.Connection,
    *,
    admin_id: str,
    device_id: str,
    new_count: int,
    limit: int | None,
) -> tuple[bool, int]:
    """One server wants to hold ``new_count`` clients. Allowed when the
    Admin is unlimited, when this server isn't growing, or when the new
    total across all servers stays within ``limit``; an allowed count is
    recorded straight away. Returns (allowed, total_after).

    BEGIN IMMEDIATE takes SQLite's write lock before reading, so two of the
    Admin's servers adding at the same moment can't both squeeze past the
    limit on the same stale total."""
    new_count = max(0, int(new_count))
    conn.commit()  # close any implicit transaction before taking the lock
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT client_count FROM client_usage WHERE admin_id = ? AND device_id = ?",
            (admin_id, device_id),
        ).fetchone()
        current = int(row["client_count"]) if row else 0
        others = total_client_usage(conn, admin_id, exclude_device_id=device_id)
        allowed = limit is None or new_count <= current or others + new_count <= limit
        if allowed:
            conn.execute(
                """INSERT INTO client_usage (admin_id, device_id, client_count, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(admin_id, device_id) DO UPDATE SET
                       client_count = excluded.client_count,
                       updated_at = excluded.updated_at""",
                (admin_id, device_id, new_count, now_iso()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return allowed, others + (new_count if allowed else current)


# --------------------------------------------------------------------------
# Settings (simple key/value, used for delivery configuration)
# --------------------------------------------------------------------------

SETTING_PUBLIC_SERVER_URL = "public_server_url"
SETTING_OWNER_NOTIFICATION_EMAIL = "owner_notification_email"
SETTING_SUPPORT_EMAIL = "support_email"
SETTING_SUPPORT_WHATSAPP_URL = "support_whatsapp_url"


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()


# --------------------------------------------------------------------------
# Trial devices - one free trial per machine, independent of which Admin
# (email) it's attached to. See db.py's trial_devices table comment for why
# admin_id is nullable/ON DELETE SET NULL rather than cascading.
# --------------------------------------------------------------------------

def has_trial_device_been_used(conn: sqlite3.Connection, fingerprint: str) -> bool:
    """Whether this machine has a trial on a still-existing Admin -
    delete_admin() removes the row, re-opening the trial."""
    row = conn.execute(
        "SELECT 1 FROM trial_devices WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return row is not None


def get_trial_device_owner(conn: sqlite3.Connection, fingerprint: str) -> str | None:
    """The admin_id that used this device for a trial, or None if none."""
    row = conn.execute(
        "SELECT admin_id FROM trial_devices WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return row["admin_id"] if row else None


def record_trial_device(conn: sqlite3.Connection, fingerprint: str, admin_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO trial_devices (fingerprint, admin_id, first_seen_at) VALUES (?, ?, ?)",
        (fingerprint, admin_id, now_iso()),
    )
    conn.commit()


def list_trial_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every PC that has used a free trial, newest first, with the trial
    Admin it belongs to - for the Settings page."""
    rows = conn.execute(
        """SELECT t.fingerprint, t.first_seen_at, t.admin_id,
                  a.name AS admin_name, a.username AS admin_username, a.email AS admin_email
           FROM trial_devices t LEFT JOIN admins a ON a.id = t.admin_id
           ORDER BY t.first_seen_at DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def delete_trial_device(conn: sqlite3.Connection, fingerprint: str) -> bool:
    """Let this PC take another free trial (testing, or a goodwill second
    chance). The trial Admin it belonged to is untouched. Returns whether a
    record was removed."""
    cursor = conn.execute("DELETE FROM trial_devices WHERE fingerprint = ?", (fingerprint,))
    conn.commit()
    return cursor.rowcount > 0


def has_trial_email_been_used(conn: sqlite3.Connection, email: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM trial_emails WHERE LOWER(email) = LOWER(?)", (email,)
    ).fetchone()
    return row is not None


def record_trial_email(conn: sqlite3.Connection, email: str, admin_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO trial_emails (email, admin_id, first_seen_at) VALUES (?, ?, ?)",
        (email.lower(), admin_id, now_iso()),
    )
    conn.commit()


def delete_trial_email(conn: sqlite3.Connection, email: str) -> None:
    """For undoing a signup that never completed (its welcome email could
    not be sent). Deleting an Admin removes its record via delete_admin()."""
    conn.execute("DELETE FROM trial_emails WHERE LOWER(email) = LOWER(?)", (email,))
    conn.commit()


# --------------------------------------------------------------------------
# Pending trial signups - waiting for the emailed verification code.
# --------------------------------------------------------------------------

def save_pending_trial_signup(
    conn: sqlite3.Connection, *, email: str, name: str, code_hash: str, expires_at: str
) -> None:
    """Create or replace (on resend) the pending signup for this email,
    resetting its failed-attempt count along with the new code."""
    conn.execute(
        "INSERT OR REPLACE INTO trial_pending_signups "
        "(email, name, code_hash, sent_at, expires_at, failed_attempts) VALUES (?, ?, ?, ?, ?, 0)",
        (email.lower(), name, code_hash, now_iso(), expires_at),
    )
    conn.commit()


def get_pending_trial_signup(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM trial_pending_signups WHERE email = ?", (email.lower(),)
    ).fetchone()
    return dict(row) if row else None


def record_failed_trial_code(conn: sqlite3.Connection, email: str) -> None:
    conn.execute(
        "UPDATE trial_pending_signups SET failed_attempts = failed_attempts + 1 WHERE email = ?",
        (email.lower(),),
    )
    conn.commit()


def delete_pending_trial_signup(conn: sqlite3.Connection, email: str) -> None:
    conn.execute("DELETE FROM trial_pending_signups WHERE email = ?", (email.lower(),))
    conn.commit()


def count_admins_by_email(conn: sqlite3.Connection, email: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM admins WHERE email IS NOT NULL AND LOWER(email) = LOWER(?)", (email,)
    ).fetchone()
    return row[0]


# --------------------------------------------------------------------------
# "Forgot password" codes (see password_reset.py)
# --------------------------------------------------------------------------


def save_password_reset_code(
    conn: sqlite3.Connection,
    *,
    account_type: str,
    email: str,
    admin_id: str | None,
    code_hash: str,
    expires_at: str,
) -> None:
    """Create or replace (on resend) the request for this account, resetting
    its failed-attempt count and any earlier confirmed code."""
    conn.execute(
        "INSERT OR REPLACE INTO password_reset_codes "
        "(account_type, email, admin_id, code_hash, reset_token_hash, sent_at, expires_at, failed_attempts) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, 0)",
        (account_type, email.lower(), admin_id, code_hash, now_iso(), expires_at),
    )
    conn.commit()


def get_password_reset_code(conn: sqlite3.Connection, account_type: str, email: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM password_reset_codes WHERE account_type = ? AND email = ?",
        (account_type, email.lower()),
    ).fetchone()
    return dict(row) if row else None


def record_failed_password_reset_code(conn: sqlite3.Connection, account_type: str, email: str) -> None:
    conn.execute(
        "UPDATE password_reset_codes SET failed_attempts = failed_attempts + 1 "
        "WHERE account_type = ? AND email = ?",
        (account_type, email.lower()),
    )
    conn.commit()


def mark_password_reset_code_confirmed(
    conn: sqlite3.Connection, account_type: str, email: str, *, reset_token_hash: str, expires_at: str
) -> None:
    """The code was right: it can't be used again, and the reset token now
    allows one password change until expires_at."""
    conn.execute(
        "UPDATE password_reset_codes SET code_hash = NULL, reset_token_hash = ?, expires_at = ? "
        "WHERE account_type = ? AND email = ?",
        (reset_token_hash, expires_at, account_type, email.lower()),
    )
    conn.commit()


def delete_password_reset_code(conn: sqlite3.Connection, account_type: str, email: str) -> None:
    conn.execute(
        "DELETE FROM password_reset_codes WHERE account_type = ? AND email = ?",
        (account_type, email.lower()),
    )
    conn.commit()
