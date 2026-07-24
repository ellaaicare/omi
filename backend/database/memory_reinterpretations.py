"""Postgres repository for memory-scoped post-session reinterpretation jobs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TERMINAL_STATUSES = {
    "no_change",
    "pending_review",
    "applied",
    "conflict",
    "dead_letter",
}
RUNNABLE_STATUSES = {"pending", "retry"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def deterministic_job_id(
    uid: str,
    logical_session_id: str,
    conversation_id: str,
    starting_summary_version_id: str,
) -> str:
    material = _stable_json([uid, logical_session_id, conversation_id, starting_summary_version_id]).encode("utf-8")
    return f"memory-reinterpretation-{hashlib.sha256(material).hexdigest()[:32]}"


def canonical_transcript_material(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": str(row.get("event_id") or ""),
            "source_identity": str(row.get("source_identity") or ""),
            "connection_id": str(row.get("connection_id") or ""),
            "turn_index": int(row.get("turn_index") or 0),
            "role": str(row.get("role") or ""),
            "text": str(row.get("text") or ""),
            "started_at": _iso(row.get("started_at")) or "",
        }
        for row in rows
    ]


def canonical_transcript_hash(rows: list[dict[str, Any]]) -> str:
    payload = _stable_json(canonical_transcript_material(rows)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_refs(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "event_id": str(row.get("event_id") or ""),
            "source_identity": str(row.get("source_identity") or ""),
        }
        for row in rows
    ]


def row_to_job(row: Any) -> dict[str, Any]:
    return {
        "id": _row_value(row, "id"),
        "uid": _row_value(row, "uid"),
        "logical_session_id": _row_value(row, "logical_session_id"),
        "conversation_id": _row_value(row, "conversation_id"),
        "starting_summary_version_id": _row_value(row, "starting_summary_version_id"),
        "source_identity": _row_value(row, "source_identity"),
        "canonical_refs": _json_value(_row_value(row, "canonical_refs"), []),
        "transcript_hash": _row_value(row, "transcript_hash"),
        "status": _row_value(row, "status"),
        "outcome": _row_value(row, "outcome"),
        "proposal_plan": _json_value(_row_value(row, "proposal_plan"), None),
        "progress": _json_value(_row_value(row, "progress"), {}),
        "proposal_ids": _json_value(_row_value(row, "proposal_ids"), []),
        "correction_ids": _json_value(_row_value(row, "correction_ids"), []),
        "receipt_refs": _json_value(_row_value(row, "receipt_refs"), []),
        "not_before": _row_value(row, "not_before"),
        "lease_owner": _row_value(row, "lease_owner"),
        "lease_token": _row_value(row, "lease_token"),
        "lease_expires_at": _row_value(row, "lease_expires_at"),
        "attempt_count": int(_row_value(row, "attempt_count", 0) or 0),
        "max_attempts": int(_row_value(row, "max_attempts", 5) or 5),
        "last_error_code": _row_value(row, "last_error_code"),
        "last_error_detail": _row_value(row, "last_error_detail"),
        "completed_at": _row_value(row, "completed_at"),
        "created_at": _row_value(row, "created_at"),
        "updated_at": _row_value(row, "updated_at"),
    }


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return identifier/status data only; never expose transcript or proposal content."""
    return {
        "job_id": job.get("id"),
        "session_id": job.get("logical_session_id"),
        "conversation_id": job.get("conversation_id"),
        "starting_summary_version_id": job.get("starting_summary_version_id"),
        "status": job.get("status"),
        "outcome": job.get("outcome"),
        "proposal_ids": list(job.get("proposal_ids") or []),
        "correction_ids": list(job.get("correction_ids") or []),
        "receipts": list(job.get("receipt_refs") or []),
        "attempt_count": int(job.get("attempt_count") or 0),
        "error_code": job.get("last_error_code"),
        "not_before": _iso(job.get("not_before")),
        "completed_at": _iso(job.get("completed_at")),
        "created_at": _iso(job.get("created_at")),
        "updated_at": _iso(job.get("updated_at")),
    }


class PostgresMemoryReinterpretationRepository:
    def __init__(
        self,
        pool_getter,
        *,
        debounce_seconds: Optional[int] = None,
        max_attempts: Optional[int] = None,
    ):
        self._pool_getter = pool_getter
        configured_debounce = (
            debounce_seconds
            if debounce_seconds is not None
            else int(os.getenv("ELLA_MEMORY_REINTERPRETATION_DEBOUNCE_SECONDS", "45"))
        )
        self.debounce_seconds = min(max(configured_debounce, 5), 300)
        self.max_attempts = max(
            1,
            (
                max_attempts
                if max_attempts is not None
                else int(os.getenv("ELLA_MEMORY_REINTERPRETATION_MAX_ATTEMPTS", "5"))
            ),
        )

    async def _pool(self):
        return await self._pool_getter()

    @staticmethod
    async def _session_rows(conn, *, uid: str, session_id: str) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT event_id, source_identity, uid, session_id, role, text, started_at,
                   COALESCE(source_ref ->> 'connection_id', metadata ->> 'connection_id', '') AS connection_id,
                   COALESCE(
                       NULLIF(source_ref ->> 'turn_index', '')::integer,
                       NULLIF(metadata ->> 'turn_index', '')::integer,
                       0
                   ) AS turn_index,
                   COALESCE(source_ref ->> 'scope_kind', metadata ->> 'scope_kind', '') AS scope_kind,
                   COALESCE(source_ref ->> 'conversation_id', metadata ->> 'conversation_id', '') AS conversation_id,
                   COALESCE(
                       source_ref ->> 'active_summary_version_id',
                       metadata ->> 'active_summary_version_id',
                       ''
                   ) AS active_summary_version_id
            FROM canonical_events
            WHERE uid = $1 AND session_id = $2
            ORDER BY started_at ASC, connection_id ASC, turn_index ASC, event_id ASC
            """,
            uid,
            session_id,
        )
        return [dict(row) for row in rows]

    async def enqueue_from_completion(self, conn, completion: dict[str, Any]) -> Optional[dict[str, Any]]:
        source_ref = completion.get("source_ref") if isinstance(completion.get("source_ref"), dict) else {}
        metadata = completion.get("metadata") if isinstance(completion.get("metadata"), dict) else {}
        scope_kind = str(source_ref.get("scope_kind") or metadata.get("scope_kind") or "")
        can_reinterpret = source_ref.get("can_reinterpret")
        if can_reinterpret is None:
            can_reinterpret = metadata.get("can_reinterpret")
        if scope_kind != "memory" or can_reinterpret is not True:
            return None

        uid = str(completion.get("uid") or "")
        session_id = str(completion.get("session_id") or "")
        conversation_id = str(source_ref.get("conversation_id") or metadata.get("conversation_id") or "")
        version_id = str(source_ref.get("active_summary_version_id") or metadata.get("active_summary_version_id") or "")
        if not all((uid, session_id, conversation_id, version_id)):
            return None

        rows = await self._session_rows(conn, uid=uid, session_id=session_id)
        scoped_rows = [
            row
            for row in rows
            if row.get("scope_kind") == "memory"
            and row.get("conversation_id") == conversation_id
            and row.get("active_summary_version_id") == version_id
        ]
        if not scoped_rows or len(scoped_rows) != len(rows):
            return None

        now = _utc_now()
        not_before = now + timedelta(seconds=self.debounce_seconds)
        job_id = deterministic_job_id(uid, session_id, conversation_id, version_id)
        row = await conn.fetchrow(
            """
            INSERT INTO memory_reinterpretation_jobs (
                id, uid, logical_session_id, conversation_id,
                starting_summary_version_id, source_identity,
                canonical_refs, transcript_hash, status, not_before,
                max_attempts, created_at, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7::jsonb, $8, 'pending', $9, $10, $11, $11
            )
            ON CONFLICT (
                uid, logical_session_id, conversation_id, starting_summary_version_id
            )
            DO UPDATE SET
                source_identity = EXCLUDED.source_identity,
                canonical_refs = EXCLUDED.canonical_refs,
                transcript_hash = EXCLUDED.transcript_hash,
                not_before = CASE
                    WHEN memory_reinterpretation_jobs.status IN ('pending', 'retry')
                    THEN GREATEST(memory_reinterpretation_jobs.not_before, EXCLUDED.not_before)
                    ELSE memory_reinterpretation_jobs.not_before
                END,
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            job_id,
            uid,
            session_id,
            conversation_id,
            version_id,
            str(completion.get("source_identity") or ""),
            _stable_json(canonical_refs(scoped_rows)),
            canonical_transcript_hash(scoped_rows),
            not_before,
            self.max_attempts,
            now,
        )
        return row_to_job(row)

    async def claim_due(self, worker_id: str, *, lease_seconds: int = 120) -> Optional[dict[str, Any]]:
        pool = await self._pool()
        lease_token = str(uuid.uuid4())
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM memory_reinterpretation_jobs
                        WHERE (
                                status IN ('pending', 'retry')
                                AND not_before <= NOW()
                              )
                           OR (
                                status = 'running'
                                AND lease_expires_at < NOW()
                              )
                        ORDER BY not_before ASC, created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE memory_reinterpretation_jobs AS jobs
                    SET status = 'running',
                        lease_owner = $1,
                        lease_token = $2,
                        lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                        attempt_count = jobs.attempt_count + 1,
                        updated_at = NOW()
                    FROM candidate
                    WHERE jobs.id = candidate.id
                    RETURNING jobs.*
                    """,
                    worker_id,
                    lease_token,
                    max(10, lease_seconds),
                )
                if row is None:
                    return None
                job = row_to_job(row)
                await conn.execute(
                    """
                    INSERT INTO memory_reinterpretation_attempts (
                        job_id, attempt_number, lease_token, worker_id, status
                    )
                    VALUES ($1, $2, $3, $4, 'running')
                    ON CONFLICT (job_id, attempt_number) DO NOTHING
                    """,
                    job["id"],
                    job["attempt_count"],
                    lease_token,
                    worker_id,
                )
                return job

    async def record_plan(self, job: dict[str, Any], plan: dict[str, Any]) -> bool:
        pool = await self._pool()
        result = await pool.execute(
            """
            UPDATE memory_reinterpretation_jobs
            SET proposal_plan = $3::jsonb,
                updated_at = NOW()
            WHERE id = $1 AND lease_token = $2 AND status = 'running'
            """,
            job["id"],
            job["lease_token"],
            _stable_json(plan),
        )
        return result.endswith("1")

    async def record_progress(
        self,
        job: dict[str, Any],
        *,
        progress: dict[str, Any],
        proposal_ids: list[str],
        correction_ids: list[str],
        receipt_refs: list[dict[str, Any]],
    ) -> bool:
        pool = await self._pool()
        result = await pool.execute(
            """
            UPDATE memory_reinterpretation_jobs
            SET progress = $3::jsonb,
                proposal_ids = $4::jsonb,
                correction_ids = $5::jsonb,
                receipt_refs = $6::jsonb,
                updated_at = NOW()
            WHERE id = $1 AND lease_token = $2 AND status = 'running'
            """,
            job["id"],
            job["lease_token"],
            _stable_json(progress),
            _stable_json(proposal_ids),
            _stable_json(correction_ids),
            _stable_json(receipt_refs),
        )
        return result.endswith("1")

    async def load_canonical_rows(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            foreign_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM canonical_events
                WHERE session_id = $1 AND uid <> $2
                """,
                job["logical_session_id"],
                job["uid"],
            )
            if int(foreign_count or 0):
                raise ValueError("session_owner_collision")
            return await self._session_rows(
                conn,
                uid=job["uid"],
                session_id=job["logical_session_id"],
            )

    async def finish(
        self,
        job: dict[str, Any],
        *,
        status: str,
        outcome: str,
        proposal_ids: list[str],
        correction_ids: list[str],
        receipt_refs: list[dict[str, Any]],
        metrics: Optional[dict[str, Any]] = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE memory_reinterpretation_jobs
                    SET status = $3,
                        outcome = $4,
                        proposal_ids = $5::jsonb,
                        correction_ids = $6::jsonb,
                        receipt_refs = $7::jsonb,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = NULL,
                        last_error_detail = NULL,
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1 AND lease_token = $2 AND status = 'running'
                    """,
                    job["id"],
                    job["lease_token"],
                    status,
                    outcome,
                    _stable_json(proposal_ids),
                    _stable_json(correction_ids),
                    _stable_json(receipt_refs),
                )
                updated = result.endswith("1")
                if updated:
                    await self._finish_attempt(
                        conn,
                        job,
                        status=status,
                        error_code=None,
                        metrics=metrics,
                    )
                return updated

    async def fail_or_retry(
        self,
        job: dict[str, Any],
        *,
        error_code: str,
        error_detail: str = "",
        retryable: bool,
        metrics: Optional[dict[str, Any]] = None,
    ) -> str:
        terminal = not retryable or job["attempt_count"] >= job["max_attempts"]
        status = "dead_letter" if terminal else "retry"
        delay_seconds = min(900, 5 * (2 ** max(job["attempt_count"] - 1, 0)))
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE memory_reinterpretation_jobs
                    SET status = $3,
                        outcome = CASE WHEN $3 = 'dead_letter' THEN 'failed' ELSE outcome END,
                        not_before = CASE
                            WHEN $3 = 'retry'
                            THEN NOW() + ($4 * INTERVAL '1 second')
                            ELSE not_before
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = $5,
                        last_error_detail = $6,
                        completed_at = CASE WHEN $3 = 'dead_letter' THEN NOW() ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = $1 AND lease_token = $2 AND status = 'running'
                    """,
                    job["id"],
                    job["lease_token"],
                    status,
                    delay_seconds,
                    error_code[:120],
                    error_detail[:1000],
                )
                await self._finish_attempt(
                    conn,
                    job,
                    status=status,
                    error_code=error_code,
                    metrics=metrics,
                )
        return status

    @staticmethod
    async def _finish_attempt(
        conn,
        job: dict[str, Any],
        *,
        status: str,
        error_code: Optional[str],
        metrics: Optional[dict[str, Any]],
    ) -> None:
        await conn.execute(
            """
            UPDATE memory_reinterpretation_attempts
            SET status = $4,
                error_code = $5,
                metrics = $6::jsonb,
                finished_at = NOW()
            WHERE job_id = $1 AND attempt_number = $2 AND lease_token = $3
            """,
            job["id"],
            job["attempt_count"],
            job["lease_token"],
            status,
            error_code,
            _stable_json(metrics or {}),
        )

    async def get_for_user(
        self,
        *,
        uid: str,
        conversation_id: str,
        job_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        pool = await self._pool()
        params: list[Any] = [uid, conversation_id]
        job_filter = ""
        if job_id:
            params.append(job_id)
            job_filter = "AND id = $3"
        row = await pool.fetchrow(
            f"""
            SELECT *
            FROM memory_reinterpretation_jobs
            WHERE uid = $1 AND conversation_id = $2 {job_filter}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            *params,
        )
        return row_to_job(row) if row else None

    async def metrics(self) -> dict[str, Any]:
        pool = await self._pool()
        rows = await pool.fetch("""
            SELECT status, COUNT(*) AS count
            FROM memory_reinterpretation_jobs
            GROUP BY status
            """)
        oldest_due_seconds = await pool.fetchval("""
            SELECT EXTRACT(EPOCH FROM (NOW() - MIN(not_before)))
            FROM memory_reinterpretation_jobs
            WHERE status IN ('pending', 'retry') AND not_before <= NOW()
            """)
        return {
            "jobs_by_status": {str(_row_value(row, "status")): int(_row_value(row, "count", 0) or 0) for row in rows},
            "oldest_due_seconds": float(oldest_due_seconds or 0),
        }


class InMemoryMemoryReinterpretationRepository:
    """Deterministic repository used by worker and completion contract tests."""

    def __init__(self, *, debounce_seconds: int = 45, max_attempts: int = 5):
        self.debounce_seconds = debounce_seconds
        self.max_attempts = max_attempts
        self.jobs: dict[str, dict[str, Any]] = {}
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.now = _utc_now()

    def set_rows(self, uid: str, session_id: str, rows: list[dict[str, Any]]) -> None:
        ordered = sorted(
            rows,
            key=lambda row: (
                row.get("started_at") or "",
                row.get("connection_id") or "",
                int(row.get("turn_index") or 0),
                row.get("event_id") or "",
            ),
        )
        self.rows[(uid, session_id)] = ordered

    async def enqueue(self, completion: dict[str, Any], rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        source_ref = completion.get("source_ref") or {}
        metadata = completion.get("metadata") or {}
        if (source_ref.get("scope_kind") or metadata.get("scope_kind")) != "memory" or (
            source_ref.get("can_reinterpret") if "can_reinterpret" in source_ref else metadata.get("can_reinterpret")
        ) is not True:
            return None
        uid = str(completion.get("uid") or "")
        session_id = str(completion.get("session_id") or "")
        conversation_id = str(source_ref.get("conversation_id") or metadata.get("conversation_id") or "")
        version_id = str(source_ref.get("active_summary_version_id") or metadata.get("active_summary_version_id") or "")
        if not all((uid, session_id, conversation_id, version_id)) or not rows:
            return None
        if any(
            row.get("uid") != uid
            or row.get("session_id") != session_id
            or row.get("scope_kind") != "memory"
            or row.get("conversation_id") != conversation_id
            or row.get("active_summary_version_id") != version_id
            for row in rows
        ):
            return None
        self.set_rows(uid, session_id, rows)
        job_id = deterministic_job_id(uid, session_id, conversation_id, version_id)
        due = self.now + timedelta(seconds=self.debounce_seconds)
        if job_id in self.jobs:
            job = self.jobs[job_id]
            job["canonical_refs"] = canonical_refs(rows)
            job["transcript_hash"] = canonical_transcript_hash(rows)
            if job["status"] in RUNNABLE_STATUSES:
                job["not_before"] = max(job["not_before"], due)
            job["updated_at"] = self.now
            return dict(job)
        job = {
            "id": job_id,
            "uid": uid,
            "logical_session_id": session_id,
            "conversation_id": conversation_id,
            "starting_summary_version_id": version_id,
            "source_identity": str(completion.get("source_identity") or ""),
            "canonical_refs": canonical_refs(rows),
            "transcript_hash": canonical_transcript_hash(rows),
            "status": "pending",
            "outcome": None,
            "proposal_plan": None,
            "progress": {},
            "proposal_ids": [],
            "correction_ids": [],
            "receipt_refs": [],
            "not_before": due,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "max_attempts": self.max_attempts,
            "last_error_code": None,
            "last_error_detail": None,
            "completed_at": None,
            "created_at": self.now,
            "updated_at": self.now,
        }
        self.jobs[job_id] = job
        return dict(job)

    async def record_plan(self, job: dict[str, Any], plan: dict[str, Any]) -> bool:
        current = self.jobs[job["id"]]
        if current["lease_token"] != job["lease_token"] or current["status"] != "running":
            return False
        current["proposal_plan"] = json.loads(_stable_json(plan))
        current["updated_at"] = self.now
        return True

    async def record_progress(
        self,
        job: dict[str, Any],
        *,
        progress: dict[str, Any],
        proposal_ids: list[str],
        correction_ids: list[str],
        receipt_refs: list[dict[str, Any]],
    ) -> bool:
        current = self.jobs[job["id"]]
        if current["lease_token"] != job["lease_token"] or current["status"] != "running":
            return False
        current["progress"] = json.loads(_stable_json(progress))
        current["proposal_ids"] = list(proposal_ids)
        current["correction_ids"] = list(correction_ids)
        current["receipt_refs"] = list(receipt_refs)
        current["updated_at"] = self.now
        return True

    async def claim_due(self, worker_id: str, *, lease_seconds: int = 120) -> Optional[dict[str, Any]]:
        candidates = [
            job
            for job in self.jobs.values()
            if (job["status"] in RUNNABLE_STATUSES and job["not_before"] <= self.now)
            or (job["status"] == "running" and job["lease_expires_at"] and job["lease_expires_at"] < self.now)
        ]
        if not candidates:
            return None
        job = sorted(candidates, key=lambda item: (item["not_before"], item["created_at"]))[0]
        job["status"] = "running"
        job["lease_owner"] = worker_id
        job["lease_token"] = str(uuid.uuid4())
        job["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
        job["attempt_count"] += 1
        job["updated_at"] = self.now
        return dict(job)

    async def load_canonical_rows(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        foreign = [
            row
            for (uid, session_id), rows in self.rows.items()
            if session_id == job["logical_session_id"] and uid != job["uid"]
            for row in rows
        ]
        if foreign:
            raise ValueError("session_owner_collision")
        return list(self.rows.get((job["uid"], job["logical_session_id"]), []))

    async def finish(self, job: dict[str, Any], **values) -> bool:
        current = self.jobs[job["id"]]
        if current["lease_token"] != job["lease_token"] or current["status"] != "running":
            return False
        current.update(
            {
                "status": values["status"],
                "outcome": values["outcome"],
                "proposal_ids": list(values["proposal_ids"]),
                "correction_ids": list(values["correction_ids"]),
                "receipt_refs": list(values["receipt_refs"]),
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "completed_at": self.now,
                "updated_at": self.now,
            }
        )
        return True

    async def fail_or_retry(
        self,
        job: dict[str, Any],
        *,
        error_code: str,
        error_detail: str = "",
        retryable: bool,
        metrics: Optional[dict[str, Any]] = None,
    ) -> str:
        current = self.jobs[job["id"]]
        terminal = not retryable or current["attempt_count"] >= current["max_attempts"]
        current["status"] = "dead_letter" if terminal else "retry"
        current["last_error_code"] = error_code
        current["last_error_detail"] = error_detail
        current["lease_owner"] = None
        current["lease_token"] = None
        current["lease_expires_at"] = None
        if terminal:
            current["outcome"] = "failed"
            current["completed_at"] = self.now
        else:
            current["not_before"] = self.now + timedelta(seconds=5 * (2 ** (current["attempt_count"] - 1)))
        return current["status"]

    async def get_for_user(
        self,
        *,
        uid: str,
        conversation_id: str,
        job_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        matches = [
            job
            for job in self.jobs.values()
            if job["uid"] == uid and job["conversation_id"] == conversation_id and (not job_id or job["id"] == job_id)
        ]
        return dict(sorted(matches, key=lambda item: item["created_at"], reverse=True)[0]) if matches else None
