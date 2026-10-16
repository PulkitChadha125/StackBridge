"""One-time (or re-run anytime) SuperAdmin credential setup.

Usage:
    python -m superadmin_panel.bootstrap_superadmin --username admin --password "S3cure!Pass"

Or set SUPERADMIN_USERNAME / SUPERADMIN_PASSWORD env vars and run with no args.

Only the Argon2id hash of the password is ever written to disk
(superadmin_config.json, git-ignored) - the plaintext password is never
persisted.
"""

from __future__ import annotations

import argparse
import os

from . import config
from .security import hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description="Set/reset the SuperAdmin login credentials.")
    parser.add_argument("--username", default=os.environ.get("SUPERADMIN_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("SUPERADMIN_PASSWORD"))
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error(
            "Provide --username/--password, or set SUPERADMIN_USERNAME/SUPERADMIN_PASSWORD"
        )

    password_hash = hash_password(args.password)
    config.save_superadmin_credentials(args.username, password_hash)
    print(f"SuperAdmin credentials saved for username '{args.username}'.")
    print(f"Config file: {config.CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
