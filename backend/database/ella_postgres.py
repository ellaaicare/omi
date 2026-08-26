"""Shared PostgreSQL pool for canonical Ella backend artifacts."""

from __future__ import annotations

import os
from typing import Optional

import asyncpg

from database.honcho_attestation import authority_credential

_pool: Optional[asyncpg.Pool] = None


async def get_ella_postgres_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
            database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
            min_size=1,
            max_size=10,
        )
    return _pool


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
