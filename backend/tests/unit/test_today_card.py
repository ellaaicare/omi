import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ella.services.today_card import (
    DeterministicTodayCardRenderer,
    TODAY_CARD_CONTRACT_VERSION,
    TodayCardAvoidance,
    TodayCardContent,
    TodayCardEvidence,
    TodayCardKind,
    TodayCardMaterializationClaim,
    TodayCardMaterializer,
    TodayCardRecord,
    TodayCardSourceRef,
    TodayCardState,
    TodayCardUserContext,
    deterministic_card_id,
    evidence_is_safe,
    select_today_card_evidence,
    sha256_ref,
)

UID = "today-card-user"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
LOCAL_DATE = date(2026, 8, 1)


def _source(
    kind: TodayCardKind,
    source_id: str,
    *,
    occurred_at: datetime,
    version: str = "summary-v1",
    confidence: float = 0.95,
    confirmed: bool = False,
    tags: list[str] | None = None,
) -> TodayCardEvidence:
    return TodayCardEvidence(
        kind=kind,
        title=f"Title {source_id}",
        summary=f"Summary grounded in {source_id}.",
        source=TodayCardSourceRef(
            source_type="conversation_summary" if kind != TodayCardKind.interest else "confirmed_interest",
            source_id=source_id,
            source_version_id=version,
            occurred_at=occurred_at,
            evidence_hash=sha256_ref({"source_id": source_id, "version": version}),
            conversation_id=source_id if kind != TodayCardKind.interest else None,
        ),
        confidence=confidence,
        confirmed=confirmed,
        tags=tags or [],
        person_keys=[f"person:{source_id}"],
        topic_keys=[f"topic:{source_id}"],
    )


class InMemoryTodayCardRepository:
    def __init__(self, evidence: list[TodayCardEvidence], *, event_count: int = 1):
        self.evidence = evidence
        self.event_count = event_count
        self.current: TodayCardRecord | None = None
        self.current_sources = True
        self.avoidance = TodayCardAvoidance()
        self.claim_count = 0
        self.load_evidence_count = 0

    async def get_user_context(self, uid):
        if uid != UID:
            return None
        return TodayCardUserContext(uid=uid, timezone="UTC", canonical_event_count=self.event_count)

    async def get_current(self, uid, local_date):
        if uid == UID and self.current and self.current.local_date == local_date:
            return self.current
        return None

    async def claim_materialization(self, *, uid, local_date, timezone_name, now, force_regenerate):
        self.claim_count += 1
        version = (self.current.version + 1) if self.current and force_regenerate else 1
        self.current = TodayCardRecord(
            card_id=deterministic_card_id(uid, local_date),
            uid=uid,
            local_date=local_date,
            timezone=timezone_name,
            version=version,
            state=TodayCardState.preparing,
            updated_at=now,
        )
        self.current_sources = True
        return TodayCardMaterializationClaim(card=self.current, acquired=True)

    async def load_evidence(self, **_kwargs):
        self.load_evidence_count += 1
        return list(self.evidence)

    async def load_avoidance(self, _uid, _local_date):
        return self.avoidance

    async def sources_are_current(self, _card):
        return self.current_sources

    async def save_materialized(self, card):
        self.current = card
        return card

    async def list_due_users(self, _now, _limit):
        return [UID]


class MalformedRenderer:
    async def render(self, **_kwargs):
        return TodayCardContent(
            eyebrow="A NOTE FROM YESTERDAY",
            headline="Unsupported claim",
            body="This sentence has no source binding.",
            spoken_text="Unsupported claim.",
            sentence_source_ids=["wrong-source"],
        )


def _materialize(repository, renderer=None):
    return asyncio.run(
        TodayCardMaterializer(repository, renderer, clock=lambda: NOW).materialize(
            UID,
            LOCAL_DATE,
        )
    )


def test_low_value_fragment_and_meta_summary_are_never_safe_sources():
    fragment = _source(
        TodayCardKind.recap,
        "fragment",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(
        update={
            "title": "A very short moment",
            "summary": (
                "This tiny recording caught only the word So before it ended. "
                "There is not enough context to know what was being discussed."
            ),
        }
    )
    one_word = fragment.model_copy(update={"title": "So", "summary": "So"})
    short_transcript = _source(
        TodayCardKind.recap,
        "short-transcript",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(update={"transcript_word_count": 1})
    short_capture = _source(
        TodayCardKind.recap,
        "short-capture",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(update={"capture_duration_seconds": 2.5})

    assert evidence_is_safe(fragment) is False
    assert evidence_is_safe(one_word) is False
    assert evidence_is_safe(short_transcript) is False
    assert evidence_is_safe(short_capture) is False


def test_meaningful_non_latin_and_title_rich_sources_are_safe():
    non_latin = _source(
        TodayCardKind.recap,
        "japanese-garden",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(
        update={
            "title": "庭の思い出",
            "summary": "今日は母と庭でトマトを植えました",
        }
    )
    title_rich = _source(
        TodayCardKind.recap,
        "title-rich",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(
        update={
            "title": "Alex and Priya planted tomatoes together",
            "summary": "Garden",
        }
    )

    assert evidence_is_safe(non_latin) is True
    assert evidence_is_safe(title_rich) is True


@pytest.mark.parametrize(
    "junk_summary",
    [
        "The recording was too short to provide a useful summary.",
        ("The recording lacked enough detail to summarize what happened. " "No useful context was available."),
        ("The clip was inaudible and could not support a useful summary. " "Nothing meaningful could be recovered."),
        "Almost no speech was captured to summarize what happened; no usable detail remained.",
        ("The recording lacked enough detail to summarize what happened. " "No coherent account could be produced."),
    ],
)
def test_low_value_summary_never_reaches_rendered_card_even_with_substantive_title(junk_summary):
    evidence = _source(
        TodayCardKind.recap,
        "title-cannot-rescue-junk",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
    ).model_copy(
        update={
            "title": "Alex and Priya planted tomatoes together",
            "summary": junk_summary,
        }
    )

    result = _materialize(InMemoryTodayCardRepository([evidence]))
    serialized = result.card.model_dump_json()

    assert evidence_is_safe(evidence) is False
    assert result.card.state == TodayCardState.degraded
    assert result.card.reason_code == "no_safe_source"
    assert result.card.content is None
    assert junk_summary not in serialized


def test_active_day_materializes_truthful_recap_before_older_memory():
    repository = InMemoryTodayCardRepository(
        [
            _source(TodayCardKind.memory, "older", occurred_at=datetime(2026, 6, 12, tzinfo=timezone.utc)),
            _source(TodayCardKind.recap, "yesterday", occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc)),
        ]
    )

    result = _materialize(repository)

    assert result.card.contract_version == TODAY_CARD_CONTRACT_VERSION
    assert result.card.state == TodayCardState.ready
    assert result.card.kind == TodayCardKind.recap
    assert result.card.content.eyebrow == "A NOTE FROM YESTERDAY"
    assert result.card.source_refs[0].source_id == "yesterday"
    assert result.card.source_refs[0].source_version_id == "summary-v1"


def test_idle_day_uses_dated_safe_memory_then_confirmed_interest():
    memory_repository = InMemoryTodayCardRepository(
        [_source(TodayCardKind.memory, "memory", occurred_at=datetime(2026, 6, 12, tzinfo=timezone.utc))]
    )
    memory = _materialize(memory_repository).card

    assert memory.kind == TodayCardKind.memory
    assert memory.content.eyebrow == "A MEMORY FROM JUNE 12"

    interest_repository = InMemoryTodayCardRepository(
        [
            _source(
                TodayCardKind.interest,
                "gardening",
                occurred_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                confirmed=True,
            )
        ]
    )
    interest = _materialize(interest_repository).card

    assert interest.kind == TodayCardKind.interest
    assert interest.content.eyebrow == "SOMETHING YOU ENJOY"


def test_memory_label_uses_card_timezone_for_truthful_source_date():
    memory = _source(
        TodayCardKind.memory,
        "late-evening-local",
        occurred_at=datetime(2026, 6, 13, 1, tzinfo=timezone.utc),
    )

    content = asyncio.run(
        DeterministicTodayCardRenderer().render(
            selected=memory,
            local_date=LOCAL_DATE,
            timezone_name="America/Los_Angeles",
            private_consolidation={},
        )
    )

    assert content.eyebrow == "A MEMORY FROM JUNE 12"


def test_unconfirmed_interest_is_not_selected_and_new_user_gets_honest_welcome():
    unconfirmed = _source(
        TodayCardKind.interest,
        "gardening",
        occurred_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        confirmed=False,
    )
    existing_repository = InMemoryTodayCardRepository([unconfirmed], event_count=3)
    existing_user = _materialize(existing_repository).card
    new_user = _materialize(InMemoryTodayCardRepository([], event_count=0)).card

    assert existing_user.state == TodayCardState.degraded
    assert existing_user.reason_code == "no_safe_source"
    assert new_user.state == TodayCardState.new_user
    assert new_user.kind == TodayCardKind.welcome
    assert "appear" not in new_user.content.body.lower()
    assert new_user.source_refs == []

    second_existing = _materialize(existing_repository).card
    assert second_existing.state == TodayCardState.degraded
    assert second_existing.version == existing_user.version
    assert existing_repository.claim_count == 1


def test_malformed_generation_degrades_without_exposing_exception_or_content():
    repository = InMemoryTodayCardRepository(
        [_source(TodayCardKind.recap, "source", occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc))]
    )

    card = _materialize(repository, MalformedRenderer()).card

    assert card.state == TodayCardState.degraded
    assert card.content is None
    assert card.reason_code == "generation_output_invalid"


def test_duplicate_job_is_idempotent_and_notification_token_independent(monkeypatch):
    repository = InMemoryTodayCardRepository(
        [_source(TodayCardKind.recap, "source", occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc))]
    )
    monkeypatch.setenv("FCM_TOKEN", "not-consulted-one")
    first = _materialize(repository)
    monkeypatch.setenv("FCM_TOKEN", "not-consulted-two")
    second = _materialize(repository)

    assert first.created is True
    assert second.created is False
    assert second.card.card_id == first.card.card_id
    assert second.card.version == first.card.version
    assert repository.claim_count == 1
    assert repository.load_evidence_count == 1


def test_fresh_preparing_claim_is_returned_without_duplicate_generation():
    repository = InMemoryTodayCardRepository(
        [_source(TodayCardKind.recap, "source", occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc))]
    )
    repository.current = TodayCardRecord(
        card_id=deterministic_card_id(UID, LOCAL_DATE),
        uid=UID,
        local_date=LOCAL_DATE,
        timezone="UTC",
        version=1,
        state=TodayCardState.preparing,
        updated_at=NOW,
    )

    async def return_in_flight(**_kwargs):
        repository.claim_count += 1
        return TodayCardMaterializationClaim(card=repository.current, acquired=False)

    repository.claim_materialization = return_in_flight

    result = _materialize(repository)

    assert result.created is False
    assert result.card.state == TodayCardState.preparing
    assert repository.claim_count == 1
    assert repository.load_evidence_count == 0


def test_materializer_has_no_legacy_daily_summary_or_notification_dependency():
    source = (Path(__file__).parents[2] / "ella" / "services" / "today_card.py").read_text(encoding="utf-8")
    repository = (Path(__file__).parents[2] / "ella" / "services" / "today_card_postgres.py").read_text(
        encoding="utf-8"
    )

    assert "daily_summaries" not in source + repository
    assert "database.notifications" not in source + repository


def test_corrected_and_deleted_source_invalidation_regenerates_same_card_versioned():
    original = _source(
        TodayCardKind.recap,
        "conversation-a",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        version="summary-v1",
    )
    repository = InMemoryTodayCardRepository([original])
    first = _materialize(repository).card

    repository.current_sources = False
    repository.evidence = [
        original.model_copy(
            update={
                "source": original.source.model_copy(
                    update={
                        "source_version_id": "summary-v2",
                        "evidence_hash": sha256_ref({"source_id": "conversation-a", "version": "summary-v2"}),
                    }
                )
            }
        )
    ]
    corrected = _materialize(repository).card

    assert corrected.card_id == first.card_id
    assert corrected.version == 2
    assert corrected.source_refs[0].source_version_id == "summary-v2"

    repository.current_sources = False
    repository.evidence = []
    deleted = _materialize(repository).card

    assert deleted.version == 3
    assert deleted.state == TodayCardState.degraded
    assert deleted.reason_code == "no_safe_source"


def test_safety_and_avoidance_hooks_exclude_sensitive_and_repeated_sources():
    sensitive = _source(
        TodayCardKind.memory,
        "sensitive",
        occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        tags=["health"],
    )
    repeated = _source(TodayCardKind.memory, "repeat", occurred_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    fresh = _source(TodayCardKind.memory, "fresh", occurred_at=datetime(2026, 6, 3, tzinfo=timezone.utc))

    selected = select_today_card_evidence(
        [sensitive, repeated, fresh],
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        avoidance=TodayCardAvoidance(source_ids={"repeat"}),
    )

    assert selected.source.source_id == "fresh"
