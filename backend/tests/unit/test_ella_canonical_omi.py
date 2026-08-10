import json
from datetime import datetime, timezone

from utils.ella.canonical_omi import build_omi_canonical_event


def test_build_omi_canonical_event_preserves_enriched_summary_and_transcript():
    conversation = {
        "id": "cafe-123",
        "created_at": datetime(2026, 5, 7, 18, 56, 59, tzinfo=timezone.utc),
        "started_at": datetime(2026, 5, 7, 18, 56, 59, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 5, 7, 18, 58, 12, tzinfo=timezone.utc),
        "structured": {
            "title": "Cafe Coffee and Waffle Stop",
            "overview": "[Ella] You ordered a noah drink and a waffle with oat.",
            "emoji": "☕",
            "category": "other",
        },
        "summary_versions": [{"id": "obs-v2", "source": "observer", "kind": "observer_enriched"}],
        "active_summary_version_id": "obs-v2",
        "enrichment_state": {"status": "writeback_applied"},
        "transcript_segments": [
            {
                "id": "seg-1",
                "is_user": True,
                "text": "Can I get the waffle?",
                "start": 0.0,
                "end": 1.5,
            }
        ],
    }

    event = build_omi_canonical_event(
        "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
        conversation,
        summary_source="observer",
        summary_kind="observer_enriched",
        trace_id="trace-cafe",
    )

    assert event["event_id"] == "omi:cafe-123:summary"
    assert event["source_ref"]["source_identity"] == "omi:cafe-123"
    assert event["channel"] == "omi"
    assert event["provider"] == "omi-backend"
    assert event["started_at"] == "2026-05-07T18:56:59Z"
    assert event["ended_at"] == "2026-05-07T18:58:12Z"
    assert "Cafe Coffee and Waffle Stop" in event["text"]
    assert "waffle with oat" in event["text"]
    assert event["metadata"]["summary_versions"][0]["id"] == "obs-v2"
    assert event["metadata"]["active_summary_version_id"] == "obs-v2"
    assert event["metadata"]["transcript_segments"][0]["text"] == "Can I get the waffle?"
    assert event["metadata"]["trace_id"] == "trace-cafe"
    grounding = event["metadata"]["today_card"]["grounding"]
    assert grounding["contract_version"] == "ella.today_card.grounding.v1"
    assert grounding["source_version_id"] == "obs-v2"
    assert grounding["grounded_content"] is False
    assert grounding["transcript_hash"].startswith("sha256:")


def test_build_omi_canonical_event_emits_language_independent_grounding_provenance():
    base = {
        "id": "grounded-123",
        "started_at": datetime(2026, 5, 7, 18, 56, 59, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 5, 7, 18, 57, 20, tzinfo=timezone.utc),
        "structured": {"title": "A grounded visit", "overview": "A meaningful visit in the garden."},
        "active_summary_version_id": "obs-v3",
        "summary_versions": [{"id": "obs-v3"}],
    }
    english = dict(base)
    english["transcript_segments"] = [
        {"text": "We planted tomatoes together and planned another garden visit for next Sunday morning."}
    ]
    japanese = dict(base)
    japanese["id"] = "grounded-ja"
    japanese["transcript_segments"] = [{"text": "今日は母と一緒に庭でトマトを植えて来週また会う約束をしました"}]

    english_event = build_omi_canonical_event("uid-123", english)
    japanese_event = build_omi_canonical_event("uid-123", japanese)

    english_grounding = english_event["metadata"]["today_card"]["grounding"]
    japanese_grounding = japanese_event["metadata"]["today_card"]["grounding"]
    assert english_grounding["grounded_content"] is True
    assert english_grounding["transcript_word_count"] >= 12
    assert japanese_grounding["grounded_content"] is True
    assert japanese_grounding["transcript_non_ascii_alphanumeric_count"] >= 12
    assert english_grounding["capture_duration_seconds"] == 21.0
    assert japanese_grounding["capture_duration_seconds"] == 21.0


def test_build_omi_canonical_event_json_normalizes_nested_timestamps():
    nested_time = datetime(2026, 5, 7, 18, 57, 12, tzinfo=timezone.utc)
    conversation = {
        "id": "nested-123",
        "created_at": nested_time,
        "started_at": nested_time,
        "structured": {
            "title": "Nested timestamp test",
            "overview": "A summary with nested metadata.",
            "events": [{"observed_at": nested_time}],
        },
        "summary_versions": [{"id": "obs-v2", "created_at": nested_time}],
        "transcript_segments": [{"id": "seg-1", "text": "Hello", "timestamp": nested_time}],
    }

    event = build_omi_canonical_event("uid-123", conversation)

    json.dumps(event)
    assert event["metadata"]["structured"]["events"][0]["observed_at"] == "2026-05-07T18:57:12Z"
    assert event["metadata"]["summary_versions"][0]["created_at"] == "2026-05-07T18:57:12Z"
    assert event["metadata"]["transcript_segments"][0]["timestamp"] == "2026-05-07T18:57:12Z"
