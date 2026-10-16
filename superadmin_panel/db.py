"""SQLite schema and connection handling for the SuperAdmin Control Plane.

Plain sqlite3 (no ORM), matching this project's existing preference for
lightweight, dependency-free persistence (see order_log_storage.py,
ticket_mapping.py in the Admin app).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import g

PACKAGE_DIR = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    expiry_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    allowed_installations INTEGER NOT NULL DEFAULT 1,
    -- How many client accounts the Admin may save in their app. NULL means
    -- unlimited.
    max_client_accounts INTEGER DEFAULT 5,
    created_at TEXT NOT NULL,
    notes TEXT,
    email TEXT,
    last_expiry_reminder_sent_for TEXT,
    signup_source TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    device_fingerprint TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    UNIQUE (admin_id, device_fingerprint)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telemetry (
    admin_id TEXT PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE,
    device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
    received_at TEXT NOT NULL,
    app_version TEXT,
    copier_status TEXT,
    copier_running INTEGER NOT NULL DEFAULT 0,
    client_count INTEGER NOT NULL DEFAULT 0,
    enabled_client_count INTEGER NOT NULL DEFAULT 0,
    recent_trades_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Cross-admin memory of which devices have already burned a free trial.
-- Deliberately NOT scoped to one admin_id (unlike `devices`) - the whole
-- point is to catch the same machine signing up again under a new email/
-- admin while the first trial Admin still exists. Only ever written for
-- signup_source='trial_signup' admins.
--
-- Deleting an Admin is a full reset: repositories.delete_admin() removes
-- that Admin's rows here and in trial_emails, so the same person/PC can
-- take a new trial afterwards. (The FK stays SET NULL only because SQLite
-- can't alter it in place; init_db purges any NULL leftovers.)
CREATE TABLE IF NOT EXISTS trial_devices (
    fingerprint TEXT PRIMARY KEY,
    admin_id TEXT REFERENCES admins(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL
);

-- Same as trial_devices, for the email side: one trial per email while
-- that trial Admin exists; deleting the Admin re-opens it.
CREATE TABLE IF NOT EXISTS trial_emails (
    email TEXT PRIMARY KEY,
    admin_id TEXT REFERENCES admins(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL
);

-- A trial signup waiting for its emailed verification code. No Admin exists
-- until the code is confirmed, so nobody can claim a trial on an email they
-- don't control. One row per email (lowercased); a resend replaces it.
CREATE TABLE IF NOT EXISTS trial_pending_signups (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0
);

-- A "Forgot password" request waiting for its emailed code (see
-- password_reset.py). account_type is 'admin' or 'superadmin'; admin_id pins
-- an Admin request to the account the code was sent for. Once the code is
-- confirmed, code_hash is cleared and reset_token_hash allows exactly one
-- password change. One row per account_type+email (lowercased).
CREATE TABLE IF NOT EXISTS password_reset_codes (
    account_type TEXT NOT NULL,
    email TEXT NOT NULL,
    admin_id TEXT,
    code_hash TEXT,
    reset_token_hash TEXT,
    sent_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_type, email)
);

-- Tombstones for deleted Admins. Their sessions are gone with them, so this
-- is how a still-installed Admin app learns "you were deleted" (rather than
-- just "session invalid") and erases its local client data.
CREATE TABLE IF NOT EXISTS deleted_admins (
    id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);

-- How many client accounts each of an Admin's servers (devices) currently
-- holds. An Admin's usage against max_client_accounts is the sum over their
-- *active* devices - revoking a dead VPS releases its slots. Client data
-- itself never leaves the Admin's machine; only this count does.
CREATE TABLE IF NOT EXISTS client_usage (
    admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    client_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (admin_id, device_id)
);

-- SuperAdmin asks an Admin's app for its App Log over a date range; the app
-- picks the request up on its next check-in and uploads the lines (the
-- server never connects to the Admin's machine). The file is kept on disk
-- for LOG_UPLOAD_RETENTION_HOURS.
CREATE TABLE IF NOT EXISTS log_requests (
    id TEXT PRIMARY KEY,
    admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    picked_up_at TEXT,
    completed_at TEXT,
    line_count INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_log_requests_device ON log_requests(device_id, status);
CREATE INDEX IF NOT EXISTS idx_devices_admin ON devices(admin_id);
CREATE INDEX IF NOT EXISTS idx_sessions_admin ON sessions(admin_id);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
"""

_db_path_override: Path | None = None


def set_db_path_override(path: Path | str | None) -> None:
    """Test hook: force a specific sqlite file instead of the default."""
    global _db_path_override
    _db_path_override = Path(path) if path is not None else None


def default_db_path() -> Path:
    env_path = os.environ.get("SUPERADMIN_DB_PATH")
    if env_path:
        return Path(env_path)
    return PACKAGE_DIR / "superadmin.db"


def db_path() -> Path:
    return _db_path_override or default_db_path()


def get_connection(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None) -> None:
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate_admins_columns(conn)
        _purge_orphaned_trial_records(conn)
        _seed_client_usage_from_telemetry(conn)
    finally:
        conn.close()


def _migrate_admins_columns(conn: sqlite3.Connection) -> None:
    """Add columns to admins introduced after this table already existed on
    disk - CREATE TABLE IF NOT EXISTS above only affects brand-new DBs."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(admins)").fetchall()}
    if "email" not in existing:
        conn.execute("ALTER TABLE admins ADD COLUMN email TEXT")
    if "last_expiry_reminder_sent_for" not in existing:
        conn.execute("ALTER TABLE admins ADD COLUMN last_expiry_reminder_sent_for TEXT")
    if "signup_source" not in existing:
        conn.execute("ALTER TABLE admins ADD COLUMN signup_source TEXT")
    if "max_client_accounts" not in existing:
        # The DEFAULT fills every existing row, so Admins created before
        # client limits existed start at 5.
        conn.execute("ALTER TABLE admins ADD COLUMN max_client_accounts INTEGER DEFAULT 5")
    conn.commit()


def _purge_orphaned_trial_records(conn: sqlite3.Connection) -> None:
    """Trial rows whose Admin was deleted before deletion became a full
    reset (admin_id was SET NULL instead of the row being removed)."""
    conn.execute("DELETE FROM trial_devices WHERE admin_id IS NULL")
    conn.execute("DELETE FROM trial_emails WHERE admin_id IS NULL")
    conn.commit()


def _seed_client_usage_from_telemetry(conn: sqlite3.Connection) -> None:
    """Upgrading a server that predates client_usage: start each Admin's
    usage from the last client count their app reported, rather than at 0
    until the next check-in. Never overwrites a count already recorded."""
    conn.execute(
        """INSERT OR IGNORE INTO client_usage (admin_id, device_id, client_count, updated_at)
           SELECT t.admin_id, t.device_id, t.client_count, t.received_at
           FROM telemetry t JOIN devices d ON d.id = t.device_id"""
    )
    conn.commit()


def get_db() -> sqlite3.Connection:
    """Per-request connection, reused across repository calls within one request."""
    if "db_conn" not in g:
        g.db_conn = get_connection()
    return g.db_conn


def close_db(_exception=None) -> None:
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()
