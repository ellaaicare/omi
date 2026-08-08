"""Canonical, source-backed daily companion card domain."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

TODAY_CARD_CONTRACT_VERSION = "ella.today_card.v1"
TODAY_CARD_RENDER_CONTRACT_VERSION = "ella.today_card.render.v1"
TODAY_CARD_MATERIALIZATION_HOUR = 3
TODAY_CARD_SOURCE_COOLDOWN_DAYS = 30
TODAY_CARD_TOPIC_COOLDOWN_DAYS = 14
TODAY_CARD_MIN_CONFIDENCE = 0.75

_DENIED_PRIVACY_SCOPES = {
    "caregiver_private",
    "care_team_private",
    "guardian_private",
    "system_private",
}
_DENIED_TAGS = {
    "abuse",
    "argument",
    "caregiver_private",
    "distress",
    "emergency",
    "financial",
    "grief",
    "guardian",
    "health",
    "medication",
    "safety",
    "self_harm",
}
_WHITESPACE = re.compile(r"\s+")


class TodayCardState(str, Enum):
    ready = "ready"
    preparing = "preparing"
    new_user = "new_user"
    degraded = "degraded"


class TodayCardKind(str, Enum):
    recap = "recap"
    memory = "memory"
    interest = "interest"
    welcome = "welcome"


class TodayCardFeedbackAction(str, Enum):
    helpful = "helpful"
    not_relevant = "not_relevant"
    hide = "hide"
    less_like_this = "less_like_this"


class TodayCardSourceRef(BaseModel):
    source_type: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=256)
    source_version_id: str | None = Field(default=None, max_length=256)
    occurred_at: datetime | None = None
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    privacy_scope: str = Field(default="user_private", max_length=64)
    conversation_id: str | None = Field(default=None, max_length=256)


class TodayCardEvidence(BaseModel):
    kind: TodayCardKind
    title: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=4000)
    source: TodayCardSourceRef
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    meaningful: bool = True
    confirmed: bool = False
    positive_or_neutral: bool = True
    superseded: bool = False
    deleted: bool = False
    tags: list[str] = Field(default_factory=list, max_length=50)
    person_keys: list[str] = Field(default_factory=list, max_length=50)
    topic_keys: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("title", "summary")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return _WHITESPACE.sub(" ", value.replace("\x00", " ")).strip()


class TodayCardPresentation(BaseModel):
    style: str = "letter"
    artwork_ref: str | None = None
    alt_text: str | None = None


class TodayCardContent(BaseModel):
    eyebrow: str = Field(min_length=1, max_length=120)
    headline: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=1800)
    spoken_text: str = Field(min_length=1, max_length=2200)
    sentence_source_ids: list[str] = Field(default_factory=list)


class TodayCardRecord(BaseModel):
    card_id: str
    uid: str
    local_date: date
    timezone: str
    contract_version: str = TODAY_CARD_CONTRACT_VERSION
    version: int = Field(default=1, ge=1)
    state: TodayCardState
    kind: TodayCardKind | None = None
    content: TodayCardContent | None = None
    source_refs: list[TodayCardSourceRef] = Field(default_factory=list)
    evidence_hash: str | None = None
    source_watermark: str | None = None
    render_contract_version: str = TODAY_CARD_RENDER_CONTRACT_VERSION
    generated_at: datetime | None = None
    updated_at: datetime
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    presentation: TodayCardPresentation = Field(default_factory=TodayCardPresentation)
    interaction_state: dict[str, Any] = Field(default_factory=dict)
    private_consolidation: dict[str, Any] = Field(default_factory=dict)
    reason_code: str | None = None


class TodayCardAvoidance(BaseModel):
    source_ids: set[str] = Field(default_factory=set)
    person_keys: set[str] = Field(default_factory=set)
    topic_keys: set[str] = Field(default_factory=set)


class TodayCardUserContext(BaseModel):
    uid: str
    timezone: str
    created_at: datetime | None = None
    canonical_event_count: int = 0


@dataclass(frozen=True)
class TodayCardMaterializationClaim:
    card: TodayCardRecord
    acquired: bool


class TodayCardMaterializationRepository(Protocol):
    async def get_user_context(self, uid: str) -> TodayCardUserContext | None: ...

    async def get_current(self, uid: str, local_date: date) -> TodayCardRecord | None: ...

    async def claim_materialization(
        self,
        *,
        uid: str,
        local_date: date,
        timezone_name: str,
        now: datetime,
        force_regenerate: bool,
    ) -> TodayCardMaterializationClaim: ...

    async def load_evidence(
        self,
        *,
        uid: str,
        previous_day_start: datetime,
        previous_day_end: datetime,
        history_start: datetime,
    ) -> list[TodayCardEvidence]: ...

    async def load_avoidance(self, uid: str, local_date: date) -> TodayCardAvoidance: ...

    async def sources_are_current(self, card: TodayCardRecord) -> bool: ...

    async def save_materialized(self, card: TodayCardRecord) -> TodayCardRecord: ...

    async def list_due_users(self, now: datetime, limit: int) -> list[str]: ...


class TodayCardRenderer(Protocol):
    async def render(
        self,
        *,
        selected: TodayCardEvidence,
        local_date: date,
        timezone_name: str,
        private_consolidation: dict[str, Any],
    ) -> TodayCardContent: ...


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_ref(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def deterministic_card_id(uid: str, local_date: date) -> str:
    material = f"{TODAY_CARD_CONTRACT_VERSION}:{uid}:{local_date.isoformat()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def normalize_timezone(value: str) -> str:
    candidate = str(value or "").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("today_card_timezone_invalid") from exc
    return candidate


def materialization_window(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(normalize_timezone(timezone_name))
    previous_day = local_date - timedelta(days=1)
    start = datetime.combine(previous_day, time.min, tzinfo=tz)
    end = datetime.combine(local_date, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def evidence_is_safe(evidence: TodayCardEvidence) -> bool:
    tags = {str(tag).strip().lower() for tag in evidence.tags}
    if evidence.source.privacy_scope.lower() in _DENIED_PRIVACY_SCOPES:
        return False
    if tags.intersection(_DENIED_TAGS):
        return False
    if evidence.deleted or evidence.superseded or not evidence.meaningful:
        return False
    if evidence.confidence < TODAY_CARD_MIN_CONFIDENCE:
        return False
    if not evidence.positive_or_neutral:
        return False
    if not evidence.title and not evidence.summary:
        return False
    return True


def today_card_source_pack(evidence: list[TodayCardEvidence]) -> tuple[list[TodayCardEvidence], str]:
    """Return the bounded eligible evidence set and its deterministic watermark."""
    safe_evidence = sorted(
        (item for item in evidence if evidence_is_safe(item)),
        key=lambda item: (item.source.source_id, item.source.source_version_id or ""),
    )[:100]
    watermark = sha256_ref(
        [
            {
                "source_id": item.source.source_id,
                "source_version_id": item.source.source_version_id,
                "evidence_hash": item.source.evidence_hash,
            }
            for item in safe_evidence
        ]
    )
    return safe_evidence, watermark


def _rank(evidence: TodayCardEvidence) -> tuple[float, float, str]:
    occurred = evidence.source.occurred_at
    timestamp = occurred.timestamp() if occurred is not None else 0.0
    return (-evidence.confidence, -timestamp, evidence.source.source_id)


def select_today_card_evidence(
    evidence: list[TodayCardEvidence],
    *,
    previous_day_start: datetime,
    previous_day_end: datetime,
    avoidance: TodayCardAvoidance,
) -> TodayCardEvidence | None:
    safe = [item for item in evidence if evidence_is_safe(item)]

    def not_avoided(item: TodayCardEvidence) -> bool:
        if item.source.source_id in avoidance.source_ids:
            return False
        if set(item.person_keys).intersection(avoidance.person_keys):
            return False
        if set(item.topic_keys).intersection(avoidance.topic_keys):
            return False
        return True

    recaps = [
        item
        for item in safe
        if item.kind == TodayCardKind.recap
        and item.source.occurred_at is not None
        and previous_day_start <= item.source.occurred_at.astimezone(timezone.utc) < previous_day_end
    ]
    if recaps:
        return sorted(recaps, key=_rank)[0]

    memories = [item for item in safe if item.kind == TodayCardKind.memory and not_avoided(item)]
    if memories:
        return sorted(memories, key=_rank)[0]

    interests = [item for item in safe if item.kind == TodayCardKind.interest and item.confirmed and not_avoided(item)]
    if interests:
        return sorted(interests, key=_rank)[0]
    return None


class DeterministicTodayCardRenderer:
    """Render only already-supported source text behind a provider-neutral seam."""

    async def render(
        self,
        *,
        selected: TodayCardEvidence,
        local_date: date,
        timezone_name: str,
        private_consolidation: dict[str, Any],
    ) -> TodayCardContent:
        del local_date, private_consolidation
        source_id = selected.source.source_id
        headline = selected.title or selected.summary[:120]
        body = selected.summary or selected.title
        if selected.kind == TodayCardKind.recap:
            eyebrow = "A NOTE FROM YESTERDAY"
        elif selected.kind == TodayCardKind.memory:
            occurred = selected.source.occurred_at
            if occurred is None:
                raise ValueError("today_card_memory_date_missing")
            occurred_local = occurred.astimezone(ZoneInfo(timezone_name))
            eyebrow = f"A MEMORY FROM {occurred_local.strftime('%B').upper()} {occurred_local.day}"
        elif selected.kind == TodayCardKind.interest:
            eyebrow = "SOMETHING YOU ENJOY"
        else:
            raise ValueError("today_card_selected_kind_invalid")
        return TodayCardContent(
            eyebrow=eyebrow,
            headline=headline,
            body=body,
            spoken_text=f"{headline}. {body}" if headline != body else body,
            sentence_source_ids=[source_id],
        )


def welcome_content() -> TodayCardContent:
    return TodayCardContent(
        eyebrow="FOR YOU TODAY",
        headline="What would you like Ella to know?",
        body="Tell Ella about a person, place, or interest you enjoy.",
        spoken_text="Tell me about a person, place, or interest you enjoy.",
        sentence_source_ids=[],
    )


def validate_rendered_content(content: TodayCardContent, source_refs: list[TodayCardSourceRef]) -> None:
    source_ids = {source.source_id for source in source_refs}
    if not source_ids:
        if content.sentence_source_ids:
            raise ValueError("today_card_unexpected_source_reference")
        return
    if not content.sentence_source_ids or not set(content.sentence_source_ids).issubset(source_ids):
        raise ValueError("today_card_unverified_factual_sentence")


@dataclass(frozen=True)
class TodayCardMaterializationResult:
    card: TodayCardRecord
    created: bool


class TodayCardMaterializer:
    def __init__(
        self,
        repository: TodayCardMaterializationRepository,
        renderer: TodayCardRenderer | None = None,
        *,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self.repository = repository
        self.renderer = renderer or DeterministicTodayCardRenderer()
        self.clock = clock

    async def materialize(self, uid: str, target_date: date | None = None) -> TodayCardMaterializationResult:
        now = self.clock().astimezone(timezone.utc)
        user = await self.repository.get_user_context(uid)
        if user is None:
            raise LookupError("today_card_user_not_found")
        timezone_name = normalize_timezone(user.timezone)
        local_date = target_date or now.astimezone(ZoneInfo(timezone_name)).date()
        existing = await self.repository.get_current(uid, local_date)
        if (
            existing
            and existing.state != TodayCardState.preparing
            and await self.repository.sources_are_current(existing)
        ):
            return TodayCardMaterializationResult(card=existing, created=False)

        claim = await self.repository.claim_materialization(
            uid=uid,
            local_date=local_date,
            timezone_name=timezone_name,
            now=now,
            force_regenerate=existing is not None,
        )
        claimed = claim.card
        if not claim.acquired:
            return TodayCardMaterializationResult(card=claimed, created=False)
        if claimed.state != TodayCardState.preparing:
            return TodayCardMaterializationResult(card=claimed, created=False)

        previous_start, previous_end = materialization_window(local_date, timezone_name)
        evidence = await self.repository.load_evidence(
            uid=uid,
            previous_day_start=previous_start,
            previous_day_end=previous_end,
            history_start=previous_start - timedelta(days=365),
        )
        avoidance = await self.repository.load_avoidance(uid, local_date)
        selected = select_today_card_evidence(
            evidence,
            previous_day_start=previous_start,
            previous_day_end=previous_end,
            avoidance=avoidance,
        )
        safe_evidence, source_watermark = today_card_source_pack(evidence)
        private_consolidation = {
            "contract_version": TODAY_CARD_CONTRACT_VERSION,
            "window": {"start": previous_start.isoformat(), "end": previous_end.isoformat()},
            "source_count": len(evidence),
            "safe_source_count": len(safe_evidence),
            "source_watermark": source_watermark,
            "evidence_pack": [item.model_dump(mode="json") for item in safe_evidence],
        }

        if selected is None:
            is_new_user = user.canonical_event_count == 0
            card = claimed.model_copy(
                update={
                    "state": TodayCardState.new_user if is_new_user else TodayCardState.degraded,
                    "kind": TodayCardKind.welcome if is_new_user else None,
                    "content": welcome_content() if is_new_user else None,
                    "source_refs": [],
                    "evidence_hash": sha256_ref([]),
                    "source_watermark": source_watermark,
                    "generated_at": now,
                    "updated_at": now,
                    "private_consolidation": private_consolidation,
                    "reason_code": None if is_new_user else "no_safe_source",
                    "invalidated_at": None,
                    "invalidation_reason": None,
                }
            )
            return TodayCardMaterializationResult(card=await self.repository.save_materialized(card), created=True)

        try:
            private_consolidation["selected"] = {
                "source_id": selected.source.source_id,
                "person_keys": selected.person_keys,
                "topic_keys": selected.topic_keys,
            }
            content = await self.renderer.render(
                selected=selected,
                local_date=local_date,
                timezone_name=timezone_name,
                private_consolidation=private_consolidation,
            )
            validate_rendered_content(content, [selected.source])
            card = claimed.model_copy(
                update={
                    "state": TodayCardState.ready,
                    "kind": selected.kind,
                    "content": content,
                    "source_refs": [selected.source],
                    "evidence_hash": sha256_ref([selected.source.model_dump(mode="json")]),
                    "source_watermark": source_watermark,
                    "generated_at": now,
                    "updated_at": now,
                    "private_consolidation": private_consolidation,
                    "reason_code": None,
                    "invalidated_at": None,
                    "invalidation_reason": None,
                }
            )
        except ValueError:
            card = claimed.model_copy(
                update={
                    "state": TodayCardState.degraded,
                    "kind": None,
                    "content": None,
                    "source_refs": [],
                    "evidence_hash": sha256_ref([]),
                    "source_watermark": source_watermark,
                    "generated_at": now,
                    "updated_at": now,
                    "private_consolidation": private_consolidation,
                    "reason_code": "generation_output_invalid",
                    "invalidated_at": None,
                    "invalidation_reason": None,
                }
            )
        except Exception:
            card = claimed.model_copy(
                update={
                    "state": TodayCardState.degraded,
                    "kind": None,
                    "content": None,
                    "source_refs": [],
                    "evidence_hash": sha256_ref([]),
                    "source_watermark": source_watermark,
                    "generated_at": now,
                    "updated_at": now,
                    "private_consolidation": private_consolidation,
                    "reason_code": "generation_failed",
                    "invalidated_at": None,
                    "invalidation_reason": None,
                }
            )
        return TodayCardMaterializationResult(card=await self.repository.save_materialized(card), created=True)

    async def materialize_due(self, limit: int = 100) -> list[TodayCardMaterializationResult]:
        now = self.clock().astimezone(timezone.utc)
        uids = await self.repository.list_due_users(now, min(max(limit, 1), 500))
        return [await self.materialize(uid) for uid in uids]
