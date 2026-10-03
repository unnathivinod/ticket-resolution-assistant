"""Bring an existing database up to date with the current schema.

  docker compose run --rm tools python scripts/migrate.py

A brand-new database gets these files automatically when it is first created.
This script is for a database that already holds data. It is safe to run again.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[1] / "infra" / "postgres" / "migrations"


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every migration file in name order. Returns the names of the files applied."""
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    conn.commit()
    return applied


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30) as conn:
        for name in migrate(conn):
            print(f"applied {name}")
    print("The database schema is up to date.")


if __name__ == "__main__":
    main()
