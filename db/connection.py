"""Postgres connection helpers for the ECHO pipeline.

A thin wrapper over psycopg 3. The rest of the pipeline imports `connect()`
(a context-managed connection) and the small `fetch_*` / `execute` conveniences
rather than reaching for psycopg directly, so connection config lives in exactly
one place.

Connection config resolves in this order:
  1. DATABASE_URL, if set (e.g. a hosted Postgres connection string)
  2. POSTGRES_* parts (USER / PASSWORD / DB / HOST / PORT), matching
     docker-compose.yml's defaults
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ModuleNotFoundError:  # dotenv is optional at runtime
    pass


def database_url() -> str:
    """Build the Postgres connection string from the environment."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    user = os.getenv("POSTGRES_USER", "echo")
    password = os.getenv("POSTGRES_PASSWORD", "echo")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "echo")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@contextmanager
def connect(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a psycopg connection whose rows come back as dicts.

    Commits on clean exit, rolls back on exception, and always closes.
    """
    conn = psycopg.connect(database_url(), row_factory=dict_row, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(query: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a read query and return every row as a list of dicts."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fetch_one(query: str, params: Sequence[Any] | dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Run a read query and return the first row, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def execute(query: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    """Run a write query and return the affected row count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount
