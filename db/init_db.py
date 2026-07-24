"""Apply db/schema.sql to the configured database.

Idempotent — schema.sql uses IF NOT EXISTS throughout, so this is safe to run
against an existing database to pick up new objects without wiping data. On a
fresh docker-compose volume the schema is already applied via the init mount;
this script is the way to (re)apply it to any other target.

Usage:
    python -m db.init_db          # apply schema
    python -m db.init_db --check  # print tables + row counts, don't modify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .connection import connect, database_url

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def apply_schema() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
    print(f"Applied {SCHEMA_PATH.name} to {_safe_target()}")


def check() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select table_name
            from information_schema.tables
            where table_schema = 'public'
            order by table_name
            """
        )
        tables = [row["table_name"] for row in cur.fetchall()]
        if not tables:
            print("No tables found — run `python -m db.init_db` first.")
            return
        print(f"Target: {_safe_target()}")
        for table in tables:
            cur.execute(f"select count(*) as n from {table}")  # table names from catalog, not user input
            print(f"  {table:<12} {cur.fetchone()['n']} rows")


def _safe_target() -> str:
    """The database URL with any password redacted, for logging."""
    url = database_url()
    if "@" in url and "//" in url:
        prefix, rest = url.split("//", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{prefix}//{user}:***@{host}"
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or inspect the ECHO database schema.")
    parser.add_argument("--check", action="store_true", help="list tables and row counts without modifying")
    args = parser.parse_args()

    try:
        if args.check:
            check()
        else:
            apply_schema()
    except Exception as exc:  # surface a clean error, not a traceback wall
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
