"""SuperAdmin-facing web UI: login, Admin CRUD, devices, password reset.

Session-cookie authenticated (this is a human clicking around a browser,
unlike the Admin-facing REST API in auth_api.py which issues opaque
tokens to a desktop app).
"""

from __future__ import annotations

import functools
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)

from . import (
    config, db, email_notifications, exe_builder, log_requests, password_reset, rate_limit, repositories, security,
)

superadmin_bp = Blueprint("superadmin_web", __name__)


def delivery_server_url() -> str:
    """The address compiled into EXEs built from this panel.

    Precedence: the value saved on the Settings page, then the
    PUBLIC_SERVER_URL environment variable, then the address this browser
    reached the panel on. The saved value wins so the operator can change
    where their license server lives without touching the machine's
    environment or restarting anything.
    """
    stored = repositories.get_setting(db.get_db(), repositories.SETTING_PUBLIC_SERVER_URL)
    if stored:
        return stored.rstrip("/")
    return config.public_server_url(request.host_url)


def _is_plausible_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_valid_date(value: str) -> bool:
    """Rejects malformed expiry dates here rather than letting them crash
    is_admin_expired()'s datetime.fromisoformat() later, inside the login/
    refresh request path used by every Admin instance."""
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


_CLIENT_LIMIT_NOT_SENT = object()


def _parse_client_limit(form) -> tuple[object, str | None]:
    """Reads the "Max client accounts" field plus its "Unlimited" checkbox.
    Returns (limit, error); a limit of None means unlimited, and
    _CLIENT_LIMIT_NOT_SENT means the form had neither field (an older form
    or script), so the caller keeps its default/current value."""
    if form.get("unlimited_client_accounts"):
        return None, None
    if "max_client_accounts" not in form:
        return _CLIENT_LIMIT_NOT_SENT, None
    raw = (form.get("max_client_accounts") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None, "Max client accounts must be a whole number (or tick Unlimited)."
    if value < 1:
        return None, "Max client accounts must be at least 1 (or tick Unlimited)."
    return value, None


@superadmin_bp.app_template_filter("ago")
def time_ago(iso_timestamp: str | None) -> str:
    """'3 min ago' style age of a stored UTC timestamp."""
    if not iso_timestamp:
        return "never"
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("superadmin_authenticated"):
            return redirect(url_for("superadmin_web.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@superadmin_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        rate_limit_key = f"superadmin_login:{request.remote_addr}"
        if rate_limit.is_rate_limited(
            rate_limit_key,
            max_attempts=config.LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
            window_seconds=config.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        ):
            return render_template("login.html", error="Too many login attempts. Try again later."), 429

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        creds = config.load_superadmin_credentials()

        if creds is None:
            error = "SuperAdmin credentials are not configured on this server."
        else:
            expected_username, expected_hash = creds
            valid_username = security.constant_time_eq(username, expected_username)
            valid_password = security.verify_password(password, expected_hash)
            if valid_username and valid_password:
                session.clear()
                session["superadmin_authenticated"] = True
                session["superadmin_username"] = username
                next_url = request.args.get("next") or url_for("superadmin_web.dashboard")
                return redirect(next_url)
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@superadmin_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("superadmin_web.login"))


# --------------------------------------------------------------------------
# Forgot password (SuperAdmin's own login) - see password_reset.py. The email
# and reset token ride along in the signed session cookie between steps.
# --------------------------------------------------------------------------


def _forgot_rate_limited() -> bool:
    return rate_limit.is_rate_limited(
        f"password_reset:{request.remote_addr}",
        max_attempts=config.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=config.PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
    )


@superadmin_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html", step="email", email="")

    email = (request.form.get("email") or "").strip()
    if _forgot_rate_limited():
        flash("Too many attempts. Please try again later.", "error")
        return render_template("forgot_password.html", step="email", email=email), 429
    try:
        wait = password_reset.request_code(db.get_db(), password_reset.SUPERADMIN, email)
    except password_reset.ResetError as exc:
        flash(exc.message, "error")
        return render_template("forgot_password.html", step="email", email=email)

    session.pop("password_reset_token", None)
    session["password_reset_email"] = email
    flash(password_reset.SENT_MESSAGE, "success")
    return render_template("forgot_password.html", step="code", email=email, resend_wait=wait)


@superadmin_bp.route("/forgot-password/verify", methods=["POST"])
def forgot_password_verify():
    email = session.get("password_reset_email")
    if not email:
        return redirect(url_for("superadmin_web.forgot_password"))
    if _forgot_rate_limited():
        flash("Too many attempts. Please try again later.", "error")
        return render_template("forgot_password.html", step="code", email=email), 429
    try:
        token = password_reset.verify_code(
            db.get_db(), password_reset.SUPERADMIN, email, request.form.get("code") or ""
        )
    except password_reset.ResetError as exc:
        flash(exc.message, "error")
        if exc.retryable:
            return render_template("forgot_password.html", step="code", email=email)
        return render_template("forgot_password.html", step="email", email=email)

    session["password_reset_token"] = token
    return redirect(url_for("superadmin_web.forgot_password_new"))


@superadmin_bp.route("/forgot-password/new", methods=["GET", "POST"])
def forgot_password_new():
    email = session.get("password_reset_email")
    token = session.get("password_reset_token")
    if not email or not token:
        return redirect(url_for("superadmin_web.forgot_password"))
    if request.method == "GET":
        return render_template("forgot_password.html", step="password", email=email)

    try:
        password_reset.complete_reset(
            db.get_db(),
            password_reset.SUPERADMIN,
            email,
            token,
            request.form.get("new_password") or "",
            request.form.get("confirm_password") or "",
        )
    except password_reset.ResetError as exc:
        flash(exc.message, "error")
        if exc.retryable:
            return render_template("forgot_password.html", step="password", email=email)
        session.pop("password_reset_token", None)
        return render_template("forgot_password.html", step="email", email=email)

    session.clear()
    flash("Password changed. Log in with your new password.", "success")
    return redirect(url_for("superadmin_web.login"))


@superadmin_bp.route("/")
@login_required
def dashboard():
    conn = db.get_db()
    admins = repositories.list_admins(conn)
    telemetry_by_admin = {row["admin_id"]: row for row in repositories.list_all_telemetry(conn)}
    return render_template(
        "dashboard.html",
        admins=admins,
        telemetry_by_admin=telemetry_by_admin,
        usage_by_admin=repositories.client_usage_summaries(conn),
    )


@superadmin_bp.route("/admins/new", methods=["GET", "POST"])
@login_required
def create_admin():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        username = (request.form.get("username") or "").strip()
        expiry_date = (request.form.get("expiry_date") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None
        try:
            allowed_installations = int(request.form.get("allowed_installations") or 1)
        except ValueError:
            allowed_installations = 1
        max_client_accounts, client_limit_error = _parse_client_limit(request.form)
        if max_client_accounts is _CLIENT_LIMIT_NOT_SENT:
            max_client_accounts = config.DEFAULT_MAX_CLIENT_ACCOUNTS

        conn = db.get_db()
        error = None
        if not name:
            error = "Name is required."
        elif not username:
            error = "Username is required."
        elif not expiry_date:
            error = "Expiry date is required."
        elif not _is_valid_date(expiry_date):
            error = "Expiry date must be a valid date (YYYY-MM-DD)."
        elif repositories.get_admin_by_username(conn, username) is not None:
            error = f"Username '{username}' is already taken."
        elif allowed_installations < 1:
            error = "Allowed installations must be at least 1."
        elif client_limit_error:
            error = client_limit_error
        elif email and not security.is_plausible_email(email):
            error = "That doesn't look like a valid email address."

        if error:
            flash(error, "error")
            return render_template(
                "create_admin.html",
                form={
                    "name": name,
                    "username": username,
                    "expiry_date": expiry_date,
                    "allowed_installations": allowed_installations,
                    "max_client_accounts": request.form.get("max_client_accounts") or "",
                    "unlimited_client_accounts": bool(request.form.get("unlimited_client_accounts")),
                    "notes": notes or "",
                    "email": email or "",
                },
            )

        temp_password = security.generate_temp_password()
        password_hash = security.hash_password(temp_password)
        admin = repositories.create_admin(
            conn,
            name=name,
            username=username,
            password_hash=password_hash,
            expiry_date=expiry_date,
            allowed_installations=allowed_installations,
            max_client_accounts=max_client_accounts,
            notes=notes,
            email=email,
            must_change_password=True,
            status="active",
        )

        # Shown exactly once - never persisted anywhere in plaintext.
        session["just_created_admin"] = {
            "admin_id": admin["id"],
            "username": admin["username"],
            "temp_password": temp_password,
            "expiry_date": admin["expiry_date"],
            "allowed_installations": admin["allowed_installations"],
            "max_client_accounts": admin["max_client_accounts"],
        }
        return redirect(url_for("superadmin_web.admin_created", admin_id=admin["id"]))

    return render_template(
        "create_admin.html", form=None, default_client_limit=config.DEFAULT_MAX_CLIENT_ACCOUNTS
    )


@superadmin_bp.route("/admins/<admin_id>/created")
@login_required
def admin_created(admin_id: str):
    payload = session.pop("just_created_admin", None)
    if not payload or payload.get("admin_id") != admin_id:
        flash("That one-time credential view has already been used or expired.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))
    return render_template("admin_created.html", info=payload)


@superadmin_bp.route("/admins/<admin_id>")
@login_required
def admin_detail(admin_id: str):
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        flash("Admin not found.", "error")
        return redirect(url_for("superadmin_web.dashboard"))
    devices = repositories.list_devices(conn, admin_id)
    telemetry = repositories.get_telemetry(conn, admin_id)
    existing_exe = exe_builder.find_existing_exe(admin_id)
    usage_by_device = repositories.client_usage_by_device(conn, admin_id)
    return render_template(
        "admin_detail.html",
        admin=admin,
        devices=devices,
        telemetry=telemetry,
        build_problems=exe_builder.preflight(),
        build_server_url=delivery_server_url(),
        existing_exe_name=existing_exe.name if existing_exe else None,
        active_job=exe_builder.get_latest_job_for_admin(admin_id),
        default_client_limit=config.DEFAULT_MAX_CLIENT_ACCOUNTS,
        usage=repositories.client_usage_summaries(conn).get(admin_id),
        usage_by_device=usage_by_device,
        log_requests=[log_requests.to_public(item) for item in log_requests.list_recent(conn, admin_id)],
        log_max_days=log_requests.MAX_RANGE_DAYS,
        today=date.today().isoformat(),
    )


@superadmin_bp.route("/admins/<admin_id>/edit", methods=["POST"])
@login_required
def edit_admin(admin_id: str):
    conn = db.get_db()
    name = (request.form.get("name") or "").strip()
    expiry_date = (request.form.get("expiry_date") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    email = (request.form.get("email") or "").strip() or None
    try:
        allowed_installations = int(request.form.get("allowed_installations") or 1)
    except ValueError:
        allowed_installations = 1

    if expiry_date and not _is_valid_date(expiry_date):
        flash("Expiry date must be a valid date (YYYY-MM-DD) - not saved.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    if email and not security.is_plausible_email(email):
        flash("That doesn't look like a valid email address - not saved.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    max_client_accounts, client_limit_error = _parse_client_limit(request.form)
    if client_limit_error:
        flash(f"{client_limit_error} - not saved.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    fields = {}
    if max_client_accounts is not _CLIENT_LIMIT_NOT_SENT:
        fields["max_client_accounts"] = max_client_accounts
    if name:
        fields["name"] = name
    if expiry_date:
        fields["expiry_date"] = expiry_date
    if allowed_installations >= 1:
        fields["allowed_installations"] = allowed_installations
    fields["notes"] = notes
    fields["email"] = email

    repositories.update_admin(conn, admin_id, **fields)
    flash("Admin updated.", "success")
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/delete", methods=["POST"])
@login_required
def delete_admin(admin_id: str):
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        flash("Admin not found.", "error")
        return redirect(url_for("superadmin_web.dashboard"))

    repositories.delete_admin(conn, admin_id)
    flash(
        f"Admin '{admin['name']}' deleted. Their login, devices, and sessions no longer work, "
        "and their email/PC can take a new trial. Their app erases its local client data "
        "the next time it checks in.",
        "success",
    )
    return redirect(url_for("superadmin_web.dashboard"))


@superadmin_bp.route("/admins/<admin_id>/revoke", methods=["POST"])
@login_required
def revoke_admin(admin_id: str):
    conn = db.get_db()
    repositories.set_admin_status(conn, admin_id, "revoked")
    repositories.revoke_all_sessions_for_admin(conn, admin_id)
    flash("Admin revoked. All active sessions were terminated immediately.", "success")
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/reactivate", methods=["POST"])
@login_required
def reactivate_admin(admin_id: str):
    conn = db.get_db()
    repositories.set_admin_status(conn, admin_id, "active")
    flash("Admin reactivated.", "success")
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/reset-password", methods=["POST"])
@login_required
def reset_admin_password(admin_id: str):
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        flash("Admin not found.", "error")
        return redirect(url_for("superadmin_web.dashboard"))

    temp_password = security.generate_temp_password()
    password_hash = security.hash_password(temp_password)
    repositories.set_admin_password(conn, admin_id, password_hash, must_change_password=True)
    repositories.revoke_all_sessions_for_admin(conn, admin_id)

    session["just_created_admin"] = {
        "admin_id": admin_id,
        "username": admin["username"],
        "temp_password": temp_password,
        "expiry_date": admin["expiry_date"],
        "allowed_installations": admin["allowed_installations"],
        "max_client_accounts": admin["max_client_accounts"],
        "is_reset": True,
    }
    return redirect(url_for("superadmin_web.admin_created", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/set-password", methods=["POST"])
@login_required
def set_admin_password(admin_id: str):
    """SuperAdmin chooses the Admin's new password directly (e.g. an Admin who
    forgot theirs asks for a specific one). Same effect as a reset otherwise:
    every session is terminated so the old password stops working everywhere."""
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        flash("Admin not found.", "error")
        return redirect(url_for("superadmin_web.dashboard"))

    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    must_change = request.form.get("must_change_password") == "1"

    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))
    if new_password != confirm_password:
        flash("The two passwords do not match.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    password_hash = security.hash_password(new_password)
    repositories.set_admin_password(conn, admin_id, password_hash, must_change_password=must_change)
    repositories.revoke_all_sessions_for_admin(conn, admin_id)

    flash(
        f"Password for '{admin['username']}' updated. All their active sessions were terminated"
        + (" and they must change it on next login." if must_change else "."),
        "success",
    )
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/devices/<device_id>/revoke", methods=["POST"])
@login_required
def revoke_device(admin_id: str, device_id: str):
    conn = db.get_db()
    repositories.revoke_device(conn, device_id)
    flash("Device revoked. Its session was terminated and its installation slot is now free.", "success")
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


@superadmin_bp.route("/admins/<admin_id>/devices/<device_id>/reactivate", methods=["POST"])
@login_required
def reactivate_device(admin_id: str, device_id: str):
    conn = db.get_db()
    conn2 = db.get_db()
    active_count = repositories.count_active_devices(conn2, admin_id)
    admin = repositories.get_admin(conn2, admin_id)
    if admin and active_count >= admin["allowed_installations"]:
        flash(
            "Cannot reactivate: allowed installation limit already reached. "
            "Revoke another device first, or increase the installation limit.",
            "error",
        )
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    repositories.reactivate_device(conn, device_id)
    flash("Device reactivated.", "success")
    return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))


# --------------------------------------------------------------------------
# Per-Admin EXE builds
# --------------------------------------------------------------------------


@superadmin_bp.route("/admins/<admin_id>/build-exe", methods=["POST"])
@login_required
def build_exe(admin_id: str):
    """Kick off a build and return its job id. Returns immediately - the
    build itself takes minutes and is polled via build_exe_status."""
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        return jsonify({"error": "not_found", "message": "Admin not found."}), 404

    problems = exe_builder.preflight()
    if problems:
        return jsonify({"error": "not_buildable", "message": problems[0]}), 400

    if exe_builder.is_build_in_progress():
        return jsonify(
            {
                "error": "busy",
                "message": "Another EXE build is already running. Builds run one at a time - wait for it to finish.",
            }
        ), 409

    server_url = delivery_server_url()
    job = exe_builder.start_build(dict(admin), server_url)
    return jsonify(job.to_dict()), 202


@superadmin_bp.route("/admins/<admin_id>/build-exe/status")
@login_required
def build_exe_status(admin_id: str):
    job_id = request.args.get("job_id")
    job = exe_builder.get_job(job_id) if job_id else exe_builder.get_latest_job_for_admin(admin_id)
    if job is None or job.admin_id != admin_id:
        return jsonify({"status": "none"})
    return jsonify(job.to_dict())


@superadmin_bp.route("/admins/<admin_id>/build-exe/download")
@login_required
def download_exe(admin_id: str):
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    if admin is None:
        flash("Admin not found.", "error")
        return redirect(url_for("superadmin_web.dashboard"))

    path = exe_builder.find_existing_exe(admin_id)
    if path is None:
        flash("No EXE has been built for this Admin yet.", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))

    return send_file(str(path), as_attachment=True, download_name=path.name)


# --------------------------------------------------------------------------
# Admin App Logs (fetched from the Admin's app on request)
# --------------------------------------------------------------------------


@superadmin_bp.route("/admins/<admin_id>/logs/request", methods=["POST"])
@login_required
def request_admin_logs(admin_id: str):
    conn = db.get_db()
    if repositories.get_admin(conn, admin_id) is None:
        return jsonify({"error": "not_found", "message": "Admin not found."}), 404
    data = request.get_json(silent=True) or request.form
    device_id = (data.get("device_id") or "").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()
    device = repositories.get_device(conn, device_id) if device_id else None
    if device is None or device["admin_id"] != admin_id:
        return jsonify({"error": "bad_device", "message": "Pick one of this Admin's servers."}), 400
    if device["status"] != "active":
        return jsonify(
            {"error": "bad_device", "message": "That server is revoked - its app can't send logs."}
        ), 400
    problem = log_requests.validate_range(date_from, date_to)
    if problem:
        return jsonify({"error": "bad_range", "message": problem}), 400
    try:
        item = log_requests.create_request(conn, admin_id, device_id, date_from, date_to)
    except ValueError as exc:
        return jsonify({"error": "busy", "message": str(exc)}), 409
    return jsonify(_log_request_json(admin_id, item)), 202


def _log_request_json(admin_id: str, item: dict) -> dict:
    body = log_requests.to_public(item)
    body["status_url"] = url_for("superadmin_web.admin_logs_status", admin_id=admin_id, request_id=item["id"])
    body["download_url"] = url_for(
        "superadmin_web.download_admin_logs", admin_id=admin_id, request_id=item["id"]
    )
    return body


def _admin_log_request(conn, admin_id: str, request_id: str) -> dict | None:
    item = log_requests.get_request(conn, request_id)
    if item is None or item["admin_id"] != admin_id:
        return None
    return item


@superadmin_bp.route("/admins/<admin_id>/logs/<request_id>/status")
@login_required
def admin_logs_status(admin_id: str, request_id: str):
    item = _admin_log_request(db.get_db(), admin_id, request_id)
    if item is None:
        return jsonify({"error": "not_found", "message": "No such log request."}), 404
    return jsonify(_log_request_json(admin_id, item))


@superadmin_bp.route("/admins/<admin_id>/logs/<request_id>/download")
@login_required
def download_admin_logs(admin_id: str, request_id: str):
    conn = db.get_db()
    admin = repositories.get_admin(conn, admin_id)
    item = _admin_log_request(conn, admin_id, request_id)
    if admin is None or item is None or not log_requests.to_public(item)["downloadable"]:
        flash("Those logs are not available (not ready yet, or older than 3 days).", "error")
        return redirect(url_for("superadmin_web.admin_detail", admin_id=admin_id))
    device = repositories.get_device(conn, item["device_id"]) if item["device_id"] else None
    errors_only = request.args.get("only") == "errors"
    safe_username = re.sub(r"[^A-Za-z0-9_.-]", "_", admin["username"] or "admin")
    kind = "app_errors" if errors_only else "app_log"
    name = f"{kind}_{safe_username}_{item['date_from']}_to_{item['date_to']}.txt"
    return Response(
        stream_with_context(
            log_requests.iter_download(item, dict(admin), dict(device) if device else None, errors_only)
        ),
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@superadmin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    conn = db.get_db()

    if request.method == "POST":
        form_name = request.form.get("form_name")

        if form_name == "owner_notification_email":
            value = (request.form.get("owner_notification_email") or "").strip()
            if not value:
                repositories.delete_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
                flash("Owner notification email cleared.", "success")
            elif not security.is_plausible_email(value):
                flash("That doesn't look like a valid email address.", "error")
            else:
                repositories.set_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL, value)
                flash("Owner notification email saved.", "success")
            return redirect(url_for("superadmin_web.settings_page"))

        if form_name == "test_email":
            to = (
                repositories.get_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
                or config.OWNER_NOTIFICATION_EMAIL
            )
            if not to:
                flash("Set your notification email first - the test email goes there.", "error")
            else:
                try:
                    email_notifications.send_email(
                        to_addrs=[to],
                        subject="[StakBridge] Test email",
                        body=(
                            "This is a test email from your StakBridge licence server.\n\n"
                            f"Sent as {config.SMTP_FROM_ADDRESS} via {config.SMTP_HOST}.\n"
                            "If you can read this, trial codes, welcome emails, trial-signup alerts "
                            "and expiry reminders can be sent.\n"
                        ),
                    )
                    flash(f"Test email sent to {to}. Check that inbox (and its spam folder).", "success")
                except Exception as exc:
                    flash(f"The test email could not be sent: {exc}", "error")
            return redirect(url_for("superadmin_web.settings_page"))

        if form_name == "support_contact":
            support_email = (request.form.get("support_email") or "").strip()
            whatsapp_url = (request.form.get("support_whatsapp_url") or "").strip()
            if support_email and not security.is_plausible_email(support_email):
                flash("That doesn't look like a valid support email address - not saved.", "error")
            elif whatsapp_url and not _is_plausible_url(whatsapp_url):
                flash("The WhatsApp link must start with https:// (e.g. https://wa.me/91...) - not saved.", "error")
            else:
                for key, value in (
                    (repositories.SETTING_SUPPORT_EMAIL, support_email),
                    (repositories.SETTING_SUPPORT_WHATSAPP_URL, whatsapp_url),
                ):
                    if value:
                        repositories.set_setting(conn, key, value)
                    else:
                        repositories.delete_setting(conn, key)
                flash("Support contact saved. Admins' apps pick it up at their next license check.", "success")
            return redirect(url_for("superadmin_web.settings_page"))

        if form_name == "allow_trial_device":
            fingerprint = (request.form.get("fingerprint") or "").strip()
            if fingerprint and repositories.delete_trial_device(conn, fingerprint):
                flash("Done - that PC can now start a new free trial.", "success")
            else:
                flash("That device record was not found (it may already be cleared).", "error")
            return redirect(url_for("superadmin_web.settings_page"))

        if form_name == "rebuild_trial_exe":
            problems = exe_builder.preflight()
            if problems:
                flash(f"Cannot build the trial EXE on this server: {problems[0]}", "error")
            else:
                exe_builder.ensure_trial_exe(delivery_server_url(), force=True)
                flash("Trial EXE build started - this takes a few minutes.", "success")
            return redirect(url_for("superadmin_web.settings_page"))

        value = (request.form.get("public_server_url") or "").strip().rstrip("/")
        if not value:
            repositories.delete_setting(conn, repositories.SETTING_PUBLIC_SERVER_URL)
            flash("Server address cleared - falling back to the automatic value.", "success")
        elif not _is_plausible_url(value):
            flash(
                "That does not look like a URL. It must start with http:// or https:// "
                "and include a host, e.g. https://license.yourcompany.com",
                "error",
            )
        else:
            repositories.set_setting(conn, repositories.SETTING_PUBLIC_SERVER_URL, value)
            # The trial EXE is server-managed, so re-point it right away
            # instead of waiting for the next prospect to need it.
            exe_builder.ensure_trial_exe(value)
            flash("Server address saved. It applies to EXEs built from now on.", "success")
        return redirect(url_for("superadmin_web.settings_page"))

    stored = repositories.get_setting(conn, repositories.SETTING_PUBLIC_SERVER_URL)
    stored_owner_email = repositories.get_setting(conn, repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
    trial_exe = exe_builder.find_trial_exe()
    return render_template(
        "settings.html",
        stored_url=stored or "",
        effective_url=delivery_server_url(),
        env_url=config.PUBLIC_SERVER_URL,
        auto_url=request.host_url.rstrip("/"),
        stored_owner_email=stored_owner_email or "",
        env_owner_email=config.OWNER_NOTIFICATION_EMAIL or "",
        smtp_configured=config.smtp_is_configured(),
        smtp_host=config.SMTP_HOST or "",
        smtp_from=config.SMTP_FROM_ADDRESS or "",
        trial_exe_name=trial_exe.name if trial_exe else None,
        trial_exe_current=exe_builder.find_trial_exe(delivery_server_url()) is not None,
        trial_build_info=exe_builder.read_build_info(exe_builder.TRIAL_BUILD_ID),
        trial_job=exe_builder.get_latest_job_for_admin(exe_builder.TRIAL_BUILD_ID),
        trial_build_problems=exe_builder.preflight(),
        trial_signup_url=url_for("trial_signup.start", _external=True),
        trial_length_days=config.TRIAL_LENGTH_DAYS,
        stored_support_email=repositories.get_setting(conn, repositories.SETTING_SUPPORT_EMAIL) or "",
        stored_support_whatsapp_url=repositories.get_setting(conn, repositories.SETTING_SUPPORT_WHATSAPP_URL) or "",
        default_support_email=config.SUPPORT_EMAIL,
        default_support_whatsapp_url=config.SUPPORT_WHATSAPP_URL,
        trial_devices=repositories.list_trial_devices(conn),
    )
