"""Async Postgres pool (psycopg3).

Serverless note: Supabase's session-mode pooler (port 5432) caps the whole
project at a handful of clients. Vercel keeps many warm function instances,
each with its own pool, which exhausts that cap and makes every DB call fail
with "(EMAXCONNSESSION) max clients reached in session mode". On serverless we
therefore rewrite the Supabase pooler URL to the transaction pooler (6543),
keep the per-instance pool tiny, and disable server-side prepared statements
(transaction pooling does not preserve them between transactions).
"""
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import settings

pool: AsyncConnectionPool | None = None


def runtime_dsn() -> str:
    """Return the DSN the API should use.

    Only rewrites Supabase's pooler host + port; every other URL (local
    Postgres, direct connection, already-transaction pooler) is untouched.
    """
    dsn = settings.database_url
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        if host.endswith(".pooler.supabase.com") and parts.port == 5432:
            userinfo, sep, _ = parts.netloc.rpartition("@")
            netloc = f"{userinfo}@{host}:6543" if sep else f"{host}:6543"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return dsn


async def get_pool() -> AsyncConnectionPool:
    global pool
    if pool is None:
        pool = AsyncConnectionPool(
            conninfo=runtime_dsn(),
            open=False,
            min_size=0,
            max_size=max(1, int(settings.db_pool_max_size or 2)),
            timeout=15,
            max_idle=60,
            kwargs={"prepare_threshold": None},
        )
        await pool.open()
    return pool


async def close_pool() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None
