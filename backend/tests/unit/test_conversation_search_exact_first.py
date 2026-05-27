from datetime import datetime, timezone
from pathlib import Path
import re
import types


def _load_search_helpers():
    path = Path(__file__).resolve().parents[2] / "utils" / "retrieval" / "tools" / "conversation_tools.py"
    source = path.read_text()
    start = source.index("_SEARCH_STOPWORDS =")
    end = source.index("\n\n@tool", start)
    module = types.ModuleType("conversation_search_helpers")
    module.datetime = datetime
    module.timezone = timezone
    module.re = re
    exec(source[start:end], module.__dict__)
    return module


def test_exact_conversation_search_finds_summary_without_vector():
    module = _load_search_helpers()
    conversations = [
        {
            "id": "older",
            "created_at": datetime(2026, 5, 7, 16, 0, tzinfo=timezone.utc),
            "structured": {"title": "Morning errand", "overview": "Stopped for groceries."},
            "transcript_segments": [],
        },
        {
            "id": "cafe",
            "created_at": datetime(2026, 5, 7, 18, 56, tzinfo=timezone.utc),
            "structured": {
                "title": "Cafe Coffee and Waffle Stop",
                "overview": "Ordered a noah drink and a waffle with oat.",
            },
            "transcript_segments": [],
        },
    ]

    assert module._rank_exact_conversation_ids(conversations, "Cafe Coffee and Waffle Stop", 5) == ["cafe"]


def test_exact_conversation_search_checks_transcript_terms():
    module = _load_search_helpers()
    conversations = [
        {
            "id": "keys",
            "created_at": datetime(2026, 5, 8, 18, 56, tzinfo=timezone.utc),
            "structured": {"title": "Home conversation", "overview": ""},
            "transcript_segments": [{"text": "I put the keys in the backpack near the door."}],
        }
    ]

    assert module._rank_exact_conversation_ids(conversations, "keys backpack", 5) == ["keys"]


def test_merge_ranked_ids_prefers_exact_then_vector_without_duplicates():
    module = _load_search_helpers()

    assert module._merge_ranked_ids(["exact-a", "shared"], ["shared", "vector-b"], 3) == [
        "exact-a",
        "shared",
        "vector-b",
    ]


def test_date_only_exact_search_returns_window_conversations():
    module = _load_search_helpers()
    conversations = [
        {
            "id": "morning-a",
            "created_at": datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc),
            "structured": {"title": "Quick check-in", "overview": "No special keywords."},
            "transcript_segments": [],
        }
    ]

    assert module._rank_exact_conversation_ids(
        conversations,
        "what happened",
        5,
        allow_date_only=True,
    ) == ["morning-a"]
    assert module._rank_exact_conversation_ids(conversations, "what happened", 5) == []
