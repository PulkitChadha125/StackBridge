"""SuperAdmin Control Plane entry point.

Run from source:
    python -m superadmin_panel.app

Binds to 127.0.0.1 by default. Set SUPERADMIN_HOST=0.0.0.0 (and open the
matching firewall port - setup_server.bat does both) to make it
reachable from other devices directly, e.g. an Admin's EXE or a phone
browser calling in over the internet. This ships without TLS/HTTPS - traffic
including login credentials travels in plain text - see TESTING.md/README.md
for the (currently not applied) reverse-proxy hardening notes if that
changes later.
"""

from __future__ import annotations

import os

from flask import Flask

from . import config, db


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = config.get_flask_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    db.init_db()
    app.teardown_appcontext(db.close_db)

    from .auth_api import auth_api_bp
    from .client_usage_api import client_usage_api_bp
    from .log_requests import log_request_api_bp
    from .superadmin_web import superadmin_bp
    from .telemetry_api import telemetry_api_bp
    from .trial_signup import trial_signup_bp

    app.register_blueprint(superadmin_bp)
    app.register_blueprint(auth_api_bp)
    app.register_blueprint(client_usage_api_bp)
    app.register_blueprint(telemetry_api_bp)
    app.register_blueprint(log_request_api_bp)
    app.register_blueprint(trial_signup_bp)

    # Off unless explicitly enabled, so importing this module (tests, tooling)
    # never starts a thread that sends real email. Production WSGI servers -
    # waitress-serve superadmin_panel.app:app - never run the __main__ block
    # below, so this env var is how the reminder loop gets started there.
    if os.environ.get("EXPIRY_REMINDER_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        from . import expiry_reminder

        expiry_reminder.start_background_thread()

    return app


app = create_app()

if __name__ == "__main__":
    from . import expiry_reminder

    expiry_reminder.start_background_thread()

    host = os.environ.get("SUPERADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("SUPERADMIN_PORT", "5070"))
    app.run(host=host, port=port, debug=False)
