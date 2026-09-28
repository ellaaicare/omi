"""Durable owner-bound Guardian playback ledger.

Replaces the in-memory `_playback_events` dict that used to live in
`ella.routers.guardian`. That dict reset on every process restart, was not
visible to the scanner process, and treated "recorded" as equivalent to
"played". This module is the single source of truth for the enqueue ->
next-audio -> iOS playback receipt lifecycle, keyed by the authenticated
owner uid plus the immutable playback/audio id.

Lifecycle: generated -> queued -> fetched -> started -> completed|failed.

Only an authenticated `started` or `completed` receipt is evidence that
audio actually played out loud on the owner's device. generated, queued and
fetched rows exist for latency/drop-off visibility only — callers MUST NOT
treat them as playback. `get_played_candidates` enforces this by only
returning rows with a recorded `started_at`.

Every read and write here is scoped by `uid`. A caller may only ever mutate
a ledger row for the uid that owns it; `record_fetched` and
`record_playback_receipt` raise `PlaybackLedgerOwnershipError` if the
`playback_id` was generated for a different uid, rather than silently
creating or updating a cross-owner row.

No playback or transcript text is ever written to logs, n8n customData, or
error receipts from this module — callers must not log `playback_text`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from database.ella_postgres import get_ella_postgres_pool

DEFAULT_RETENTION_DAYS = int(os.getenv("ELLA_GUARDIAN_PLAYBACK_LEDGER_RETENTION_DAYS", "7"))

# Monotonic lifecycle ordering. `completed` and `failed` are both terminal
# (rank 4): once a terminal receipt is recorded, later receipts of any kind
# for the same playback_id are idempotent no-ops so a duplicate or
# out-of-order network retry can never rewrite history.
_STATUS_RANK = {
    "generated": 0,
    "queued": 1,
    "fetched": 2,
    "started": 3,
    "completed": 4,
    "failed": 4,
}
_TERMINAL_STATUSES = {"completed", "failed"}
_RECEIPT_EVENT_TYPES = {"started", "completed", "failed"}

_LEDGER_COLUMNS = (
    "uid",
    "playback_id",
    "queue_item_id",
    "audio_id",
    "trace_id",
    "status",
    "purpose",
    "playback_text",
    "text_provenance",
    "route",
    "device_class",
    "port_name",
    "device_uid",
    "duration_ms",
    "error_message",
    "generated_at",
    "queued_at",
    "fetched_at",
    "started_at",
    "completed_at",
    "failed_at",
    "created_at",
    "updated_at",
)


class PlaybackLedgerOwnershipError(Exception):
    """Raised when a caller tries to mutate another uid's ledger row."""

    def __init__(self, uid: str, playback_id: str):
        super().__init__(f"playback_id is not owned by uid={uid!r}")
        self.uid = uid
        self.playback_id = playback_id


class PlaybackLedgerUnknownItemError(Exception):
    """Raised when a playback receipt references an item this uid never
    generated, queued, or fetched — a receipt is evidence about a real
    delivery, not a way to create one."""

    def __init__(self, uid: str, playback_id: str):
        super().__init__(f"playback_id {playback_id!r} was never generated for uid={uid!r}")
        self.uid = uid
        self.playback_id = playback_id


@dataclass(frozen=True)
class PlaybackCandidate:
    """A confirmed-played ledger entry eligible for echo-classifier review."""

    playback_id: str
    queue_item_id: Optional[str]
    trace_id: Optional[str]
    purpose: Optional[str]
    playback_text: Optional[str]
    route: Optional[str]
    device_class: Optional[str]
    duration_ms: Optional[int]
    # The evidence timestamp: the row's `started_at` if one was recorded,
    # else its `completed_at` — `get_played_candidates` only ever returns
    # rows that have at least one of the two, so this is never null here.
    started_at: datetime
    completed_at: Optional[datetime]

    def to_classifier_candidate(self) -> dict[str, Any]:
        """Owner-scoped, minimal shape handed to the semantic classifier prompt.

        Deliberately narrow: only what the classifier needs to match spoken
        text against a specific played Whisper. No queue/trace ids that
        aren't needed for matching, no device metadata.
        """
        return {
            "playback_id": self.playback_id,
            "text": self.playback_text or "",
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
        }


async def _fetch_owner_row(conn: asyncpg.Connection, uid: str, playback_id: str) -> Optional[asyncpg.Record]:
    # The durable key is the (uid, playback_id) pair (see the table's UNIQUE
    # constraint) — playback_id alone is not guaranteed unique across
    # owners, so querying by playback_id only could lock and return a
    # different owner's row, either leaking its status or rejecting this
    # owner as a false "not owned" instead of treating the item as new.
    return await conn.fetchrow(
        "SELECT uid, status FROM guardian_playback_ledger WHERE uid = $1 AND playback_id = $2 FOR UPDATE",
        uid,
        playback_id,
    )


def _assert_owned(row: Optional[asyncpg.Record], uid: str, playback_id: str) -> None:
    """Defensive invariant: `_fetch_owner_row` is scoped by `uid`, so a
    returned row's `uid` should always already match. Kept as a guard
    against a future query regression rather than the primary ownership
    check it used to be."""
    if row is not None and str(row["uid"]) != uid:
        raise PlaybackLedgerOwnershipError(uid, playback_id)


async def record_generated(
    pool: asyncpg.Pool,
    *,
    uid: str,
    playback_id: str,
    queue_item_id: Optional[str] = None,
    audio_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    purpose: Optional[str] = None,
    playback_text: Optional[str] = None,
    text_provenance: Optional[str] = None,
) -> None:
    """Record TTS generation. Not evidence of playback."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await _fetch_owner_row(conn, uid, playback_id)
            _assert_owned(existing, uid, playback_id)
            await conn.execute(
                """
                INSERT INTO guardian_playback_ledger (
                    uid, playback_id, queue_item_id, audio_id, trace_id,
                    status, purpose, playback_text, text_provenance,
                    generated_at
                )
                VALUES ($1, $2, $3, $4, $5, 'generated', $6, $7, $8, NOW())
                ON CONFLICT (uid, playback_id) DO UPDATE SET
                    queue_item_id = COALESCE(EXCLUDED.queue_item_id, guardian_playback_ledger.queue_item_id),
                    audio_id = COALESCE(EXCLUDED.audio_id, guardian_playback_ledger.audio_id),
                    trace_id = COALESCE(EXCLUDED.trace_id, guardian_playback_ledger.trace_id),
                    updated_at = NOW()
                """,
                uid,
                playback_id,
                queue_item_id,
                audio_id,
                trace_id,
                purpose,
                playback_text,
                text_provenance,
            )


async def record_queued(
    pool: asyncpg.Pool,
    *,
    uid: str,
    playback_id: str,
    queue_item_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> None:
    """Record that the audio was accepted onto the delivery queue."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await _fetch_owner_row(conn, uid, playback_id)
            _assert_owned(existing, uid, playback_id)
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO guardian_playback_ledger (
                        uid, playback_id, queue_item_id, trace_id, status, queued_at
                    )
                    VALUES ($1, $2, $3, $4, 'queued', NOW())
                    """,
                    uid,
                    playback_id,
                    queue_item_id,
                    trace_id,
                )
                return
            if _STATUS_RANK[str(existing["status"])] >= _STATUS_RANK["queued"]:
                return
            await conn.execute(
                """
                UPDATE guardian_playback_ledger
                SET status = 'queued', queued_at = NOW(), updated_at = NOW(),
                    queue_item_id = COALESCE($3, queue_item_id),
                    trace_id = COALESCE($4, trace_id)
                WHERE uid = $1 AND playback_id = $2
                """,
                uid,
                playback_id,
                queue_item_id,
                trace_id,
            )


async def record_fetched(
    pool: asyncpg.Pool,
    *,
    uid: str,
    playback_id: str,
    queue_item_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    route: Optional[str] = None,
    device_class: Optional[str] = None,
) -> None:
    """Record that iOS popped the item from /next-audio. Not evidence of playback."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await _fetch_owner_row(conn, uid, playback_id)
            _assert_owned(existing, uid, playback_id)
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO guardian_playback_ledger (
                        uid, playback_id, queue_item_id, trace_id, status,
                        route, device_class, fetched_at
                    )
                    VALUES ($1, $2, $3, $4, 'fetched', $5, $6, NOW())
                    """,
                    uid,
                    playback_id,
                    queue_item_id,
                    trace_id,
                    route,
                    device_class,
                )
                return
            if _STATUS_RANK[str(existing["status"])] >= _STATUS_RANK["fetched"]:
                return
            await conn.execute(
                """
                UPDATE guardian_playback_ledger
                SET status = 'fetched', fetched_at = NOW(), updated_at = NOW(),
                    queue_item_id = COALESCE($3, queue_item_id),
                    trace_id = COALESCE($4, trace_id),
                    route = COALESCE($5, route),
                    device_class = COALESCE($6, device_class)
                WHERE uid = $1 AND playback_id = $2
                """,
                uid,
                playback_id,
                queue_item_id,
                trace_id,
                route,
                device_class,
            )


async def record_playback_receipt(
    pool: asyncpg.Pool,
    *,
    uid: str,
    playback_id: str,
    event_type: str,
    queue_item_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    route: Optional[str] = None,
    device_class: Optional[str] = None,
    port_name: Optional[str] = None,
    device_uid: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> str:
    """Transactional, idempotent upsert of an authenticated playback receipt.

    `event_type` must be one of started/completed/failed. Returns the
    ledger row's resulting status after applying monotonic ordering: a
    duplicate or out-of-order receipt (e.g. a retried "started" arriving
    after "completed" was already recorded) is a no-op that returns the
    existing, already-terminal status rather than rewriting it.

    Raises `PlaybackLedgerUnknownItemError` if this uid never generated,
    queued, or fetched `playback_id` — a receipt is evidence about a real
    delivery the system already knows about, never a way to fabricate one
    from scratch.
    """
    event_type = str(event_type or "").strip().lower()
    if event_type not in _RECEIPT_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {_RECEIPT_EVENT_TYPES}, got {event_type!r}")

    column = {"started": "started_at", "completed": "completed_at", "failed": "failed_at"}[event_type]

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await _fetch_owner_row(conn, uid, playback_id)
            _assert_owned(existing, uid, playback_id)

            if existing is None:
                raise PlaybackLedgerUnknownItemError(uid, playback_id)

            current_status = str(existing["status"])
            if current_status in _TERMINAL_STATUSES:
                # Idempotent: a terminal receipt was already recorded. Duplicate
                # and out-of-order retries are no-ops so replay can never
                # flip a completed receipt to failed or vice versa.
                return current_status

            if _STATUS_RANK[event_type] < _STATUS_RANK[current_status]:
                return current_status

            await conn.execute(
                f"""
                UPDATE guardian_playback_ledger
                SET status = $3, {column} = NOW(), updated_at = NOW(),
                    queue_item_id = COALESCE($4, queue_item_id),
                    trace_id = COALESCE($5, trace_id),
                    route = COALESCE($6, route),
                    device_class = COALESCE($7, device_class),
                    port_name = COALESCE($8, port_name),
                    device_uid = COALESCE($9, device_uid),
                    duration_ms = COALESCE($10, duration_ms),
                    error_message = CASE WHEN $3 = 'failed' THEN $11 ELSE error_message END
                WHERE uid = $1 AND playback_id = $2
                """,
                uid,
                playback_id,
                event_type,
                queue_item_id,
                trace_id,
                route,
                device_class,
                port_name,
                device_uid,
                duration_ms,
                error_message,
            )
            return event_type


async def get_played_candidates(
    pool: asyncpg.Pool,
    uid: str,
    *,
    window_seconds: int = 45,
    limit: int = 5,
) -> list[PlaybackCandidate]:
    """Return this owner's recently-PLAYED ledger entries only.

    Rows must have a recorded `started_at` OR `completed_at` (either is an
    authenticated receipt) within `window_seconds` — a `completed` receipt
    that arrived without a preceding `started` receipt (e.g. the started
    beacon was lost in transit) is just as much evidence of playback as a
    `started` one. generated/queued/fetched-only rows are never returned —
    they are not evidence of playback.
    """
    rows = await pool.fetch(
        """
        SELECT playback_id, queue_item_id, trace_id, purpose, playback_text,
               route, device_class, duration_ms, started_at, completed_at,
               COALESCE(started_at, completed_at) AS played_at
        FROM guardian_playback_ledger
        WHERE uid = $1
          AND COALESCE(started_at, completed_at) IS NOT NULL
          AND COALESCE(started_at, completed_at) > NOW() - ($2 || ' seconds')::interval
        ORDER BY played_at DESC
        LIMIT $3
        """,
        uid,
        str(int(window_seconds)),
        limit,
    )
    return [
        PlaybackCandidate(
            playback_id=str(row["playback_id"]),
            queue_item_id=row["queue_item_id"],
            trace_id=row["trace_id"],
            purpose=row["purpose"],
            playback_text=row["playback_text"],
            route=row["route"],
            device_class=row["device_class"],
            duration_ms=row["duration_ms"],
            started_at=row["played_at"],
            completed_at=row["completed_at"],
        )
        for row in rows
    ]


async def cleanup_expired(
    pool: asyncpg.Pool,
    *,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Delete ledger rows past the retention window. Returns rows deleted."""
    retention_days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    result = await pool.execute(
        "DELETE FROM guardian_playback_ledger WHERE created_at < $1",
        cutoff,
    )
    # asyncpg execute() returns a tag string like "DELETE 3".
    try:
        return int(result.split(" ")[-1])
    except (ValueError, IndexError):
        return 0


async def get_pool() -> asyncpg.Pool:
    """Convenience re-export so callers don't need a separate import."""
    return await get_ella_postgres_pool()
