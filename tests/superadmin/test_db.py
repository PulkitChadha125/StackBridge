import sqlite3

import pytest

from superadmin_panel import db


def test_init_db_creates_all_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"admins", "devices", "sessions"} <= tables


def test_init_db_is_idempotent(conn):
    db.init_db()
    db.init_db()  # must not raise


def test_admin_username_unique_constraint(conn):
    conn.execute(
        "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at) "
        "VALUES ('a1','A','dup','hash','2030-01-01','2026-01-01')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at) "
            "VALUES ('a2','B','dup','hash','2030-01-01','2026-01-01')"
        )
        conn.commit()


def test_admin_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at, status) "
            "VALUES ('a1','A','u1','hash','2030-01-01','2026-01-01','bogus')"
        )
        conn.commit()


def test_device_cascade_delete_on_admin_delete(conn):
    conn.execute(
        "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at) "
        "VALUES ('a1','A','u1','hash','2030-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO devices (id, admin_id, device_fingerprint, first_seen_at, last_seen_at) "
        "VALUES ('d1','a1','fp1','2026-01-01','2026-01-01')"
    )
    conn.commit()

    conn.execute("DELETE FROM admins WHERE id = 'a1'")
    conn.commit()

    remaining = conn.execute("SELECT * FROM devices WHERE id = 'd1'").fetchall()
    assert remaining == []


def test_device_unique_per_admin_fingerprint(conn):
    conn.execute(
        "INSERT INTO admins (id, name, username, password_hash, expiry_date, created_at) "
        "VALUES ('a1','A','u1','hash','2030-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO devices (id, admin_id, device_fingerprint, first_seen_at, last_seen_at) "
        "VALUES ('d1','a1','fp1','2026-01-01','2026-01-01')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO devices (id, admin_id, device_fingerprint, first_seen_at, last_seen_at) "
            "VALUES ('d2','a1','fp1','2026-01-01','2026-01-01')"
        )
        conn.commit()
