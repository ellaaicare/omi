"""Durable Observer run logs."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import asyncpg

from ella.services.observer import ObserverRunLog, observer_log_to_dict

_pool: Optional[asyncpg.Pool] = None


def _log_to_python(log: ObserverRunLog) -> dict[str, Any]:
    if hasattr(log, "model_dump"):
        return log.model_dump(mode="python")
    return log.dict()


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
            min_size=1,
            max_size=5,
        )
    return _pool


class ObserverRunLogStore:
    async def save(self, log: ObserverRunLog) -> ObserverRunLog:
        raise NotImplementedError

    async def get(self, run_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class InMemoryObserverRunLogStore(ObserverRunLogStore):
    def __init__(self) -> None:
        self.logs: dict[str, dict[str, Any]] = {}

    async def save(self, log: ObserverRunLog) -> ObserverRunLog:
        self.logs[log.run_id] = observer_log_to_dict(log)
        return log

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return self.logs.get(run_id)


class PostgresObserverRunLogStore(ObserverRunLogStore):
    async def _ensure_table(self) -> None:
        pool = await _get_pool()
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS observer_run_logs (
                run_id TEXT PRIMARY KEY,
                profile_uid TEXT NOT NULL,
                canonical_identity TEXT,
                dry_run BOOLEAN NOT NULL DEFAULT TRUE,
                status TEXT NOT NULL,
                cursor_before TEXT,
                cursor_after TEXT,
                source_event_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                proposal_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                source_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
                proposal_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
                model_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """)

    async def save(self, log: ObserverRunLog) -> ObserverRunLog:
        await self._ensure_table()
        data = _log_to_python(log)
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO observer_run_logs (
                run_id, profile_uid, canonical_identity, dry_run, status,
                cursor_before, cursor_after, source_event_count, candidate_count,
                proposal_count, skipped_count, error_count, source_counts,
                proposal_ids, decisions, model_metadata, started_at, completed_at,
                latency_ms
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12, $13::jsonb,
                $14::jsonb, $15::jsonb, $16::jsonb, $17, $18,
                $19
            )
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                cursor_after = EXCLUDED.cursor_after,
                source_event_count = EXCLUDED.source_event_count,
                candidate_count = EXCLUDED.candidate_count,
                proposal_count = EXCLUDED.proposal_count,
                skipped_count = EXCLUDED.skipped_count,
                error_count = EXCLUDED.error_count,
                source_counts = EXCLUDED.source_counts,
                proposal_ids = EXCLUDED.proposal_ids,
                decisions = EXCLUDED.decisions,
                model_metadata = EXCLUDED.model_metadata,
                completed_at = EXCLUDED.completed_at,
                latency_ms = EXCLUDED.latency_ms
            """,
            data["run_id"],
            data["profile_uid"],
            data.get("canonical_identity") or "",
            data["dry_run"],
            data["status"],
            data.get("cursor_before") or "",
            data.get("cursor_after") or "",
            data["source_event_count"],
            data["candidate_count"],
            data["proposal_count"],
            data["skipped_count"],
            data["error_count"],
            json.dumps(data.get("source_counts") or {}),
            json.dumps(data.get("proposal_ids") or []),
            json.dumps(data.get("decisions") or []),
            json.dumps(data.get("model_metadata") or {}),
            data["started_at"],
            data["completed_at"],
            data["latency_ms"],
        )
        return log

    async def get(self, run_id: str) -> dict[str, Any] | None:
        await self._ensure_table()
        pool = await _get_pool()
        row = await pool.fetchrow("SELECT * FROM observer_run_logs WHERE run_id = $1", run_id)
        if not row:
            return None

        def json_value(name: str, default: Any) -> Any:
            raw = row[name]
            if raw is None:
                return default
            if isinstance(raw, str):
                return json.loads(raw)
            return raw

        return {
            "run_id": row["run_id"],
            "profile_uid": row["profile_uid"],
            "canonical_identity": row["canonical_identity"] or "",
            "dry_run": row["dry_run"],
            "status": row["status"],
            "cursor_before": row["cursor_before"] or "",
            "cursor_after": row["cursor_after"] or "",
            "source_event_count": row["source_event_count"],
            "candidate_count": row["candidate_count"],
            "proposal_count": row["proposal_count"],
            "skipped_count": row["skipped_count"],
            "error_count": row["error_count"],
            "source_counts": json_value("source_counts", {}),
            "proposal_ids": json_value("proposal_ids", []),
            "decisions": json_value("decisions", []),
            "model_metadata": json_value("model_metadata", {}),
            "started_at": row["started_at"].isoformat(),
            "completed_at": row["completed_at"].isoformat(),
            "latency_ms": row["latency_ms"],
            "inserted_at": row["inserted_at"].isoformat(),
        }
