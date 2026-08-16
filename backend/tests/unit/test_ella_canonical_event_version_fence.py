import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

_module_path = Path(__file__).resolve().parents[2] / "ella" / "routers" / "canonical_events.py"
_module_spec = importlib.util.spec_from_file_location("ella_canonical_events_version_fence_test_module", _module_path)
canonical_events = importlib.util.module_from_spec(_module_spec)
assert _module_spec is not None and _module_spec.loader is not None
_module_spec.loader.exec_module(canonical_events)


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FenceConnection:
    def __init__(self, existing_active_version_id, *, exact_replay=True):
        self.existing_active_version_id = existing_active_version_id
        self.exact_replay = exact_replay
        self.queries = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        incoming_metadata = json.loads(args[14])
        incoming_active = incoming_metadata["active_summary_version_id"]
        ancestors = incoming_metadata["summary_version_ancestor_ids"]
        assert "summary_version_ancestor_ids" in query
        assert "canonical_events.metadata" in query
        if self.existing_active_version_id is None:
            return {"inserted": True}
        if self.existing_active_version_id != incoming_active and self.existing_active_version_id in ancestors:
            self.existing_active_version_id = incoming_active
            return {"inserted": False}
        return None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "raw_event =" in query:
            return self.exact_replay
        return self.existing_active_version_id


class _FencePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def _summary_event(active_version_id, ancestors):
    return canonical_events.CanonicalEventIn(
        uid="owner-1",
        canonical_identity="owner-1",
        event_id="omi:conv-1:summary",
        session_id="conv-1",
        channel="omi",
        provider="omi-backend",
        role="user",
        text=active_version_id,
        started_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
        source_ref={"source_identity": "omi:conv-1"},
        metadata={
            "adapter": "omi-enriched-conversation",
            "active_summary_version_id": active_version_id,
            "summary_version_ancestor_ids": ancestors,
        },
    )


def test_stale_corrected_publisher_cannot_overwrite_newer_undo(monkeypatch):
    connection = _FenceConnection("undo-v3")

    async def pool():
        return _FencePool(connection)

    monkeypatch.setattr(canonical_events, "_get_pool", pool)
    result = asyncio.run(
        canonical_events.PostgresCanonicalEventStore().write_batch([_summary_event("corrected-v2", ["base-v1"])])
    )

    assert result["ok"] is False
    assert result["stale"] == 1
    assert result["events"][0]["existing_active_summary_version_id"] == "undo-v3"
    assert connection.existing_active_version_id == "undo-v3"


def test_ack_loss_replay_of_exact_canonical_version_is_a_confirmed_duplicate(monkeypatch):
    connection = _FenceConnection("corrected-v2")

    async def pool():
        return _FencePool(connection)

    monkeypatch.setattr(canonical_events, "_get_pool", pool)
    result = asyncio.run(
        canonical_events.PostgresCanonicalEventStore().write_batch([_summary_event("corrected-v2", ["base-v1"])])
    )

    assert result["ok"] is True
    assert result["duplicates"] == 1
    assert result["stale"] == 0
    assert connection.existing_active_version_id == "corrected-v2"

    mismatched_connection = _FenceConnection("corrected-v2", exact_replay=False)

    async def mismatched_pool():
        return _FencePool(mismatched_connection)

    monkeypatch.setattr(canonical_events, "_get_pool", mismatched_pool)
    mismatched = asyncio.run(
        canonical_events.PostgresCanonicalEventStore().write_batch([_summary_event("corrected-v2", ["base-v1"])])
    )
    assert mismatched["ok"] is False
    assert mismatched["duplicates"] == 0
    assert mismatched["stale"] == 1
