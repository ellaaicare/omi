"""Shared PostgreSQL pool for canonical Ella backend artifacts."""

from __future__ import annotations

import os
from typing import Optional

import asyncpg

from database.honcho_attestation import authority_credential

_pool: Optional[asyncpg.Pool] = None


def _connection_kwargs() -> dict:
    return dict(
        host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
        user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
        password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
        database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
    )


async def get_ella_postgres_pool() -> asyncpg.Pool:
    """The shared pool bound to whichever event loop first calls this.

    Only ever await this from the application's main event loop. A caller
    that runs on its own dedicated loop (e.g. a background thread) must use
    `create_dedicated_ella_postgres_pool()` instead — asyncpg pools are not
    portable across event loops, and mixing the two here would silently
    bind this singleton to the wrong loop.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(**_connection_kwargs(), min_size=1, max_size=10)
    return _pool


async def create_dedicated_ella_postgres_pool(*, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    """A standalone pool, independent of the shared `get_ella_postgres_pool()` singleton.

    For callers that run on their own dedicated, long-lived event loop
    (never the shared singleton's loop).
    """
    return await asyncpg.create_pool(**_connection_kwargs(), min_size=min_size, max_size=max_size)


async def open_ella_postgres_connection() -> asyncpg.Connection:
    """Open a loop-local session for connection-scoped advisory locks."""
    dsn = os.getenv("ELLA_POSTGRES_DSN", "").strip()
    if dsn:
        return await asyncpg.connect(dsn=dsn)
    return await asyncpg.connect(
        host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
        user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
        password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
        database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
    )
