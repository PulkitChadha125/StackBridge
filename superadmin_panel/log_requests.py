"""Admin App Logs on the SuperAdmin's request.

The SuperAdmin picks one of an Admin's servers (device) and a date range of
up to MAX_RANGE_DAYS days. The Admin's app checks for requests every
POLL_SECONDS (POST /api/logs/pending, with its usual session token),
reads those days from its log files, and uploads them gzip-compressed
(POST /api/logs/upload). The server never connects to the Admin's machine -
same push model as telemetry.

Lifecycle: pending -> picked_up -> ready | failed; or expired when the app
doesn't answer (not running / logged out). Uploaded files are deleted after
RETENTION_HOURS.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from flask import Blueprint, jsonify, request

from . import config, db
from .session_auth import resolve_session

MAX_RANGE_DAYS = 7
# How often the app checks for requests (sent to the app, so it can change
# here without a new EXE).
POLL_SECONDS = 30
# Not picked up in this long: the app is not running / not logged in.
PICKUP_TIMEOUT_SECONDS = 10 * 60
# Picked up but nothing uploaded in this long.
UPLOAD_TIMEOUT_SECONDS = 10 * 60
RETENTION_HOURS = 72
# Compressed upload limit. Base64 adds a third, so a request stays under
# IIS's default 30,000,000-byte request limit.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 23 * 1024 * 1024
MAX_MESSAGE_LENGTH = 500

OPEN_STATUSES = ("pending", "picked_up")

# "Errors only" downloads. A log line is "2026-10-08 10:00:00,123 LEVEL ..."; lines without that start
# (tracebacks) belong to the line above. The app adds a trades section after the log lines
# (stakbridge/tradereport.py): one block per master trade, client lines needing a look start with "  !! ".
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3} (\w+) ")
PROBLEM_LEVELS = ("WARNING", "ERROR", "CRITICAL")
TRADES_HEADER = "==== Trades:"
TRADE_START = "Master trade"
PROBLEM_MARK = "  !! "

log_request_api_bp = Blueprint("log_request_api", __name__, url_prefix="/api/logs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def file_path(request_id: str) -> Path:
    return config.log_upload_dir() / f"{request_id}.txt.gz"


def validate_range(date_from: str, date_to: str, today: date | None = None) -> str | None:
    """None when the range is usable, else the message to show."""
    try:
        start = date.fromisoformat(date_from or "")
        end = date.fromisoformat(date_to or "")
    except ValueError:
        return "Pick a valid From and To date."
    if start > end:
        return "The From date must be on or before the To date."
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return f"Pick at most {MAX_RANGE_DAYS} days of logs at a time."
    # The Admin's PC may be a day ahead of this server's clock.
    if start > (today or date.today()) + timedelta(days=1):
        return "The From date is in the future."
    return None


# --- storage --------------------------------------------------------------------


def _row(conn, request_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM log_requests WHERE id = ?", (request_id,)).fetchone()
    return dict(row) if row else None


def _set(conn, request_id: str, **fields: Any) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE log_requests SET {columns} WHERE id = ?", (*fields.values(), request_id))
    conn.commit()


def expire_stale(conn) -> None:
    """Requests the app never answered stop waiting."""
    now = _now()
    for row in conn.execute(
        "SELECT * FROM log_requests WHERE status IN ('pending', 'picked_up')"
    ).fetchall():
        item = dict(row)
        if item["status"] == "pending":
            created = _parse(item["created_at"])
            if created and (now - created).total_seconds() > PICKUP_TIMEOUT_SECONDS:
                _set(
                    conn,
                    item["id"],
                    status="expired",
                    completed_at=_iso(now),
                    message=(
                        "The Admin's app did not answer within 10 minutes. It must be open and logged in "
                        "on that server - try again then."
                    ),
                )
        else:
            picked = _parse(item["picked_up_at"])
            if picked and (now - picked).total_seconds() > UPLOAD_TIMEOUT_SECONDS:
                _set(
                    conn,
                    item["id"],
                    status="failed",
                    completed_at=_iso(now),
                    message="The Admin's app started sending the logs but never finished. Try again.",
                )


def purge_old(conn) -> None:
    cutoff = _iso(_now() - timedelta(hours=RETENTION_HOURS))
    rows = conn.execute("SELECT id FROM log_requests WHERE created_at < ?", (cutoff,)).fetchall()
    for row in rows:
        try:
            file_path(row["id"]).unlink(missing_ok=True)
        except OSError:
            pass
    conn.execute("DELETE FROM log_requests WHERE created_at < ?", (cutoff,))
    conn.commit()


def create_request(conn, admin_id: str, device_id: str, date_from: str, date_to: str) -> dict[str, Any]:
    expire_stale(conn)
    purge_old(conn)
    open_row = conn.execute(
        "SELECT id FROM log_requests WHERE device_id = ? AND status IN ('pending', 'picked_up')",
        (device_id,),
    ).fetchone()
    if open_row:
        raise ValueError("Logs are already being fetched from this server - wait for that request to finish.")
    request_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO log_requests (id, admin_id, device_id, date_from, date_to, status, message, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            request_id,
            admin_id,
            device_id,
            date_from,
            date_to,
            "Waiting for the Admin's app to pick up the request (it checks every 30 seconds)...",
            _iso(_now()),
        ),
    )
    conn.commit()
    return _row(conn, request_id)


def get_request(conn, request_id: str) -> dict[str, Any] | None:
    expire_stale(conn)
    return _row(conn, request_id)


def list_recent(conn, admin_id: str, limit: int = 5) -> list[dict[str, Any]]:
    expire_stale(conn)
    purge_old(conn)
    rows = conn.execute(
        "SELECT * FROM log_requests WHERE admin_id = ? ORDER BY created_at DESC LIMIT ?",
        (admin_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def take_pending(conn, device_id: str) -> dict[str, Any] | None:
    expire_stale(conn)
    row = conn.execute(
        "SELECT * FROM log_requests WHERE device_id = ? AND status = 'pending' ORDER BY created_at LIMIT 1",
        (device_id,),
    ).fetchone()
    if row is None:
        return None
    _set(
        conn,
        row["id"],
        status="picked_up",
        picked_up_at=_iso(_now()),
        message="The Admin's app is reading and sending the logs...",
    )
    return _row(conn, row["id"])


def complete(conn, request_id: str, content_gz: bytes, line_count: int, truncated: bool) -> dict[str, Any]:
    path = file_path(request_id)
    path.write_bytes(content_gz)
    message = f"Ready: {line_count:,} log line(s)."
    if truncated:
        message += " The range was too large to send whole - only the newest lines are included."
    if line_count == 0:
        message = "Ready, but the app has no log lines for these dates."
    _set(
        conn,
        request_id,
        status="ready",
        completed_at=_iso(_now()),
        line_count=line_count,
        truncated=int(truncated),
        size_bytes=len(content_gz),
        message=message,
    )
    return _row(conn, request_id)


def fail(conn, request_id: str, message: str) -> dict[str, Any]:
    _set(conn, request_id, status="failed", completed_at=_iso(_now()), message=message[:MAX_MESSAGE_LENGTH])
    return _row(conn, request_id)


def problems_only(lines) -> Iterator[str]:
    """Warnings and errors (with their tracebacks), and the trades where a client had a problem."""
    keep, in_trades, block, problems = False, False, [], 0

    def finish_block():
        nonlocal problems
        if any(line.startswith(PROBLEM_MARK) for line in block):
            problems += 1
            yield from block

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith(TRADES_HEADER):
            in_trades = True
            yield ""
            yield line
        elif in_trades:
            if line.startswith(TRADE_START):
                yield from finish_block()
                block = [line]
            elif block:
                block.append(line)
            elif line:
                yield line  # the section's own notes
        else:
            match = LOG_LINE.match(line)
            if match:
                keep = match.group(1) in PROBLEM_LEVELS
            if keep:
                yield line
    yield from finish_block()
    if in_trades and not problems:
        yield "No trade had a problem on any client."


def iter_download(
    item: dict[str, Any], admin: dict[str, Any], device: dict[str, Any] | None, errors_only: bool = False
) -> Iterator[bytes]:
    fingerprint = (device or {}).get("device_fingerprint") or "unknown"
    header = (
        f"# StakBridge - App Log of {admin.get('name')} ({admin.get('username')})\n"
        f"# Server (device) {fingerprint[:16]}... - {item['date_from']} to {item['date_to']} "
        f"(dates and times are the Admin PC's local time)\n"
        f"# {item.get('line_count') or 0} line(s)"
        f"{' - TRUNCATED: only the newest lines fitted' if item.get('truncated') else ''}\n"
    )
    if errors_only:
        header += "# ERRORS ONLY: warnings, errors and the trades where a client had a problem\n"
    yield (header + "\n").encode("utf-8")
    if errors_only:
        with gzip.open(file_path(item["id"]), "rt", encoding="utf-8", errors="replace") as text:
            for line in problems_only(text):
                yield (line + "\n").encode("utf-8")
        return
    with gzip.open(file_path(item["id"]), "rb") as handle:
        while True:
            chunk = handle.read(256 * 1024)
            if not chunk:
                return
            yield chunk


def to_public(item: dict[str, Any]) -> dict[str, Any]:
    """Fields the SuperAdmin page uses."""
    return {
        "id": item["id"],
        "device_id": item["device_id"],
        "date_from": item["date_from"],
        "date_to": item["date_to"],
        "status": item["status"],
        "message": item.get("message") or "",
        "created_at": item["created_at"],
        "line_count": item.get("line_count"),
        "truncated": bool(item.get("truncated")),
        "done": item["status"] not in OPEN_STATUSES,
        "downloadable": item["status"] == "ready" and file_path(item["id"]).exists(),
    }


# --- Admin app API (session token) ------------------------------------------------


@log_request_api_bp.post("/pending")
def pending():
    data = request.get_json(silent=True) or {}
    conn = db.get_db()
    admin, device, error = resolve_session(conn, (data.get("token") or "").strip())
    if error:
        return error
    item = take_pending(conn, device["id"])
    body: dict[str, Any] = {"request": None, "poll_seconds": POLL_SECONDS}
    if item:
        body["request"] = {
            "id": item["id"],
            "date_from": item["date_from"],
            "date_to": item["date_to"],
            "max_bytes": MAX_UPLOAD_BYTES,
        }
    return jsonify(body), 200


@log_request_api_bp.post("/upload")
def upload():
    if (request.content_length or 0) > MAX_REQUEST_BYTES:
        return jsonify({"error": "too_large", "message": "Upload too large."}), 413
    data = request.get_json(silent=True) or {}
    conn = db.get_db()
    admin, device, error = resolve_session(conn, (data.get("token") or "").strip())
    if error:
        return error
    item = _row(conn, str(data.get("request_id") or ""))
    if item is None or item["device_id"] != device["id"] or item["admin_id"] != admin["id"]:
        return jsonify({"error": "not_found", "message": "No such log request."}), 404
    if item["status"] not in OPEN_STATUSES:
        return jsonify({"error": "closed", "message": f"This request is already {item['status']}."}), 409

    if data.get("error"):
        fail(conn, item["id"], "The Admin's app could not read its logs: " + str(data["error"]))
        return jsonify({"message": "recorded"}), 200

    try:
        content = base64.b64decode(data.get("content_gzip_b64") or "", validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "bad_content", "message": "Content is not valid base64."}), 400
    if len(content) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "too_large", "message": "Upload too large."}), 413
    if content[:2] != b"\x1f\x8b":
        return jsonify({"error": "bad_content", "message": "Content is not gzip."}), 400
    try:
        line_count = max(0, int(data.get("line_count") or 0))
    except (TypeError, ValueError):
        line_count = 0
    complete(conn, item["id"], content, line_count, bool(data.get("truncated")))
    return jsonify({"message": "stored"}), 200
