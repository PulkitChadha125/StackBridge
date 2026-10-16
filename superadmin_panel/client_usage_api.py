"""Admin-facing client-limit check.

An Admin's limit (admins.max_client_accounts) is shared by all of their
servers: each server keeps its own clients locally, and the SuperAdmin only
learns each server's *count*. Before an Admin's app saves more clients it
calls /api/clients/reserve with the number it is about to hold; this server
adds that to the other servers' counts and says yes or no. Shrinking is
always allowed, so deletes and the periodic telemetry count can never be
refused.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import config, db, repositories as repo
from .session_auth import resolve_session

client_usage_api_bp = Blueprint("client_usage_api", __name__, url_prefix="/api/clients")

MAX_CLIENT_COUNT = 100_000


def support_contact(conn) -> dict[str, str]:
    """Contact details for the app's "limit reached" pop-up."""
    return {
        "email": repo.get_setting(conn, repo.SETTING_SUPPORT_EMAIL) or config.SUPPORT_EMAIL,
        "whatsapp_url": repo.get_setting(conn, repo.SETTING_SUPPORT_WHATSAPP_URL)
        or config.SUPPORT_WHATSAPP_URL,
    }


def usage_fields(conn, admin: dict) -> dict:
    """Added to login/refresh answers so the app can show "used / limit"."""
    return {
        "client_usage_total": repo.total_client_usage(conn, admin["id"]),
        "support_contact": support_contact(conn),
    }


@client_usage_api_bp.post("/reserve")
def reserve():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()

    conn = db.get_db()
    admin, device, error = resolve_session(conn, token)
    if error:
        return error

    try:
        client_count = int(data.get("client_count"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_request", "message": "client_count must be a whole number."}), 400
    if not 0 <= client_count <= MAX_CLIENT_COUNT:
        return jsonify({"error": "invalid_request", "message": "client_count is out of range."}), 400

    limit = admin["max_client_accounts"]
    allowed, total = repo.reserve_client_slots(
        conn, admin_id=admin["id"], device_id=device["id"], new_count=client_count, limit=limit
    )
    body = {
        "allowed": allowed,
        "max_client_accounts": limit,
        "client_usage_total": total,
        "support_contact": support_contact(conn),
    }
    if not allowed:
        body.update(
            {
                "error": "client_limit_reached",
                "message": (
                    f"Your plan allows {limit} client accounts across all your servers "
                    f"and {total} are in use. Contact your provider to increase the limit."
                ),
            }
        )
        return jsonify(body), 409
    return jsonify(body), 200
