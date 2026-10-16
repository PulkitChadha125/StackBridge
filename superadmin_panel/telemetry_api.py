"""Admin-facing telemetry sync endpoint (Phase 10).

Push model, not pull: the Admin app calls this periodically (see
telemetry_sync.py/telemetry_client.py in the repo root) using its existing
opaque session token - the SuperAdmin server never opens a connection back
into an Admin's machine. See README.md/plan.md for the full push-vs-pull
rationale.

Deliberately does NOT accept or store: broker passwords, passwords, session
tokens, or full trade request/response payloads - only aggregate counts and
a small, already-sanitized recent-trades summary (symbol/side/quantity/
status/action/timestamp).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import client_usage_api, db, repositories as repo
from .session_auth import resolve_session

telemetry_api_bp = Blueprint("telemetry_api", __name__, url_prefix="/api/telemetry")

MAX_RECENT_TRADES = 20
MAX_APP_VERSION_LENGTH = 64
MAX_STATUS_LENGTH = 32


@telemetry_api_bp.post("/sync")
def sync_telemetry():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()

    conn = db.get_db()
    admin, device, error = resolve_session(conn, token)
    if error:
        return error

    recent_trades = data.get("recent_trades")
    if not isinstance(recent_trades, list):
        recent_trades = []
    recent_trades = recent_trades[:MAX_RECENT_TRADES]

    repo.upsert_telemetry(
        conn,
        admin_id=admin["id"],
        device_id=device["id"],
        app_version=str(data.get("app_version") or "")[:MAX_APP_VERSION_LENGTH],
        copier_status=str(data.get("copier_status") or "")[:MAX_STATUS_LENGTH],
        copier_running=bool(data.get("copier_running")),
        client_count=int(data.get("client_count") or 0),
        enabled_client_count=int(data.get("enabled_client_count") or 0),
        recent_trades=recent_trades,
    )
    # The app's own client count is the truth for this server: a periodic
    # report also repairs a count left stale by a failed save or an offline
    # delete. Never refused - the limit is only checked when adding.
    repo.set_client_usage(conn, admin["id"], device["id"], int(data.get("client_count") or 0))
    # A telemetry sync is also a good "the Admin is alive" signal.
    repo.register_or_touch_device(conn, admin["id"], device["device_fingerprint"])

    return jsonify(
        {
            "message": "synced",
            "max_client_accounts": admin["max_client_accounts"],
            **client_usage_api.usage_fields(conn, admin),
        }
    ), 200
