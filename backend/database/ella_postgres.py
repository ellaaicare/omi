"""Shared PostgreSQL pool for canonical Ella backend artifacts."""

from __future__ import annotations

import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def get_ella_postgres_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
            min_size=1,
            max_size=10,
        )
    return _pool
