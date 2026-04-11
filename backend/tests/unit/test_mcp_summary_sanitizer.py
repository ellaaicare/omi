import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

sys.modules.setdefault("database._client", MagicMock())
sys.modules.setdefault("database.memories", MagicMock())
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("database.mcp_api_key", MagicMock())

_llm_memories = types.ModuleType("utils.llm.memories")
_llm_memories.identify_category_for_memory = MagicMock()
sys.modules.setdefault("utils.llm.memories", _llm_memories)

_mcp_path = _backend_path / "routers" / "mcp_sse.py"
_mcp_spec = importlib.util.spec_from_file_location("mcp_sse_test_module", _mcp_path)
mcp_sse = importlib.util.module_from_spec(_mcp_spec)
assert _mcp_spec is not None and _mcp_spec.loader is not None
_mcp_spec.loader.exec_module(mcp_sse)


def test_update_conversation_summary_mcp_sanitizes_before_write(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        mcp_sse.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {"id": conversation_id},
    )

    def fake_update_conversation(uid, conversation_id, update_data):
        captured["uid"] = uid
        captured["conversation_id"] = conversation_id
        captured["update_data"] = update_data

    monkeypatch.setattr(mcp_sse.conversations_db, "update_conversation", fake_update_conversation)

    result = mcp_sse.execute_tool(
        "user-123",
        "update_conversation_summary",
        {
            "conversation_id": "conv-123",
            "overview": "Useful ambient summary with enough detail to safely replace the prior text.",
            "category": "technology",
        },
    )

    assert result["success"] is True
    assert result["sanitizer_warnings"] == ["overview_missing_ella_prefix"]
    assert captured["uid"] == "user-123"
    assert captured["conversation_id"] == "conv-123"
    assert captured["update_data"]["structured.overview"].startswith("[Ella] ")
    assert captured["update_data"]["structured.category"] == "technology"
    assert captured["update_data"]["apps_results"] == []
    assert captured["update_data"]["plugins_results"] == []


def test_update_conversation_summary_mcp_rejects_debug_jargon(monkeypatch):
    monkeypatch.setattr(
        mcp_sse.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {"id": conversation_id},
    )
    monkeypatch.setattr(mcp_sse.conversations_db, "update_conversation", MagicMock())

    with pytest.raises(mcp_sse.ToolExecutionError) as excinfo:
        mcp_sse.execute_tool(
            "user-123",
            "update_conversation_summary",
            {
                "conversation_id": "conv-123",
                "overview": (
                    "[Ella] The transcript mostly covered device testing and family conversation. "
                    "The qmd routing trace selected a write-back retry."
                ),
            },
        )

    assert excinfo.value.code == -32602
    assert "Unsafe conversation summary" in excinfo.value.message
    mcp_sse.conversations_db.update_conversation.assert_not_called()
