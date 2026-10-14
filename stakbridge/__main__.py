"""Command line: `python -m stakbridge serve` or `python -m stakbridge create-token`."""

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="stakbridge")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the gateway, workers and web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--ssl-certfile", help="enables HTTPS together with --ssl-keyfile")
    serve.add_argument("--ssl-keyfile")

    token = commands.add_parser("create-token", help="create an API token (shown once)")
    token.add_argument("--label", default="")

    args = parser.parse_args()

    from .config import load_settings

    settings = load_settings()

    if args.command == "create-token":
        from .db import Database
        from .security import hash_token, new_api_token

        value = new_api_token()
        Database(settings.db_path).add_token(hash_token(value), args.label)
        print("New API token (it is stored only as a hash, save it now):")
        print(value)
        return

    import uvicorn

    from .api import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
