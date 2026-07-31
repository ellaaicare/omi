import asyncio

from ella.routers import canonical_events
from ella.routers.canonical_events import (
    CanonicalEventIn,
    InMemoryCanonicalEventStore,
    PostgresCanonicalEventStore,
)


def _event(
    event_id: str,
    text: str,
    *,
    uid: str = "uid-a",
    conversation_id: str = "",
    card_id: str = "",
) -> CanonicalEventIn:
    scope = {}
    if conversation_id:
        scope = {
            "scope_kind": "memory",
            "conversation_id": conversation_id,
            "active_summary_version_id": "version-1",
        }
    elif card_id:
        scope = {
            "scope_kind": "daily_card",
            "card_id": card_id,
            "card_version": 1,
        }
    return CanonicalEventIn(
        uid=uid,
        canonical_identity=uid,
        event_id=event_id,
        session_id="session-a",
        channel="ios_voice",
        provider="grok-realtime",
        role="user",
        text=text,
        started_at="2026-07-24T10:00:00Z",
        scan_policy="none" if conversation_id else "immediate",
        source_ref={
            "source_identity": f"test:{event_id}",
            **scope,
        },
        metadata=scope,
    )


def test_public_canonical_timeline_excludes_memory_and_daily_card_scoped_voice_events():
    store = InMemoryCanonicalEventStore()
    asyncio.run(
        store.write_batch(
            [
                _event("general", "General voice turn"),
                _event("scoped-a", "Private memory A turn", conversation_id="memory-a"),
                _event("scoped-b", "Private memory B turn", conversation_id="memory-b"),
                _event("daily-card", "Private daily card turn", card_id="card-a"),
            ]
        )
    )

    timeline = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=None))

    assert [event["event_id"] for event in timeline] == ["general"]


def test_public_canonical_timeline_uses_exact_case_sensitive_uid():
    store = InMemoryCanonicalEventStore()
    asyncio.run(
        store.write_batch(
            [
                _event("upper-general", "User A general turn", uid="UserA"),
                _event("lower-general", "Lowercase user general turn", uid="usera"),
                _event(
                    "upper-scoped",
                    "User A scoped turn",
                    uid="UserA",
                    conversation_id="shared-memory-id",
                ),
                _event(
                    "lower-scoped",
                    "Lowercase user scoped turn",
                    uid="usera",
                    conversation_id="shared-memory-id",
                ),
            ]
        )
    )

    timeline = asyncio.run(store.timeline(uid="UserA", since=None, limit=50, channels=None))

    assert [event["event_id"] for event in timeline] == ["upper-general"]


def test_postgres_public_timeline_query_uses_exact_uid(monkeypatch):
    queries = []

    class Pool:
        async def fetch(self, query, *args):
            queries.append((query, args))
            return []

    async def get_pool():
        return Pool()

    monkeypatch.setattr(canonical_events, "_get_pool", get_pool)

    timeline = asyncio.run(
        PostgresCanonicalEventStore().timeline(
            uid="UserA",
            since=None,
            limit=50,
            channels=None,
        )
    )

    assert timeline == []
    assert "uid = $1" in queries[0][0]
    assert "lower(uid)" not in queries[0][0]
