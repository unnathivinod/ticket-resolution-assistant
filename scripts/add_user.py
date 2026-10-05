"""Add a sign-in account, change its password or role, or switch it off.

  docker compose run --rm tools python scripts/add_user.py kavya agent "Kavya M"
  docker compose run --rm tools python scripts/add_user.py priya --off     # the account can no longer sign in

The script asks for the password and stores only its salted hash. Roles:
  agent      resolves complaints, sees only their own cases
  expert     also records fixes and sees every case
  engineer   also gets the monitoring link and sees every case
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import psycopg

from services.gateway.auth import ROLES, hash_password

MIN_PASSWORD = 8


def main() -> None:
    parser = argparse.ArgumentParser(description="Add or change a sign-in account.")
    parser.add_argument("username")
    parser.add_argument("role", nargs="?", choices=ROLES)
    parser.add_argument("name", nargs="?", help='the name shown on the page, for example "Kavya M"')
    parser.add_argument("--off", action="store_true", help="switch the account off instead")
    args = parser.parse_args()
    username = args.username.strip().lower()

    if not args.off and not (args.role and args.name):
        parser.error('give a role and a name, for example: kavya agent "Kavya M"')
    password = ""
    if not args.off:
        password = os.environ.get("NEW_PASSWORD") or getpass.getpass(f"Password for {username}: ")
        if len(password) < MIN_PASSWORD:
            sys.exit(f"The password must have at least {MIN_PASSWORD} characters.")

    # Leaving the "with" block saves the change. Nothing below may exit from inside it.
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30) as conn:
        if args.off:
            found = conn.execute(
                "UPDATE users SET is_active = FALSE WHERE username = %s", (username,)
            ).rowcount
        else:
            conn.execute(
                """INSERT INTO users (username, display_name, role, password_hash)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (username) DO UPDATE SET
                       display_name = EXCLUDED.display_name, role = EXCLUDED.role,
                       password_hash = EXCLUDED.password_hash, is_active = TRUE""",
                (username, args.name, args.role, hash_password(password)),
            )
            found = 1
    if not found:
        sys.exit(f"There is no account called {username}.")
    print(f"{username} can no longer sign in." if args.off else f"{username} can now sign in as {args.role}.")


if __name__ == "__main__":
    main()
