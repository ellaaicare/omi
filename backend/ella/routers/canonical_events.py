"""
Canonical event ledger for unified Ella memory.

Source adapters write lossless turn-level events here first, then downstream
processors can fan out to OMI conversation objects, OpenClaw workspace files,
scanner/guardian routes, or future agent runtimes. This module intentionally
does not require OpenClaw runtime access.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from utils.ella.canonical_context import (
    MAX_CANONICAL_EVENT_SEQUENCE,
    MAX_CANONICAL_TURN_ORDINAL,
    parse_canonical_ordering_integer,
)
from utils.ella.time_context import annotate_event_time, build_time_context

logger = logging.getLogger("ella.canonical_events")

DEFAULT_TIMELINE_LIMIT = 100
MAX_TIMELINE_LIMIT = 500
_TURN_ORDINAL_SQL_IS_VALID = """(
    metadata->>'turn_ordinal' ~ '^(0|[1-9][0-9]{0,18})$'
    AND (
        length(metadata->>'turn_ordinal') < 19
        OR metadata->>'turn_ordinal' <= '9223372036854775807'
    )
)"""
_EVENT_SEQUENCE_SQL_IS_VALID = """(
    metadata->>'event_sequence' ~ '^(0|[1-9][0-9]{0,9})$'
    AND (
        length(metadata->>'event_sequence') < 10
        OR metadata->>'event_sequence' <= '2147483647'
    )
)"""
_TURN_IDENTITY_SQL = """COALESCE(
    NULLIF(metadata->>'turn_id', ''),
    NULLIF(source_ref->>'client_message_id', ''),
    NULLIF(source_ref->>'turn_id', ''),
    regexp_replace(event_id, ':(user|assistant)$', '')
)"""

_pool: Optional[asyncpg.Pool] = None


def _turn_identity(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source_ref = item.get("source_ref") if isinstance(item.get("source_ref"), dict) else {}
    explicit = metadata.get("turn_id") or source_ref.get("client_message_id") or source_ref.get("turn_id")
    if explicit:
        return str(explicit)
    event_id = str(item.get("event_id") or "")
    for suffix in (":user", ":assistant"):
        if event_id.endswith(suffix):
            return event_id[: -len(suffix)]
    return event_id


def _event_sequence(item: dict[str, Any]) -> int:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    sequence = metadata.get("event_sequence")
    parsed = parse_canonical_ordering_integer(sequence, maximum=MAX_CANONICAL_EVENT_SEQUENCE)
    if parsed is not None:
        return parsed
    if item.get("role") == "user":
        return 0
    if item.get("role") == "assistant":
        return 1
    return 2


def _turn_ordinal(item: dict[str, Any]) -> Optional[int]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    ordinal = metadata.get("turn_ordinal")
    return parse_canonical_ordering_integer(ordinal, maximum=MAX_CANONICAL_TURN_ORDINAL)


def _canonical_event_order(item: dict[str, Any]) -> tuple[datetime, str, int, int, str, int, str]:
    turn_ordinal = _turn_ordinal(item)
    return (
        item["started_at"],
        str(item.get("session_id") or ""),
        0 if turn_ordinal is not None else 1,
        turn_ordinal or 0,
        _turn_identity(item),
        _event_sequence(item),
        str(item.get("event_id") or ""),
    )


async def _get_pool() -> asyncpg.Pool:
    """Get or create the Postgres pool used by the OMI backend patches."""
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: datetime | str | None, field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {field_name}: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _stable_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_ordering_integer(raw: Any, *, field_name: str, maximum: int) -> int:
    value = parse_canonical_ordering_integer(raw, maximum=maximum)
    if value is None:
        raise HTTPException(status_code=422, detail=f"invalid_{field_name}")
    return value


def _normalize_ordering_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata)
    if "turn_ordinal" in normalized:
        normalized["turn_ordinal"] = _normalize_ordering_integer(
            normalized["turn_ordinal"], field_name="turn_ordinal", maximum=MAX_CANONICAL_TURN_ORDINAL
        )
    if "event_sequence" in normalized:
        normalized["event_sequence"] = _normalize_ordering_integer(
            normalized["event_sequence"], field_name="event_sequence", maximum=MAX_CANONICAL_EVENT_SEQUENCE
        )
    return normalized


def _should_replace_existing_event(item: dict[str, Any]) -> bool:
    """Allow derived OMI summary rows to refresh while preserving raw source data.

    Normal turn events remain immutable. OMI summary events are app-facing
    derived summaries; Observer corrections/enrichment should update the single
    canonical summary row instead of creating duplicate timeline entries.
    """
    metadata = item.get("metadata") or {}
    return item.get("channel") == "omi" and metadata.get("adapter") == "omi-enriched-conversation"


def _derive_source_identity(
    *,
    uid: str,
    channel: str,
    provider: str,
    session_id: Optional[str],
    source_ref: dict[str, Any],
) -> str:
    """Build the source side of the idempotency key.

    Adapters can pass `source_ref.source_identity` when they already have a
    durable source namespace. Otherwise we derive a stable namespace from common
    source identifiers and finally fall back to uid/channel/provider/session.
    """
    explicit = source_ref.get("source_identity")
    if isinstance(explicit, str) and explicit:
        return explicit

    for key in (
        "conversation_id",
        "message_id",
        "row_id",
        "call_id",
        "thread_id",
        "source_id",
    ):
        value = source_ref.get(key)
        if value is not None and value != "":
            return f"{provider}:{channel}:{key}:{value}"

    session_part = session_id or "no-session"
    return f"{provider}:{channel}:uid:{uid}:session:{session_part}"


class CanonicalEventIn(BaseModel):
    uid: str
    canonical_identity: str
    event_id: str
    session_id: Optional[str] = None
    channel: str
    provider: str
    role: str
    text: str = ""
    started_at: datetime | str
    ended_at: Optional[datetime | str] = None
    privacy_scope: str = "user_private"
    scan_policy: str = "none"
    source_ref: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized(self) -> dict[str, Any]:
        started_at = _parse_datetime(self.started_at, "started_at")
        ended_at = _parse_datetime(self.ended_at, "ended_at")
        assert started_at is not None
        raw_event = _model_dump(self)
        metadata = _normalize_ordering_metadata(self.metadata)
        raw_event["metadata"] = metadata
        source_identity = _derive_source_identity(
            uid=self.uid,
            channel=self.channel,
            provider=self.provider,
            session_id=self.session_id,
            source_ref=self.source_ref,
        )
        return {
            **raw_event,
            "started_at": started_at,
            "ended_at": ended_at,
            "metadata": metadata,
            "source_identity": source_identity,
            "raw_event": raw_event,
        }


class CanonicalEventsBatch(BaseModel):
    events: list[CanonicalEventIn] = Field(default_factory=list)


class SessionCompleteIn(BaseModel):
    uid: Optional[str] = None
    canonical_identity: Optional[str] = None
    channel: Optional[str] = None
    provider: Optional[str] = None
    started_at: Optional[datetime | str] = None
    ended_at: Optional[datetime | str] = None
    source_ref: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized(self, session_id: str) -> dict[str, Any]:
        metadata = _normalize_ordering_metadata(self.metadata)
        source_identity = _derive_source_identity(
            uid=self.uid or "",
            channel=self.channel or "unknown",
            provider=self.provider or "unknown",
            session_id=session_id,
            source_ref=self.source_ref,
        )
        raw_completion = _model_dump(self)
        raw_completion["metadata"] = metadata
        return {
            "session_id": session_id,
            "uid": self.uid,
            "canonical_identity": self.canonical_identity,
            "channel": self.channel,
            "provider": self.provider,
            "started_at": _parse_datetime(self.started_at, "started_at"),
            "completed_at": _parse_datetime(self.ended_at, "ended_at") or _utc_now(),
            "source_ref": self.source_ref,
            "metadata": metadata,
            "source_identity": source_identity,
            "raw_completion": raw_completion,
        }


class CanonicalEventStore:
    async def write_batch(self, events: list[CanonicalEventIn]) -> dict[str, Any]:
        raise NotImplementedError

    async def complete_session(self, session_id: str, completion: SessionCompleteIn) -> dict[str, Any]:
        raise NotImplementedError

    async def timeline(
        self,
        *,
        uid: str,
        since: Optional[datetime],
        limit: int,
        channels: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def events_by_event_ids(
        self, *, uid: str, source_identity: str, event_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Read back exact owner-bound event ids after an idempotent write."""
        raise NotImplementedError


class PostgresCanonicalEventStore(CanonicalEventStore):
    async def write_batch(self, events: list[CanonicalEventIn]) -> dict[str, Any]:
        if not events:
            return {"ok": True, "inserted": 0, "duplicates": 0, "events": []}

        normalized_events = [event.normalized() for event in events]
        pool = await _get_pool()
        statuses: list[dict[str, Any]] = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                for item in normalized_events:
                    conflict_clause = (
                        """
                        DO UPDATE SET
                            uid = EXCLUDED.uid,
                            canonical_identity = EXCLUDED.canonical_identity,
                            session_id = EXCLUDED.session_id,
                            channel = EXCLUDED.channel,
                            provider = EXCLUDED.provider,
                            role = EXCLUDED.role,
                            text = EXCLUDED.text,
                            started_at = EXCLUDED.started_at,
                            ended_at = EXCLUDED.ended_at,
                            privacy_scope = EXCLUDED.privacy_scope,
                            scan_policy = EXCLUDED.scan_policy,
                            source_ref = EXCLUDED.source_ref,
                            metadata = EXCLUDED.metadata,
                            raw_event = EXCLUDED.raw_event
                        """
                        if _should_replace_existing_event(item)
                        else "DO NOTHING"
                    )
                    row = await conn.fetchrow(
                        f"""
                        INSERT INTO canonical_events (
                            uid, canonical_identity, event_id, source_identity,
                            session_id, channel, provider, role, text,
                            started_at, ended_at, privacy_scope, scan_policy,
                            source_ref, metadata, raw_event
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13, $14::jsonb, $15::jsonb, $16::jsonb
                        )
                        ON CONFLICT (event_id, source_identity) {conflict_clause}
                        RETURNING id, (xmax = 0) AS inserted
                        """,
                        item["uid"],
                        item["canonical_identity"],
                        item["event_id"],
                        item["source_identity"],
                        item.get("session_id"),
                        item["channel"],
                        item["provider"],
                        item["role"],
                        item["text"],
                        item["started_at"],
                        item["ended_at"],
                        item["privacy_scope"],
                        item["scan_policy"],
                        _stable_json(item["source_ref"]),
                        _stable_json(item["metadata"]),
                        _stable_json(item["raw_event"]),
                    )
                    statuses.append(
                        {
                            "event_id": item["event_id"],
                            "source_identity": item["source_identity"],
                            "inserted": bool(row and row["inserted"]),
                            "updated": bool(row and not row["inserted"]),
                        }
                    )

        inserted_count = sum(1 for status in statuses if status["inserted"])
        updated_count = sum(1 for status in statuses if status.get("updated"))
        return {
            "ok": True,
            "inserted": inserted_count,
            "updated": updated_count,
            "duplicates": len(statuses) - inserted_count - updated_count,
            "events": statuses,
        }

    async def complete_session(self, session_id: str, completion: SessionCompleteIn) -> dict[str, Any]:
        item = completion.normalized(session_id)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO canonical_event_sessions (
                    session_id, source_identity, uid, canonical_identity,
                    channel, provider, started_at, completed_at,
                    source_ref, metadata, raw_completion
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb)
                ON CONFLICT (session_id, source_identity) DO NOTHING
                RETURNING id
                """,
                item["session_id"],
                item["source_identity"],
                item["uid"],
                item["canonical_identity"],
                item["channel"],
                item["provider"],
                item["started_at"],
                item["completed_at"],
                _stable_json(item["source_ref"]),
                _stable_json(item["metadata"]),
                _stable_json(item["raw_completion"]),
            )

        return {
            "ok": True,
            "session_id": session_id,
            "source_identity": item["source_identity"],
            "completed_at": item["completed_at"].isoformat(),
            "inserted": inserted is not None,
            "duplicate": inserted is None,
        }

    async def timeline(
        self,
        *,
        uid: str,
        since: Optional[datetime],
        limit: int,
        channels: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        params: list[Any] = [uid]
        filters = ["uid = $1"]
        if since:
            params.append(since)
            filters.append(f"started_at >= ${len(params)}")
        if channels:
            params.append(channels)
            filters.append(f"channel = ANY(${len(params)}::text[])")
        params.append(limit)
        limit_placeholder = f"${len(params)}"
        where_clause = " AND ".join(filters)

        pool = await _get_pool()
        rows = await pool.fetch(
            f"""
            SELECT *
            FROM (
                SELECT uid, canonical_identity, event_id, source_identity,
                       session_id, channel, provider, role, text,
                       started_at, ended_at, privacy_scope, scan_policy,
                       source_ref, metadata, raw_event, inserted_at
                FROM canonical_events
                WHERE {where_clause}
                ORDER BY
                    started_at DESC,
                    COALESCE(session_id, '') DESC,
                    CASE WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN 0 ELSE 1 END DESC,
                    CASE
                        WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN (metadata->>'turn_ordinal')::bigint
                        ELSE 0
                    END DESC,
                    {_TURN_IDENTITY_SQL} DESC,
                    CASE
                        WHEN {_EVENT_SEQUENCE_SQL_IS_VALID} THEN (metadata->>'event_sequence')::integer
                        WHEN role = 'assistant' THEN 1 WHEN role = 'user' THEN 0 ELSE 2
                    END DESC,
                    event_id DESC
                LIMIT {limit_placeholder}
            ) recent_events
            ORDER BY
                started_at ASC,
                COALESCE(session_id, '') ASC,
                CASE WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN 0 ELSE 1 END ASC,
                CASE
                    WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN (metadata->>'turn_ordinal')::bigint
                    ELSE 0
                END ASC,
                {_TURN_IDENTITY_SQL} ASC,
                CASE
                    WHEN {_EVENT_SEQUENCE_SQL_IS_VALID} THEN (metadata->>'event_sequence')::integer
                    WHEN role = 'user' THEN 0 WHEN role = 'assistant' THEN 1 ELSE 2
                END ASC,
                event_id ASC
            """,
            *params,
        )
        return [_row_to_event(row) for row in rows]

    async def events_by_event_ids(
        self, *, uid: str, source_identity: str, event_ids: list[str]
    ) -> list[dict[str, Any]]:
        pool = await _get_pool()
        rows = await pool.fetch(
            f"""
            SELECT uid, canonical_identity, event_id, source_identity,
                   session_id, channel, provider, role, text,
                   started_at, ended_at, privacy_scope, scan_policy,
                   source_ref, metadata, raw_event, inserted_at
            FROM canonical_events
            WHERE event_id = ANY($1::text[])
              AND source_identity = $2
              AND uid = $3
            ORDER BY
                started_at ASC,
                COALESCE(session_id, '') ASC,
                CASE WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN 0 ELSE 1 END ASC,
                CASE
                    WHEN {_TURN_ORDINAL_SQL_IS_VALID} THEN (metadata->>'turn_ordinal')::bigint
                    ELSE 0
                END ASC,
                {_TURN_IDENTITY_SQL} ASC,
                CASE
                    WHEN {_EVENT_SEQUENCE_SQL_IS_VALID} THEN (metadata->>'event_sequence')::integer
                    WHEN role = 'user' THEN 0 WHEN role = 'assistant' THEN 1 ELSE 2
                END ASC,
                event_id ASC
            """,
            event_ids,
            source_identity,
            uid,
        )
        return [_row_to_event(row) for row in rows]


class InMemoryCanonicalEventStore(CanonicalEventStore):
    def __init__(self):
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}

    async def write_batch(self, events: list[CanonicalEventIn]) -> dict[str, Any]:
        normalized_events = [event.normalized() for event in events]
        statuses = []
        for item in normalized_events:
            item["inserted_at"] = _utc_now()
            key = (item["event_id"], item["source_identity"])
            inserted = key not in self._events
            if inserted:
                self._events[key] = item
            statuses.append(
                {
                    "event_id": item["event_id"],
                    "source_identity": item["source_identity"],
                    "inserted": inserted,
                }
            )
        inserted_count = sum(1 for status in statuses if status["inserted"])
        return {
            "ok": True,
            "inserted": inserted_count,
            "duplicates": len(statuses) - inserted_count,
            "events": statuses,
        }

    async def complete_session(self, session_id: str, completion: SessionCompleteIn) -> dict[str, Any]:
        item = completion.normalized(session_id)
        key = (item["session_id"], item["source_identity"])
        inserted = key not in self._sessions
        if inserted:
            self._sessions[key] = item
        return {
            "ok": True,
            "session_id": session_id,
            "source_identity": item["source_identity"],
            "completed_at": item["completed_at"].isoformat(),
            "inserted": inserted,
            "duplicate": not inserted,
        }

    async def timeline(
        self,
        *,
        uid: str,
        since: Optional[datetime],
        limit: int,
        channels: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        channel_set = set(channels or [])
        events = []
        for event in self._events.values():
            if event["uid"] != uid:
                continue
            if since and event["started_at"] < since:
                continue
            if channel_set and event["channel"] not in channel_set:
                continue
            events.append(event)

        events.sort(key=_canonical_event_order)
        return [_row_to_event(event) for event in events[-limit:]]

    async def events_by_event_ids(
        self, *, uid: str, source_identity: str, event_ids: list[str]
    ) -> list[dict[str, Any]]:
        expected_ids = set(event_ids)
        events = [
            event
            for event in self._events.values()
            if event["event_id"] in expected_ids and event["uid"] == uid and event["source_identity"] == source_identity
        ]
        events.sort(key=_canonical_event_order)
        return [_row_to_event(event) for event in events]


def _row_to_event(row: Any) -> dict[str, Any]:
    def value(name: str) -> Any:
        if isinstance(row, dict):
            return row.get(name)
        return row[name]

    def json_value(name: str) -> dict[str, Any]:
        raw = value(name)
        if raw is None:
            return {}
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw)

    def iso(name: str) -> Optional[str]:
        raw = value(name)
        if not raw:
            return None
        if isinstance(raw, datetime):
            return raw.astimezone(timezone.utc).isoformat()
        return str(raw)

    return annotate_event_time(
        {
            "uid": value("uid"),
            "canonical_identity": value("canonical_identity"),
            "event_id": value("event_id"),
            "source_identity": value("source_identity"),
            "session_id": value("session_id"),
            "channel": value("channel"),
            "provider": value("provider"),
            "role": value("role"),
            "text": value("text"),
            "started_at": iso("started_at"),
            "ended_at": iso("ended_at"),
            "privacy_scope": value("privacy_scope"),
            "scan_policy": value("scan_policy"),
            "source_ref": json_value("source_ref"),
            "metadata": json_value("metadata"),
            "raw_event": json_value("raw_event"),
        }
    )


def _parse_channels(channels: Optional[str]) -> Optional[list[str]]:
    if not channels:
        return None
    parsed = [part.strip() for part in channels.split(",") if part.strip()]
    return parsed or None


def _sanitize_limit(limit: int) -> int:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    return min(limit, MAX_TIMELINE_LIMIT)


def create_canonical_events_router(store: Optional[CanonicalEventStore] = None) -> APIRouter:
    """Create the backend router for canonical event ingestion and timeline reads."""
    router = APIRouter()
    default_store = store or PostgresCanonicalEventStore()

    @router.post("/v1/ella/events")
    async def write_events(batch: CanonicalEventsBatch):
        """
        Idempotently write raw canonical events.

        Source adapters should submit one event per user/assistant/source turn.
        Duplicates are detected by `(event_id, source_identity)` and ignored;
        existing raw text/metadata is never overwritten by summaries,
        corrections, retries, or downstream enrichment.
        """
        return await default_store.write_batch(batch.events)

    @router.post("/v1/ella/sessions/{session_id}/complete")
    async def complete_session(session_id: str, completion: SessionCompleteIn):
        """Record source session completion without converting sessions into OMI objects."""
        return await default_store.complete_session(session_id, completion)

    @router.get("/v1/ella/timeline")
    async def read_timeline(
        uid: str,
        since: Optional[str] = None,
        limit: int = Query(default=DEFAULT_TIMELINE_LIMIT),
        channels: Optional[str] = None,
        timezone: Optional[str] = Query(default=None, alias="timezone"),
    ):
        """
        Read a single chronological timeline across channels for one uid.

        `channels` is a comma-separated filter such as
        `ios_chat,ios_voice,imessage,omi_transcript`. OMI conversation objects
        remain separate durable session objects; this endpoint returns the raw
        canonical turn ledger used for memory hydration and reconciliation.
        """
        parsed_since = _parse_datetime(since, "since") if since else None
        events = await default_store.timeline(
            uid=uid,
            since=parsed_since,
            limit=_sanitize_limit(limit),
            channels=_parse_channels(channels),
        )
        if timezone:
            events = [annotate_event_time(event, tz_name=timezone) for event in events]
        return {"ok": True, "uid": uid, "time_context": build_time_context(timezone), "events": events}

    return router


async def handle_events(request: Request, store: Optional[CanonicalEventStore] = None) -> dict[str, Any]:
    """Direct import helper for backend integration tests or legacy mounting."""
    payload = CanonicalEventsBatch(**(await request.json()))
    return await (store or PostgresCanonicalEventStore()).write_batch(payload.events)
