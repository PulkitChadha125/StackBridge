"""Admin-facing REST authentication API.

Consumed by the StakBridge desktop app (stakbridge/licensing in the repo),
never by a browser directly. Opaque server-side session tokens, not JWT -
every token is a random secret whose SHA-256 hash is the only thing stored,
and revocation is a single DB write (see repositories.revoke_session /
revoke_all_sessions_for_admin), no blocklist needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from . import (
    client_usage_api,
    config,
    db,
    license_signing,
    password_reset,
    rate_limit,
    repositories as repo,
    security,
)
from .session_auth import resolve_session

auth_api_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

# Used to burn roughly the same amount of time on an unknown-username login
# attempt as a real one, so response timing doesn't reveal which usernames exist.
_DUMMY_PASSWORD_HASH = security.hash_password("dummy-password-for-timing-parity")

MAX_FINGERPRINT_LENGTH = 512


def _admin_public_fields(admin: dict) -> dict:
    return {
        "admin_id": admin["id"],
        "name": admin["name"],
        "username": admin["username"],
        "status": admin["status"],
        "expiry_date": admin["expiry_date"],
        "allowed_installations": admin["allowed_installations"],
        # None = unlimited. Older apps ignore the field.
        "max_client_accounts": admin["max_client_accounts"],
        "must_change_password": bool(admin["must_change_password"]),
    }


def _license_valid(admin: dict) -> bool:
    return admin["status"] == "active" and not repo.is_admin_expired(admin)


def _build_signed_license_cache(admin: dict, device: dict) -> dict:
    now = datetime.now(timezone.utc)
    grace_until = now + timedelta(hours=config.OFFLINE_GRACE_HOURS)
    payload = {
        "admin_id": admin["id"],
        "device_id": device["id"],
        "status": admin["status"],
        "license_valid": _license_valid(admin),
        "expiry_date": admin["expiry_date"],
        "allowed_installations": admin["allowed_installations"],
        # Signed so the app can trust its client limit while offline and an
        # Admin can't raise it by editing license_cache.json.
        "max_client_accounts": admin["max_client_accounts"],
        "verified_at": now.isoformat(),
        "grace_until": grace_until.isoformat(),
        "issued_at": now.isoformat(),
    }
    return license_signing.sign_license_payload(payload)


def _issue_session(conn, admin: dict, device: dict) -> dict:
    raw_token = security.generate_token()
    session_row = repo.create_session(
        conn,
        admin_id=admin["id"],
        device_id=device["id"],
        token_hash=security.hash_token(raw_token),
        ttl_seconds=config.ADMIN_SESSION_TTL_SECONDS,
    )
    payload = _admin_public_fields(admin)
    payload.update(client_usage_api.usage_fields(conn, admin))
    payload.update(
        {
            "token": raw_token,
            "device_id": device["id"],
            "license_valid": _license_valid(admin),
            "session_expires_at": session_row["expires_at"],
            "license_cache": _build_signed_license_cache(admin, device),
        }
    )
    return payload


@auth_api_bp.post("/login")
def login():
    rate_limit_key = f"login:{request.remote_addr}"
    if rate_limit.is_rate_limited(
        rate_limit_key,
        max_attempts=config.LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=config.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    ):
        return jsonify(
            {"error": "rate_limited", "message": "Too many login attempts. Try again later."}
        ), 429

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    fingerprint = (data.get("device_fingerprint") or "").strip()

    if not username or not password:
        return jsonify({"error": "invalid_request", "message": "username and password are required."}), 400
    if not fingerprint:
        return jsonify({"error": "invalid_request", "message": "device_fingerprint is required."}), 400
    if len(fingerprint) > MAX_FINGERPRINT_LENGTH:
        return jsonify({"error": "invalid_request", "message": "device_fingerprint is too long."}), 400

    conn = db.get_db()
    admin = repo.get_admin_by_username(conn, username)

    if admin is None:
        security.verify_password(password, _DUMMY_PASSWORD_HASH)  # timing parity
        return jsonify({"error": "invalid_credentials", "message": "Invalid username or password."}), 401

    if not security.verify_password(password, admin["password_hash"]):
        return jsonify({"error": "invalid_credentials", "message": "Invalid username or password."}), 401

    if admin["status"] == "revoked":
        return jsonify({"error": "admin_revoked", "message": "This account has been revoked. Contact your provider."}), 403

    if repo.is_admin_expired(admin):
        return jsonify({"error": "license_expired", "message": "Your license has expired. Contact your provider."}), 403

    existing_device = repo.get_device_by_fingerprint(conn, admin["id"], fingerprint)
    if existing_device is not None and existing_device["status"] == "revoked":
        return jsonify(
            {"error": "device_revoked", "message": "This device has been removed from your account. Contact your provider."}
        ), 403

    if existing_device is None:
        active_count = repo.count_active_devices(conn, admin["id"])
        if active_count >= admin["allowed_installations"]:
            return jsonify(
                {
                    "error": "installation_limit_reached",
                    "message": (
                        f"Installation limit reached ({admin['allowed_installations']} allowed). "
                        "Contact your provider to add a device or remove an old one."
                    ),
                }
            ), 403

        # Trial abuse guard: a free trial is one per *device*, not one per
        # email - checked only the first time this fingerprint logs into
        # THIS admin, and only for trial-sourced admins, so it never affects
        # a legitimate paying Admin reinstalling on a new machine.
        if admin["signup_source"] == "trial_signup":
            # Deleting a trial Admin removes its trial_devices row, so a
            # deleted trial does not block this device.
            trial_owner_id = repo.get_trial_device_owner(conn, fingerprint)
            if repo.has_trial_device_been_used(conn, fingerprint) and trial_owner_id != admin["id"]:
                return jsonify(
                    {
                        "error": "trial_already_used",
                        "message": "This device has already used a free trial. Contact us to continue.",
                    }
                ), 403

    device = repo.register_or_touch_device(conn, admin["id"], fingerprint)

    if admin["signup_source"] == "trial_signup":
        repo.record_trial_device(conn, fingerprint, admin["id"])

    payload = _issue_session(conn, admin, device)
    return jsonify(payload), 200


@auth_api_bp.post("/refresh")
def refresh():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()

    conn = db.get_db()
    admin, device, error = resolve_session(conn, token)
    if error:
        # A deleted Admin's sessions are gone with it, so the token alone
        # can't say why it's invalid - the app also sends its admin_id.
        claimed_admin_id = (data.get("admin_id") or "").strip()
        if claimed_admin_id and repo.is_admin_deleted(conn, claimed_admin_id):
            return jsonify(
                {"error": "admin_deleted", "message": "This account has been deleted by your provider."}
            ), 401
        return error

    repo.extend_session(conn, _current_session_id(conn, token), config.ADMIN_SESSION_TTL_SECONDS)
    repo.register_or_touch_device(conn, admin["id"], device["device_fingerprint"])

    payload = _admin_public_fields(admin)
    payload.update(client_usage_api.usage_fields(conn, admin))
    payload.update(
        {
            "device_id": device["id"],
            "license_valid": _license_valid(admin),
            "license_cache": _build_signed_license_cache(admin, device),
        }
    )
    return jsonify(payload), 200


def _current_session_id(conn, token: str) -> str:
    row = repo.get_session_by_token_hash(conn, security.hash_token(token))
    return row["id"]


@auth_api_bp.post("/logout")
def logout():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if token:
        conn = db.get_db()
        session_row = repo.get_session_by_token_hash(conn, security.hash_token(token))
        if session_row is not None:
            repo.revoke_session(conn, session_row["id"])
    # Idempotent by design - logging out an already-invalid token is not an error.
    return jsonify({"message": "Logged out."}), 200


@auth_api_bp.post("/change-password")
def change_password():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    if not new_password or len(new_password) < 8:
        return jsonify({"error": "invalid_request", "message": "New password must be at least 8 characters."}), 400

    conn = db.get_db()
    admin, device, error = resolve_session(conn, token)
    if error:
        return error

    if not security.verify_password(current_password, admin["password_hash"]):
        return jsonify({"error": "invalid_credentials", "message": "Current password is incorrect."}), 401

    new_hash = security.hash_password(new_password)
    repo.set_admin_password(conn, admin["id"], new_hash, must_change_password=False)

    # Rotate: kill every existing session (including this one) and issue a fresh one,
    # so a stale copied session elsewhere dies the moment the password changes.
    repo.revoke_all_sessions_for_admin(conn, admin["id"])
    updated_admin = repo.get_admin(conn, admin["id"])
    payload = _issue_session(conn, updated_admin, device)
    return jsonify(payload), 200


# --------------------------------------------------------------------------
# Forgot password - the Admin app's login screen calls these (no session
# needed; see password_reset.py for the rules).
# --------------------------------------------------------------------------


def _password_reset_rate_limited():
    if rate_limit.is_rate_limited(
        f"password_reset:{request.remote_addr}",
        max_attempts=config.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=config.PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
    ):
        return jsonify({"error": "rate_limited", "message": "Too many attempts. Please try again later."}), 429
    return None


def _reset_error(exc: password_reset.ResetError):
    return jsonify({"error": "reset_failed", "message": exc.message, "retryable": exc.retryable}), 400


@auth_api_bp.post("/forgot-password")
def forgot_password():
    limited = _password_reset_rate_limited()
    if limited:
        return limited
    data = request.get_json(silent=True) or {}
    try:
        wait = password_reset.request_code(db.get_db(), password_reset.ADMIN, data.get("email") or "")
    except password_reset.ResetError as exc:
        return _reset_error(exc)
    return jsonify({"message": password_reset.SENT_MESSAGE, "resend_wait": wait}), 200


@auth_api_bp.post("/forgot-password/verify")
def forgot_password_verify():
    limited = _password_reset_rate_limited()
    if limited:
        return limited
    data = request.get_json(silent=True) or {}
    try:
        token = password_reset.verify_code(
            db.get_db(), password_reset.ADMIN, data.get("email") or "", data.get("code") or ""
        )
    except password_reset.ResetError as exc:
        return _reset_error(exc)
    return jsonify({"reset_token": token}), 200


@auth_api_bp.post("/forgot-password/reset")
def forgot_password_reset():
    data = request.get_json(silent=True) or {}
    try:
        password_reset.complete_reset(
            db.get_db(),
            password_reset.ADMIN,
            data.get("email") or "",
            data.get("reset_token") or "",
            data.get("new_password") or "",
            data.get("confirm_password") or "",
        )
    except password_reset.ResetError as exc:
        return _reset_error(exc)
    return jsonify({"message": "Password changed. Log in with your new password."}), 200
