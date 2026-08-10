"""PostgreSQL persistence and canonical-ledger evidence for Today cards."""

from __future__ import annotations

import json
import math
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import asyncpg

from database.ella_postgres import get_ella_postgres_pool

from ella.services.today_card import (
    TODAY_CARD_CONTRACT_VERSION,
    TODAY_CARD_MIN_CAPTURE_SECONDS,
    TODAY_CARD_MIN_TRANSCRIPT_NON_ASCII_ALPHANUMERIC,
    TODAY_CARD_MIN_TRANSCRIPT_WORDS,
    TODAY_CARD_RENDER_CONTRACT_VERSION,
    TODAY_CARD_SOURCE_COOLDOWN_DAYS,
    TodayCardAvoidance,
    TodayCardEvidence,
    TodayCardFeedbackAction,
    TodayCardKind,
    TodayCardMaterializationClaim,
    TodayCardRecord,
    TodayCardSourceRef,
    TodayCardState,
    TodayCardUserContext,
    deterministic_card_id,
    evidence_is_safe,
    materialization_window,
    source_text_is_meaningful,
    sha256_ref,
    today_card_source_pack,
)

logger = logging.getLogger("ella.today_card")
TODAY_CARD_GROUNDING_CONTRACT_VERSION = "ella.today_card.grounding.v1"

_SOURCE_VALIDITY_LOCK_PREFIX = "ella.today-card-source-validity"

_SENSITIVE_MARKERS = (
    "abuse",
    "argument",
    "critical",
    "distress",
    "emergency",
    "fall",
    "financial",
    "grief",
    "guardian",
    "health",
    "medical",
    "medication",
    "safety",
    "self_harm",
    "suicide",
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _record_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _strict_bool(mapping: dict[str, Any], key: str, default: bool) -> bool:
    if key not in mapping:
        return default
    return mapping[key] is True


def _first_nonnegative_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            return parsed
    return None


def _card_from_row(row: Any) -> TodayCardRecord:
    content = _record_value(row, "content")
    return TodayCardRecord(
        card_id=str(_record_value(row, "card_id")),
        uid=str(_record_value(row, "uid")),
        local_date=_record_value(row, "local_date"),
        timezone=str(_record_value(row, "timezone")),
        contract_version=str(_record_value(row, "contract_version")),
        version=int(_record_value(row, "version")),
        state=TodayCardState(str(_record_value(row, "state"))),
        kind=(TodayCardKind(str(_record_value(row, "kind"))) if _record_value(row, "kind") else None),
        content=_json_object(content) or None,
        source_refs=_json_list(_record_value(row, "source_refs")),
        evidence_hash=_record_value(row, "evidence_hash"),
        source_watermark=_record_value(row, "source_watermark"),
        render_contract_version=str(_record_value(row, "render_contract_version")),
        generated_at=_record_value(row, "generated_at"),
        updated_at=_record_value(row, "updated_at"),
        invalidated_at=_record_value(row, "invalidated_at"),
        invalidation_reason=_record_value(row, "invalidation_reason"),
        presentation=_json_object(_record_value(row, "presentation")),
        interaction_state=_json_object(_record_value(row, "interaction_state")),
        private_consolidation=_json_object(_record_value(row, "private_consolidation")),
        reason_code=_record_value(row, "reason_code"),
    )


def _source_tags(metadata: dict[str, Any], scan_policy: str) -> list[str]:
    today = metadata.get("today_card") if isinstance(metadata.get("today_card"), dict) else {}
    raw_tags = _string_list(metadata.get("ella_tags")) + _string_list(today.get("tags"))
    assessment = metadata.get("internal_assessment")
    if isinstance(assessment, dict):
        for key in ("emergency", "safety", "distress", "grief", "health", "financial", "argument"):
            if assessment.get(key) is True:
                raw_tags.append(key)
        sensitivity = assessment.get("sensitivity")
        if isinstance(sensitivity, str) and sensitivity:
            raw_tags.append(sensitivity)
        risk_level = str(assessment.get("risk_level") or "").strip().lower()
        if risk_level not in {"", "none", "normal", "low"}:
            raw_tags.append("safety")
        escalation = str(assessment.get("escalation_recommendation") or "").strip().lower()
        if escalation not in {"", "none", "no_action", "monitor"}:
            raw_tags.append("safety")
        raw_tags.extend(_string_list(assessment.get("reason_codes")))
    signal = metadata.get("ella_signal")
    if isinstance(signal, dict) and signal.get("guardian_relevant") is True:
        raw_tags.append("safety")
    if any(marker in str(tag).strip().lower() for tag in raw_tags for marker in _SENSITIVE_MARKERS):
        raw_tags.append("safety")
    if scan_policy and scan_policy != "none":
        raw_tags.append("safety")
    return sorted({str(tag).strip().lower() for tag in raw_tags if str(tag).strip()})


def _has_grounded_content_provenance(today: dict[str, Any], source_version_id: str | None) -> bool:
    grounding = today.get("grounding")
    if not isinstance(grounding, dict) or not source_version_id:
        return False
    transcript_word_count = _first_nonnegative_number(grounding.get("transcript_word_count"))
    transcript_non_ascii_count = _first_nonnegative_number(grounding.get("transcript_non_ascii_alphanumeric_count"))
    capture_duration_seconds = _first_nonnegative_number(grounding.get("capture_duration_seconds"))
    transcript_hash = str(grounding.get("transcript_hash") or "")
    transcript_hash_digest = transcript_hash.removeprefix("sha256:")
    has_grounded_transcript = bool(
        (transcript_word_count is not None and transcript_word_count >= TODAY_CARD_MIN_TRANSCRIPT_WORDS)
        or (
            transcript_non_ascii_count is not None
            and transcript_non_ascii_count >= TODAY_CARD_MIN_TRANSCRIPT_NON_ASCII_ALPHANUMERIC
        )
    )
    return (
        grounding.get("contract_version") == TODAY_CARD_GROUNDING_CONTRACT_VERSION
        and grounding.get("grounded_content") is True
        and grounding.get("source_version_id") == source_version_id
        and capture_duration_seconds is not None
        and capture_duration_seconds >= TODAY_CARD_MIN_CAPTURE_SECONDS
        and has_grounded_transcript
        and transcript_hash.startswith("sha256:")
        and len(transcript_hash_digest) == 64
        and all(character in "0123456789abcdef" for character in transcript_hash_digest)
    )


async def lock_today_card_source_validity(conn: Any, uid: str) -> None:
    """Serialize canonical evidence writes, invalidation, and card publication per owner."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"{_SOURCE_VALIDITY_LOCK_PREFIX}:{uid}",
    )


def _evidence_from_row(
    row: Any,
    *,
    previous_day_start: datetime,
    previous_day_end: datetime,
) -> TodayCardEvidence | None:
    metadata = _json_object(_record_value(row, "metadata"))
    source_ref = _json_object(_record_value(row, "source_ref"))
    today = metadata.get("today_card") if isinstance(metadata.get("today_card"), dict) else {}
    structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
    source_quality = metadata.get("source_quality") if isinstance(metadata.get("source_quality"), dict) else {}
    adapter = str(metadata.get("adapter") or "")
    occurred_at = _record_value(row, "started_at")
    if not isinstance(occurred_at, datetime):
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    occurred_at = occurred_at.astimezone(timezone.utc)

    explicit_kind = str(today.get("kind") or "").strip().lower()
    if explicit_kind in {item.value for item in TodayCardKind if item != TodayCardKind.welcome}:
        kind = TodayCardKind(explicit_kind)
    elif adapter == "omi-enriched-conversation":
        kind = TodayCardKind.recap if previous_day_start <= occurred_at < previous_day_end else TodayCardKind.memory
    else:
        return None

    title = str(today.get("title") or structured.get("title") or metadata.get("title") or "").strip()
    summary = str(today.get("summary") or structured.get("overview") or _record_value(row, "text") or "").strip()
    summary_lines = [line.strip() for line in summary.splitlines() if line.strip()]
    summary = " ".join(
        (
            line
            if index == len(summary_lines) - 1 or line.endswith((".", "!", "?", ";", ":", ",", "—", "–", "-"))
            else f"{line}."
        )
        for index, line in enumerate(summary_lines)
    )
    source_id = str(
        source_ref.get("conversation_id") or today.get("source_id") or _record_value(row, "event_id") or ""
    ).strip()
    if not source_id:
        return None
    source_version_id = (
        str(source_ref.get("active_summary_version_id") or today.get("source_version_id") or "").strip() or None
    )
    conversation_id = str(source_ref.get("conversation_id") or "").strip() or None
    source_type = "conversation_summary" if conversation_id else str(today.get("source_type") or "canonical_event")
    confidence = today.get("confidence")
    transcript_word_count_value = _first_nonnegative_number(
        today.get("transcript_word_count"),
        source_quality.get("transcript_word_count"),
        structured.get("transcript_word_count"),
        metadata.get("transcript_word_count"),
    )
    transcript_word_count = int(transcript_word_count_value) if transcript_word_count_value is not None else None
    capture_duration_seconds = _first_nonnegative_number(
        today.get("capture_duration_seconds"),
        source_quality.get("capture_duration_seconds"),
        source_quality.get("duration_seconds"),
        structured.get("duration_seconds"),
        metadata.get("duration_seconds"),
    )
    source_text_meaningful = source_text_is_meaningful(title, summary)
    if adapter == "omi-enriched-conversation":
        source_text_meaningful = source_text_meaningful and _has_grounded_content_provenance(
            today,
            source_version_id,
        )
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = (
            0.82 if adapter == "omi-enriched-conversation" and source_version_id and source_text_meaningful else 0.0
        )
    privacy_scope = str(_record_value(row, "privacy_scope") or "user_private")
    scan_policy = str(_record_value(row, "scan_policy") or "none")
    meaningful = source_text_meaningful and _strict_bool(today, "meaningful", source_text_meaningful)
    confirmed = _strict_bool(today, "confirmed", False)
    positive_or_neutral = _strict_bool(today, "positive_or_neutral", True)
    superseded = _strict_bool(today, "superseded", False)
    deleted = _strict_bool(today, "deleted", False)
    tags = _source_tags(metadata, scan_policy)
    person_keys = [value[:128] for value in _string_list(today.get("person_keys"))]
    topic_keys = [value[:128] for value in _string_list(today.get("topic_keys"))]
    source = TodayCardSourceRef(
        source_type=source_type,
        source_id=source_id,
        source_version_id=source_version_id,
        occurred_at=occurred_at,
        evidence_hash=sha256_ref(
            {
                "event_id": _record_value(row, "event_id"),
                "source_id": source_id,
                "source_version_id": source_version_id,
                "source_type": source_type,
                "conversation_id": conversation_id,
                "occurred_at": occurred_at,
                "title": title,
                "summary": summary,
                "privacy_scope": privacy_scope,
                "scan_policy": scan_policy,
                "confidence": float(confidence),
                "meaningful": meaningful,
                "confirmed": confirmed,
                "positive_or_neutral": positive_or_neutral,
                "superseded": superseded,
                "deleted": deleted,
                "tags": tags,
                "person_keys": person_keys,
                "topic_keys": topic_keys,
                "transcript_word_count": transcript_word_count,
                "capture_duration_seconds": capture_duration_seconds,
            }
        ),
        privacy_scope=privacy_scope,
        conversation_id=conversation_id,
    )
    return TodayCardEvidence(
        kind=kind,
        title=title,
        summary=summary,
        source=source,
        confidence=float(confidence),
        meaningful=meaningful,
        confirmed=confirmed,
        positive_or_neutral=positive_or_neutral,
        superseded=superseded,
        deleted=deleted,
        tags=tags,
        person_keys=person_keys,
        topic_keys=topic_keys,
        transcript_word_count=transcript_word_count,
        capture_duration_seconds=capture_duration_seconds,
    )


async def _sources_are_current(queryable: Any, card: TodayCardRecord) -> bool:
    if card.invalidated_at is not None:
        return False
    if not card.source_refs:
        if card.state not in {TodayCardState.new_user, TodayCardState.degraded}:
            return False
        previous_day_start, previous_day_end = materialization_window(card.local_date, card.timezone)
        evidence = await _load_evidence(
            queryable,
            uid=card.uid,
            previous_day_start=previous_day_start,
            previous_day_end=previous_day_end,
            history_start=previous_day_start - timedelta(days=365),
        )
        _, current_watermark = today_card_source_pack(evidence)
        return card.source_watermark == current_watermark
    previous_day_start, previous_day_end = materialization_window(card.local_date, card.timezone)
    for source in card.source_refs:
        row = await queryable.fetchrow(
            """
            SELECT event_id, text, started_at, privacy_scope, scan_policy, source_ref, metadata
            FROM canonical_events
            WHERE uid = $1
              AND COALESCE(
                    NULLIF(source_ref ->> 'conversation_id', ''),
                    NULLIF(metadata #>> '{today_card,source_id}', ''),
                    event_id
                  ) = $2
              AND (
                    COALESCE(metadata ->> 'adapter', '') = 'omi-enriched-conversation'
                    OR metadata ? 'today_card'
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM ella_today_card_source_tombstones tombstone
                    WHERE tombstone.uid = canonical_events.uid
                      AND tombstone.source_id = $2
              )
            ORDER BY inserted_at DESC, started_at DESC, event_id DESC
            LIMIT 1
            """,
            card.uid,
            source.source_id,
        )
        if row is None:
            return False
        try:
            current = _evidence_from_row(
                row,
                previous_day_start=previous_day_start,
                previous_day_end=previous_day_end,
            )
        except (TypeError, ValueError):
            return False
        if (
            current is None
            or not evidence_is_safe(current)
            or current.source.source_id != source.source_id
            or current.source.source_version_id != source.source_version_id
            or current.source.evidence_hash != source.evidence_hash
        ):
            return False
    return True


async def _load_evidence(
    queryable: Any,
    *,
    uid: str,
    previous_day_start: datetime,
    previous_day_end: datetime,
    history_start: datetime,
) -> list[TodayCardEvidence]:
    rows = await queryable.fetch(
        """
        SELECT event_id, text, started_at, privacy_scope, scan_policy, source_ref, metadata
        FROM (
            SELECT uid, event_id, text, started_at, inserted_at, privacy_scope, scan_policy, source_ref, metadata,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(
                           NULLIF(source_ref ->> 'conversation_id', ''),
                           NULLIF(metadata #>> '{today_card,source_id}', ''),
                           event_id
                       )
                       ORDER BY inserted_at DESC, started_at DESC, event_id DESC
                   ) AS source_rank
            FROM canonical_events
            WHERE uid = $1
              AND started_at >= $2
              AND (
                    COALESCE(metadata ->> 'adapter', '') = 'omi-enriched-conversation'
                    OR metadata ? 'today_card'
              )
        ) evidence_row
        WHERE source_rank = 1
          AND NOT EXISTS (
                SELECT 1
                FROM ella_today_card_source_tombstones tombstone
                WHERE tombstone.uid = evidence_row.uid
                  AND tombstone.source_id = COALESCE(
                        NULLIF(evidence_row.source_ref ->> 'conversation_id', ''),
                        NULLIF(evidence_row.metadata #>> '{today_card,source_id}', ''),
                        evidence_row.event_id
                  )
          )
        ORDER BY started_at DESC, event_id ASC
        LIMIT 500
        """,
        uid,
        history_start,
    )
    evidence: list[TodayCardEvidence] = []
    for row in rows:
        try:
            item = _evidence_from_row(
                row,
                previous_day_start=previous_day_start,
                previous_day_end=previous_day_end,
            )
        except (TypeError, ValueError):
            logger.warning("[FLOW:TODAY-CARD] malformed canonical evidence skipped")
            continue
        if item is not None:
            evidence.append(item)
    return evidence


class PostgresTodayCardRepository:
    def __init__(self, pool_getter: Callable[[], Awaitable[Any]]):
        self._pool_getter = pool_getter

    async def _pool(self):
        return await self._pool_getter()

    async def get_user_context(self, uid: str) -> TodayCardUserContext | None:
        pool = await self._pool()
        row = await pool.fetchrow(
            """
            SELECT u.omi_uid, COALESCE(NULLIF(u.timezone, ''), 'UTC') AS timezone,
                   u.created_at,
                   (SELECT COUNT(*) FROM canonical_events ce WHERE ce.uid = u.omi_uid) AS canonical_event_count
            FROM users u
            WHERE u.omi_uid = $1
            """,
            uid,
        )
        if row is None:
            return None
        return TodayCardUserContext(
            uid=str(_record_value(row, "omi_uid")),
            timezone=str(_record_value(row, "timezone") or "UTC"),
            created_at=_record_value(row, "created_at"),
            canonical_event_count=int(_record_value(row, "canonical_event_count") or 0),
        )

    async def get_current(self, uid: str, local_date: date) -> TodayCardRecord | None:
        pool = await self._pool()
        row = await pool.fetchrow(
            """
            SELECT * FROM ella_today_cards
            WHERE uid = $1 AND local_date = $2 AND contract_version = $3
            """,
            uid,
            local_date,
            TODAY_CARD_CONTRACT_VERSION,
        )
        return _card_from_row(row) if row else None

    async def get_by_id(self, uid: str, card_id: str) -> TodayCardRecord | None:
        pool = await self._pool()
        row = await pool.fetchrow(
            "SELECT * FROM ella_today_cards WHERE uid = $1 AND card_id = $2::uuid",
            uid,
            card_id,
        )
        return _card_from_row(row) if row else None

    async def claim_materialization(
        self,
        *,
        uid: str,
        local_date: date,
        timezone_name: str,
        now: datetime,
        force_regenerate: bool,
    ) -> TodayCardMaterializationClaim:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{TODAY_CARD_CONTRACT_VERSION}:{uid}:{local_date.isoformat()}",
                )
                row = await conn.fetchrow(
                    """
                    SELECT * FROM ella_today_cards
                    WHERE uid = $1 AND local_date = $2 AND contract_version = $3
                    FOR UPDATE
                    """,
                    uid,
                    local_date,
                    TODAY_CARD_CONTRACT_VERSION,
                )
                if row is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ella_today_cards (
                            card_id, uid, local_date, timezone, contract_version,
                            version, state, render_contract_version, updated_at
                        )
                        VALUES ($1::uuid, $2, $3, $4, $5, 1, 'preparing', $6, $7)
                        RETURNING *
                        """,
                        deterministic_card_id(uid, local_date),
                        uid,
                        local_date,
                        timezone_name,
                        TODAY_CARD_CONTRACT_VERSION,
                        TODAY_CARD_RENDER_CONTRACT_VERSION,
                        now,
                    )
                    return TodayCardMaterializationClaim(card=_card_from_row(row), acquired=True)

                existing = _card_from_row(row)
                still_running = existing.state == TodayCardState.preparing and existing.updated_at >= now - timedelta(
                    minutes=5
                )
                if still_running or not force_regenerate:
                    return TodayCardMaterializationClaim(card=existing, acquired=False)
                row = await conn.fetchrow(
                    """
                    UPDATE ella_today_cards
                    SET version = version + 1,
                        timezone = $2,
                        state = 'preparing',
                        kind = NULL,
                        content = NULL,
                        source_refs = '[]'::jsonb,
                        evidence_hash = NULL,
                        source_watermark = NULL,
                        generated_at = NULL,
                        invalidated_at = NULL,
                        invalidation_reason = NULL,
                        reason_code = NULL,
                        updated_at = $3
                    WHERE card_id = $1
                    RETURNING *
                    """,
                    _record_value(row, "card_id"),
                    timezone_name,
                    now,
                )
                return TodayCardMaterializationClaim(card=_card_from_row(row), acquired=True)

    async def load_evidence(
        self,
        *,
        uid: str,
        previous_day_start: datetime,
        previous_day_end: datetime,
        history_start: datetime,
    ) -> list[TodayCardEvidence]:
        pool = await self._pool()
        return await _load_evidence(
            pool,
            uid=uid,
            previous_day_start=previous_day_start,
            previous_day_end=previous_day_end,
            history_start=history_start,
        )

    async def load_avoidance(self, uid: str, local_date: date) -> TodayCardAvoidance:
        pool = await self._pool()
        rows = await pool.fetch(
            """
            SELECT source_refs, private_consolidation
            FROM ella_today_cards
            WHERE uid = $1
              AND local_date < $2
              AND local_date >= $2 - $3::integer
              AND state IN ('ready', 'new_user')
            ORDER BY local_date DESC
            """,
            uid,
            local_date,
            TODAY_CARD_SOURCE_COOLDOWN_DAYS,
        )
        source_ids: set[str] = set()
        person_keys: set[str] = set()
        topic_keys: set[str] = set()
        for row in rows:
            for source in _json_list(_record_value(row, "source_refs")):
                if isinstance(source, dict) and source.get("source_id"):
                    source_ids.add(str(source["source_id"]))
            consolidation = _json_object(_record_value(row, "private_consolidation"))
            selected = consolidation.get("selected") if isinstance(consolidation.get("selected"), dict) else {}
            person_keys.update(str(value) for value in selected.get("person_keys", []) if str(value).strip())
            topic_keys.update(str(value) for value in selected.get("topic_keys", []) if str(value).strip())
        return TodayCardAvoidance(source_ids=source_ids, person_keys=person_keys, topic_keys=topic_keys)

    async def sources_are_current(self, card: TodayCardRecord) -> bool:
        pool = await self._pool()
        return await _sources_are_current(pool, card)

    async def save_materialized(self, card: TodayCardRecord) -> TodayCardRecord:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await lock_today_card_source_validity(conn, card.uid)
                if not await _sources_are_current(conn, card):
                    raise RuntimeError("today_card_materialization_conflict")
                row = await conn.fetchrow(
                    """
                    UPDATE ella_today_cards
            SET state = $3,
                kind = $4,
                content = $5::jsonb,
                source_refs = $6::jsonb,
                evidence_hash = $7,
                source_watermark = $8,
                render_contract_version = $9,
                private_consolidation = $10::jsonb,
                presentation = $11::jsonb,
                interaction_state = $12::jsonb,
                reason_code = $13,
                generated_at = $14,
                invalidated_at = $15,
                invalidation_reason = $16,
                updated_at = $17
            WHERE card_id = $1::uuid
              AND version = $2
              AND state = 'preparing'
              AND NOT EXISTS (
                    SELECT 1
                    FROM ella_today_card_source_tombstones tombstone
                    WHERE tombstone.uid = ella_today_cards.uid
                      AND $6::jsonb @> jsonb_build_array(
                            jsonb_build_object('source_id', tombstone.source_id)
                      )
              )
            RETURNING *
                    """,
                    card.card_id,
                    card.version,
                    card.state.value,
                    card.kind.value if card.kind else None,
                    json.dumps(card.content.model_dump(mode="json")) if card.content else None,
                    json.dumps([source.model_dump(mode="json") for source in card.source_refs]),
                    card.evidence_hash,
                    card.source_watermark,
                    card.render_contract_version,
                    json.dumps(card.private_consolidation, default=str),
                    json.dumps(card.presentation.model_dump(mode="json")),
                    json.dumps(card.interaction_state, default=str),
                    card.reason_code,
                    card.generated_at,
                    card.invalidated_at,
                    card.invalidation_reason,
                    card.updated_at,
                )
        if row is None:
            raise RuntimeError("today_card_materialization_conflict")
        return _card_from_row(row)

    async def invalidate_source(
        self,
        *,
        uid: str,
        source_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await lock_today_card_source_validity(conn, uid)
                result = await conn.execute(
                    """
            UPDATE ella_today_cards
            SET invalidated_at = $4,
                invalidation_reason = $3,
                updated_at = $4
            WHERE uid = $1
              AND invalidated_at IS NULL
              AND source_refs @> $2::jsonb
                    """,
                    uid,
                    json.dumps([{"source_id": source_id}]),
                    reason[:120],
                    now or datetime.now(timezone.utc),
                )
        return int(str(result).split()[-1])

    async def tombstone_source(
        self,
        *,
        uid: str,
        source_id: str,
        reason: str,
    ) -> int:
        if reason not in {"source_deleted", "source_retracted"}:
            raise ValueError("today_card_tombstone_reason_invalid")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await lock_today_card_source_validity(conn, uid)
                await conn.execute(
                    """
                    INSERT INTO ella_today_card_source_tombstones (uid, source_id, reason)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (uid, source_id) DO UPDATE
                    SET reason = EXCLUDED.reason,
                        deleted_at = LEAST(
                            ella_today_card_source_tombstones.deleted_at,
                            EXCLUDED.deleted_at
                        )
                    """,
                    uid,
                    source_id,
                    reason,
                )
                await conn.execute(
                    """
                    DELETE FROM canonical_event_sessions
                    WHERE uid = $1
                      AND source_ref ->> 'conversation_id' = $2
                    """,
                    uid,
                    source_id,
                )
                await conn.execute(
                    """
                    DELETE FROM canonical_events
                    WHERE uid = $1
                      AND (
                            source_ref ->> 'conversation_id' = $2
                            OR metadata #>> '{today_card,source_id}' = $2
                            OR event_id = $2
                      )
                    """,
                    uid,
                    source_id,
                )
                invalidated = await self.invalidate_source_in_connection(
                    conn,
                    uid=uid,
                    source_id=source_id,
                    reason=reason,
                )
        # Retain an explicit post-commit pass for callers that predate the
        # shared source-validity lock; new publication paths are fenced above.
        invalidated += await self.invalidate_source(uid=uid, source_id=source_id, reason=reason)
        return invalidated

    @staticmethod
    async def invalidate_source_in_connection(
        conn: Any,
        *,
        uid: str,
        source_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        result = await conn.execute(
            """
            UPDATE ella_today_cards
            SET invalidated_at = $4,
                invalidation_reason = $3,
                updated_at = $4
            WHERE uid = $1
              AND invalidated_at IS NULL
              AND source_refs @> $2::jsonb
            """,
            uid,
            json.dumps([{"source_id": source_id}]),
            reason[:120],
            now or datetime.now(timezone.utc),
        )
        return int(str(result).split()[-1])

    async def list_due_users(self, now: datetime, limit: int) -> list[str]:
        pool = await self._pool()
        rows = await pool.fetch(
            """
            SELECT u.omi_uid
            FROM users u
            JOIN pg_timezone_names tz
              ON tz.name = COALESCE(NULLIF(u.timezone, ''), 'UTC')
            WHERE u.omi_uid IS NOT NULL
              AND (EXTRACT(HOUR FROM ($1 AT TIME ZONE tz.name)) = 2
                   AND EXTRACT(MINUTE FROM ($1 AT TIME ZONE tz.name)) >= 30
                   OR EXTRACT(HOUR FROM ($1 AT TIME ZONE tz.name)) = 3)
              AND NOT EXISTS (
                    SELECT 1 FROM ella_today_cards c
                    WHERE c.uid = u.omi_uid
                      AND c.local_date = ($1 AT TIME ZONE tz.name)::date
                      AND c.contract_version = $2
                      AND c.invalidated_at IS NULL
              )
            ORDER BY u.omi_uid
            LIMIT $3
            """,
            now,
            TODAY_CARD_CONTRACT_VERSION,
            limit,
        )
        return [str(_record_value(row, "omi_uid")) for row in rows]

    async def record_feedback(
        self,
        *,
        uid: str,
        card_id: str,
        expected_version: int,
        feedback_id: str,
        action: TodayCardFeedbackAction,
    ) -> tuple[TodayCardRecord, bool]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM ella_today_cards WHERE uid = $1 AND card_id = $2::uuid FOR UPDATE",
                    uid,
                    card_id,
                )
                if row is None:
                    raise LookupError("today_card_not_found")
                card = _card_from_row(row)
                if card.version != expected_version:
                    raise ValueError("today_card_version_stale")
                if not await _sources_are_current(conn, card):
                    raise ValueError("today_card_source_stale")
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO ella_today_card_feedback (
                        feedback_id, card_id, uid, expected_version, action, source_fingerprint
                    )
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)
                    ON CONFLICT (feedback_id) DO NOTHING
                    RETURNING feedback_id
                    """,
                    feedback_id,
                    card_id,
                    uid,
                    expected_version,
                    action.value,
                    card.evidence_hash,
                )
                if inserted is None:
                    existing_feedback = await conn.fetchrow(
                        """
                        SELECT uid, card_id::text AS card_id, expected_version, action
                        FROM ella_today_card_feedback
                        WHERE feedback_id = $1::uuid
                        """,
                        feedback_id,
                    )
                    if (
                        existing_feedback is None
                        or str(_record_value(existing_feedback, "uid")) != uid
                        or str(_record_value(existing_feedback, "card_id")) != card_id
                        or int(_record_value(existing_feedback, "expected_version") or 0) != expected_version
                        or str(_record_value(existing_feedback, "action")) != action.value
                    ):
                        raise ValueError("today_card_feedback_conflict")
                    return card, False
                state = dict(card.interaction_state)
                state["last_feedback"] = action.value
                if action in {TodayCardFeedbackAction.hide, TodayCardFeedbackAction.less_like_this}:
                    state["avoid_source"] = True
                row = await conn.fetchrow(
                    """
                    UPDATE ella_today_cards
                    SET interaction_state = $3::jsonb, updated_at = NOW()
                    WHERE uid = $1 AND card_id = $2::uuid AND version = $4
                    RETURNING *
                    """,
                    uid,
                    card_id,
                    json.dumps(state),
                    expected_version,
                )
                return _card_from_row(row), True


async def invalidate_deleted_conversation_source(uid: str, conversation_id: str) -> int:
    """Tombstone and remove exact canonical evidence before source deletion."""
    repository = PostgresTodayCardRepository(get_ella_postgres_pool)
    try:
        return await repository.tombstone_source(
            uid=uid,
            source_id=conversation_id,
            reason="source_deleted",
        )
    except asyncpg.UndefinedTableError:
        # Preserve the additive migration compatibility window. Once migration
        # 016 exists, every other failure blocks deletion before content can be
        # detached from its source.
        logger.warning("[FLOW:TODAY-CARD] delete invalidation unavailable before migration 016")
        return 0
