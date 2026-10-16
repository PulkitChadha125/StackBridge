"""Shared opaque-session resolution, used by both auth_api.py and
telemetry_api.py (any Admin-facing endpoint authenticated by session token
rather than the SuperAdmin's own browser-cookie login).
"""

from __future__ import annotations

from flask import jsonify

from . import repositories as repo, security


def resolve_session(conn, token: str):
    """Returns (admin, device, None) on success, or (None, None, error_response) on failure."""
    invalid = (
        None,
        None,
        (jsonify({"error": "invalid_session", "message": "Session is invalid, expired, or revoked. Please log in again."}), 401),
    )

    if not token:
        return None, None, (jsonify({"error": "invalid_session", "message": "Missing token."}), 401)

    session_row = repo.get_session_by_token_hash(conn, security.hash_token(token))
    if session_row is None or not repo.is_session_valid(session_row):
        return invalid

    admin = repo.get_admin(conn, session_row["admin_id"])
    device = repo.get_device(conn, session_row["device_id"])
    if admin is None or device is None or device["status"] != "active":
        return invalid

    return admin, device, None
