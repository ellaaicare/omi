import asyncio

from ella.routers.canonical_events import CanonicalEventIn, InMemoryCanonicalEventStore


def _event(event_id: str, text: str, *, conversation_id: str = "") -> CanonicalEventIn:
    scope = (
        {
            "scope_kind": "memory",
            "conversation_id": conversation_id,
            "active_summary_version_id": "version-1",
        }
        if conversation_id
        else {}
    )
    return CanonicalEventIn(
        uid="uid-a",
        canonical_identity="uid-a",
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


def test_public_canonical_timeline_excludes_memory_scoped_voice_events():
    store = InMemoryCanonicalEventStore()
    asyncio.run(
        store.write_batch(
            [
                _event("general", "General voice turn"),
                _event("scoped-a", "Private memory A turn", conversation_id="memory-a"),
                _event("scoped-b", "Private memory B turn", conversation_id="memory-b"),
            ]
        )
    )

    timeline = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=None))

    assert [event["event_id"] for event in timeline] == ["general"]
