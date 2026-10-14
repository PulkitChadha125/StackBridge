"""Command line: `python -m stakbridge serve | set-password | create-token`."""

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

    token = commands.add_parser("create-token", help="create an API token for a signal program (shown once)")
    token.add_argument("--label", default="")

    args = parser.parse_args()

    from .config import load_settings
    from .db import Database

    settings = load_settings()
    db = Database(settings.db_path)

    if args.command == "create-token":
        from .security import hash_token, new_api_token

        value = new_api_token()
        db.add_token(hash_token(value), args.label)
        print("New API token for programs that send trades (stored only as a hash, save it now):")
        print(f"   {value}")
        print("Send it in every request as:  Authorization: Bearer <token>")
        return

    if args.command == "set-password":
        set_password(db)
        return

    import uvicorn

    from .api import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("stakbridge")
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
