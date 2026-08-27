import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def _load_module():
    database_module_name = "database.conversations"
    database_stub = types.ModuleType(database_module_name)
    database_stub.get_conversation = lambda uid, memory_id: None
    database_stub.claim_conversation_processing_retry = lambda uid, memory_id, request_id: None
    saved_database = sys.modules.get(database_module_name)
    sys.modules[database_module_name] = database_stub
    module_name = "ella_memory_artwork_recovery_test_module"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[2] / "ella" / "services" / "memory_artwork_recovery.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        if saved_database is None:
            sys.modules.pop(database_module_name, None)
        else:
            sys.modules[database_module_name] = saved_database
    return module


memory_artwork_recovery = _load_module()


def test_recovery_claim_is_deterministic_for_the_same_owner_source(monkeypatch):
    conversation = {
        "id": "memory-1",
        "active_summary_version_id": "generic-v1",
        "transcript_segments": [{"text": "private text", "speaker": 0}],
    }
    claims = []
    monkeypatch.setattr(memory_artwork_recovery.conversations_db, "get_conversation", lambda uid, cid: conversation)

    def claim(uid, memory_id, request_id):
        claims.append((uid, memory_id, request_id))
        return {"outcome": "claimed", "attempt_count": 1}

    monkeypatch.setattr(memory_artwork_recovery.conversations_db, "claim_conversation_processing_retry", claim)

    first = asyncio.run(memory_artwork_recovery.claim_memory_artwork_enrichment_recovery("owner-a", "memory-1"))
    second = asyncio.run(memory_artwork_recovery.claim_memory_artwork_enrichment_recovery("owner-a", "memory-1"))

    assert first["request_id"] == second["request_id"]
    assert claims[0] == claims[1]
    assert "owner-a" not in first["request_id"]


def test_recovery_claim_changes_when_terminal_source_changes(monkeypatch):
    conversations = iter(
        [
            {"active_summary_version_id": "generic-v1", "transcript_segments": [{"text": "one"}]},
            {"active_summary_version_id": "generic-v2", "transcript_segments": [{"text": "two"}]},
        ]
    )
    monkeypatch.setattr(
        memory_artwork_recovery.conversations_db,
        "get_conversation",
        lambda uid, cid: next(conversations),
    )
    monkeypatch.setattr(
        memory_artwork_recovery.conversations_db,
        "claim_conversation_processing_retry",
        lambda uid, memory_id, request_id: {"outcome": "claimed", "attempt_count": 1},
    )

    first = asyncio.run(memory_artwork_recovery.claim_memory_artwork_enrichment_recovery("owner-a", "memory-1"))
    second = asyncio.run(memory_artwork_recovery.claim_memory_artwork_enrichment_recovery("owner-a", "memory-1"))

    assert first["request_id"] != second["request_id"]
