import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_recovery_vector_refresh_preserves_metadata_without_transcript_extraction(monkeypatch):
    existing_metadata = {
        "uid": "stale-user",
        "memory_id": "stale-conversation",
        "created_at": 123,
        "topics": ["coffee"],
        "people_mentioned": ["Greg"],
        "custom_marker": "preserve-me",
        "active_summary_version_id": "generic-v1",
        "summary_content_sha256": "old-hash",
    }
    generated = []
    upserts = []
    forbidden = MagicMock(side_effect=AssertionError("transcript metadata extraction must not run"))

    vector_db = MagicMock()
    vector_db.fetch_conversation_vector_metadata.return_value = existing_metadata
    vector_db.upsert_conversation_vector.side_effect = lambda uid, conversation_id, vector, metadata: upserts.append(
        (uid, conversation_id, vector, metadata)
    ) or {"upserted_count": 1}
    notifications = MagicMock()
    notifications.get_user_time_zone.side_effect = AssertionError("timezone lookup must not run")
    llm_chat = MagicMock(
        retrieve_metadata_fields_from_transcript=forbidden,
        retrieve_metadata_from_message=forbidden,
        retrieve_metadata_from_text=forbidden,
    )
    llm_clients = MagicMock(generate_embedding=lambda content: generated.append(content) or [0.1, 0.2])
    monkeypatch.setitem(sys.modules, "database.notifications", notifications)
    monkeypatch.setitem(sys.modules, "database.vector_db", vector_db)
    monkeypatch.setitem(sys.modules, "utils.llm.chat", llm_chat)
    monkeypatch.setitem(sys.modules, "utils.llm.clients", llm_clients)

    module_path = Path(__file__).resolve().parents[2] / "utils" / "conversations" / "vector.py"
    spec = importlib.util.spec_from_file_location("summary_vector_refresh_test_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    structured = {
        "title": "Cafe breakfast",
        "overview": "[Ella] The user ordered breakfast.",
        "category": "personal",
    }
    conversation = SimpleNamespace(
        id="conversation-1",
        created_at=datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc),
        structured=structured,
    )

    result = module.refresh_structured_summary_vector(
        "user-1",
        conversation,
        summary_version_id="enriched-v2",
        summary_content_sha256="new-hash",
    )

    assert result == {"upserted_count": 1}
    assert generated == [
        json.dumps(
            structured,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    ]
    assert upserts == [
        (
            "user-1",
            "conversation-1",
            [0.1, 0.2],
            {
                **existing_metadata,
                "uid": "user-1",
                "memory_id": "conversation-1",
                "active_summary_version_id": "enriched-v2",
                "summary_content_sha256": "new-hash",
            },
        )
    ]
    forbidden.assert_not_called()
    notifications.get_user_time_zone.assert_not_called()
