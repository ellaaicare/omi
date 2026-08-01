import asyncio

from database.memory_reinterpretations import InMemoryMemoryReinterpretationRepository
from ella.routers.canonical_events import CanonicalEventIn, InMemoryCanonicalEventStore, SessionCompleteIn

UID = "today-card-correction-user"
SESSION_ID = "today-card-session"
CONVERSATION_ID = "conversation-a"
VERSION_ID = "summary-v1"


def _event(text: str) -> CanonicalEventIn:
    scope = {
        "scope_kind": "daily_card",
        "conversation_id": CONVERSATION_ID,
        "active_summary_version_id": VERSION_ID,
        "can_reinterpret": True,
    }
    return CanonicalEventIn(
        uid=UID,
        canonical_identity=UID,
        event_id="daily-card-turn",
        session_id=SESSION_ID,
        channel="ios_voice",
        provider="grok-realtime",
        role="user",
        text=text,
        started_at="2026-07-24T18:00:00Z",
        ended_at="2026-07-24T18:00:00Z",
        privacy_scope="user_private",
        scan_policy="none",
        source_ref={
            "source_identity": f"test:{UID}:{SESSION_ID}:0:user",
            "connection_id": "connection-a",
            "turn_index": 0,
            **scope,
        },
        metadata={"connection_id": "connection-a", "turn_index": 0, **scope},
    )


def _completion(*, correction_confirmed: bool | None) -> SessionCompleteIn:
    scope = {
        "scope_kind": "daily_card",
        "conversation_id": CONVERSATION_ID,
        "active_summary_version_id": VERSION_ID,
        "can_reinterpret": True,
    }
    if correction_confirmed is not None:
        scope["correction_confirmed"] = correction_confirmed
    return SessionCompleteIn(
        uid=UID,
        canonical_identity=UID,
        channel="ios_voice",
        provider="grok-realtime",
        started_at="2026-07-24T18:00:00Z",
        ended_at="2026-07-24T18:01:00Z",
        source_ref={
            "source_identity": f"grok-realtime:ios_voice:session:{SESSION_ID}",
            **scope,
        },
        metadata=scope,
    )


def test_daily_card_reminiscence_does_not_mutate_without_explicit_confirmation():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        store = InMemoryCanonicalEventStore(repository)
        await store.write_batch([_event("A normal reminiscence.")])

        result = await store.complete_session(SESSION_ID, _completion(correction_confirmed=None))

        assert result["reinterpretation"] is None
        assert repository.jobs == {}

    asyncio.run(run())


def test_daily_card_explicit_confirmed_single_source_reuses_reinterpretation_outbox():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        store = InMemoryCanonicalEventStore(repository)
        await store.write_batch([_event("A confirmed correction.")])

        result = await store.complete_session(SESSION_ID, _completion(correction_confirmed=True))

        assert result["reinterpretation"]["job_id"]
        assert len(repository.jobs) == 1

    asyncio.run(run())
