"""Command line: `python -m stakbridge serve | set-password`."""

import argparse
import getpass
import logging
import sys

MIN_PASSWORD = 8


def main() -> None:
    parser = argparse.ArgumentParser(prog="stakbridge")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the gateway, workers and web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--ssl-certfile", help="enables HTTPS together with --ssl-keyfile")
    serve.add_argument("--ssl-keyfile")

    commands.add_parser("set-password", help="set or reset the web UI password (signs out all browsers)")

    args = parser.parse_args()

    from .config import load_settings
    from .db import Database

    settings = load_settings()
    db = Database(settings.db_path)

    if args.command == "set-password":
        from .licensing import paths

        if paths.server_url():
            print("This StakBridge signs in with a licence: use 'Forgot password' on the sign-in page instead.")
            return
        set_password(db)
        return

    import uvicorn

    from . import logfiles
    from .api import create_app
    from .licensing import paths

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    licence_server = paths.server_url()
    logfiles.install((paths.device_root() if licence_server else settings.data_dir) / "logs")
    log = logging.getLogger("stakbridge")
    if licence_server:
        log.info("licence server: %s  (each Admin's data is kept under %s)", licence_server,
                 paths.device_root() / paths.ADMINS_DIR)
    else:
        log.info("data folder: %s  backend: %s", settings.data_dir, settings.backend)
    log.info("open http%s://%s:%s in your browser", "s" if args.ssl_certfile else "", args.host, args.port)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


def set_password(db) -> None:
    from .api import PASSWORD_KEY
    from .security import hash_password

    while True:
        first = getpass.getpass(f"New web UI password (at least {MIN_PASSWORD} characters): ")
        if len(first) < MIN_PASSWORD:
            print("Too short, try again.")
            continue
        if getpass.getpass("Type it again: ") != first:
            print("The two passwords differ, try again.")
            continue
        break
    db.set_state(PASSWORD_KEY, hash_password(first))
    db.delete_all_sessions()
    print("Password saved. All browsers were signed out, sign in again with the new password.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
