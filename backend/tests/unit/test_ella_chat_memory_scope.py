import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("utils.other.endpoints", MagicMock())
sys.modules.setdefault("ella.config", MagicMock(ELLA_CONFIG=MagicMock(debug_level=0)))
sys.modules.setdefault("ella.routers.resolve", MagicMock(resolve_user_routing=MagicMock()))
sys.modules.setdefault("ella.routers.trace", MagicMock(RouteTrace=MagicMock(), record_trace=MagicMock()))
sys.modules.setdefault(
    "ella.routers.canonical_events",
    MagicMock(CanonicalEventIn=MagicMock(), PostgresCanonicalEventStore=MagicMock()),
)
sys.modules.setdefault(
    "ella.services.hermes_session",
    MagicMock(canonical_omi_session_key=lambda uid: f"ella:{uid}", safe_session_component=lambda value: value),
)
sys.modules.setdefault(
    "ella.services.provisioning",
    MagicMock(ProvisioningError=type("ProvisioningError", (Exception,), {"retryable": False, "code": "error"})),
)
sys.modules.setdefault(
    "ella.services.runtime_resolver",
    MagicMock(IsolatedRuntime=object, resolve_isolated_runtime=MagicMock(), runtime_bindings_enabled=MagicMock()),
)
sys.modules.setdefault(
    "utils.ella.canonical_context",
    MagicMock(
        DEFAULT_CONTEXT_CHANNELS=["ios_chat"],
        canonical_events_to_server_messages=MagicMock(),
        fetch_canonical_timeline=MagicMock(),
        format_canonical_context=MagicMock(return_value=""),
    ),
)
sys.modules.setdefault("utils.ella.time_context", MagicMock(timezone_name=lambda value: value or "UTC"))

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_chat_path = _backend_path / "ella" / "routers" / "chat.py"
_chat_spec = importlib.util.spec_from_file_location("ella_chat_memory_scope_test_module", _chat_path)
chat = importlib.util.module_from_spec(_chat_spec)
assert _chat_spec is not None and _chat_spec.loader is not None
_chat_spec.loader.exec_module(chat)


def test_memory_scoped_turn_attaches_to_existing_conversation_without_creating_memory(monkeypatch):
    updates = []
    conversation = {"memory_talk_state": {"turns": []}}
    conversations_db = sys.modules["database.conversations"]
    monkeypatch.setattr(conversations_db, "get_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(
        conversations_db, "update_conversation", lambda uid, conversation_id, update: updates.append(update)
    )

    chat._append_memory_talk_turn(
        uid="user-1",
        conversation_id="memory-1",
        turn_id="turn-1",
        user_text="Tell me about this.",
        assistant_text="It was a quiet morning in the garden.",
        started_at=datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 20, 9, 1, tzinfo=timezone.utc),
    )

    assert len(updates) == 1
    assert "memory_talk_state" in updates[0]
    assert updates[0]["memory_talk_state"]["has_discussion"] is True
    assert updates[0]["memory_talk_state"]["turn_count"] == 1
    assert all("structured" not in update for update in updates)
    assert not hasattr(conversations_db, "create_conversation") or not conversations_db.create_conversation.called
