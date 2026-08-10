import json
import hashlib
from datetime import datetime, timezone

from utils.ella.canonical_omi import (
    TODAY_CARD_GROUNDING_ATTESTER,
    TODAY_CARD_GROUNDING_CONTRACT_VERSION,
    build_omi_canonical_event,
    summary_grounding_hash,
    transcript_grounding_hash,
)


def _semantic_receipt(conversation, source_version_id, uid="uid-123"):
    return {
        "contract_version": TODAY_CARD_GROUNDING_CONTRACT_VERSION,
        "attester": TODAY_CARD_GROUNDING_ATTESTER,
        "semantic_outcome": "supported",
        "source_version_id": source_version_id,
        "transcript_hash": transcript_grounding_hash(conversation["transcript_segments"]),
        "summary_hash": summary_grounding_hash(conversation["structured"]),
        "supporting_quote_hashes": ["sha256:" + ("a" * 64)],
        "policy_version": "hermes-cloud-grounding-verifier-v1",
        "owner_hash": "sha256:" + hashlib.sha256(uid.encode("utf-8")).hexdigest(),
        "conversation_id_hash": "sha256:" + hashlib.sha256(conversation["id"].encode("utf-8")).hexdigest(),
        "runtime_interaction_id": "runtime-interaction-a",
        "canonical_assistant_event_id": "canonical-assistant-a",
        "verifier_runtime_interaction_id": "verifier-runtime-a",
        "verifier_canonical_assistant_event_id": "verifier-assistant-a",
    }


def _parallel_semantic_receipt(conversation, source_version_id, uid="uid-123"):
    receipt = _semantic_receipt(conversation, source_version_id, uid)
    for key in (
        "runtime_interaction_id",
        "canonical_assistant_event_id",
        "verifier_runtime_interaction_id",
        "verifier_canonical_assistant_event_id",
    ):
        receipt.pop(key)
    receipt.update(
        {
            "attester": "hermes_parallel_grounding_verifier",
            "policy_version": "hermes-parallel-grounding-verifier-v1",
            "summary_request_id": "summary-request-a",
            "summary_response_id": "summary-response-a",
            "verifier_request_id": "verifier-request-a",
            "verifier_response_id": "verifier-response-a",
        }
    )
    return receipt


def test_transcript_grounding_hash_matches_parallel_runtime_contract():
    assert (
        transcript_grounding_hash([{"text": "I ordered a waffle with oat milk after our morning walk."}])
        == "sha256:504ea992fe2ddf25098ea54cc133e1bcaccd2e3cf65af79d3b9fab879b916c96"
    )


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
    assert grounding == {}


def test_build_omi_canonical_event_emits_bound_semantic_grounding_provenance():
    base = {
        "id": "grounded-123",
        "started_at": datetime(2026, 5, 7, 18, 56, 59, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 5, 7, 18, 57, 20, tzinfo=timezone.utc),
        "structured": {"title": "A grounded visit", "overview": "A meaningful visit in the garden."},
        "active_summary_version_id": "obs-v3",
        "summary_versions": [
            {
                "id": "obs-v3",
                "title": "A grounded visit",
                "overview": "A meaningful visit in the garden.",
                "source": "hermes_cloud",
                "kind": "hermes_enriched",
            }
        ],
    }
    english = dict(base)
    english["transcript_segments"] = [
        {
            "text": "We planted tomatoes together and planned another garden visit for next Sunday morning.",
            "timestamp": datetime(2026, 5, 7, 18, 57, 12, tzinfo=timezone.utc),
        }
    ]
    english["enrichment_state"] = {"today_card_grounding": _semantic_receipt(english, "obs-v3")}
    japanese = dict(base)
    japanese["id"] = "grounded-ja"
    japanese["transcript_segments"] = [{"text": "今日は母と一緒に庭でトマトを植えて来週また会う約束をしました"}]
    japanese["enrichment_state"] = {"today_card_grounding": _semantic_receipt(japanese, "obs-v3")}

    english_event = build_omi_canonical_event("uid-123", english)
    japanese_event = build_omi_canonical_event("uid-123", japanese)

    english_grounding = english_event["metadata"]["today_card"]["grounding"]
    japanese_grounding = japanese_event["metadata"]["today_card"]["grounding"]
    assert english_grounding["semantic_outcome"] == "supported"
    assert japanese_grounding["semantic_outcome"] == "supported"
    assert english_grounding["source_version_id"] == "obs-v3"
    assert japanese_grounding["source_version_id"] == "obs-v3"
    assert english_grounding["transcript_hash"] == transcript_grounding_hash(
        english_event["metadata"]["transcript_segments"]
    )


def test_build_omi_canonical_event_preserves_parallel_verifier_receipt():
    conversation = {
        "id": "grounded-parallel",
        "started_at": datetime(2026, 5, 7, 18, 56, 59, tzinfo=timezone.utc),
        "structured": {"title": "A grounded visit", "overview": "A meaningful visit in the garden."},
        "active_summary_version_id": "parallel-v1",
        "summary_versions": [
            {
                "id": "parallel-v1",
                "title": "A grounded visit",
                "overview": "A meaningful visit in the garden.",
                "source": "hermes_parallel",
                "kind": "hermes_enriched",
            }
        ],
        "transcript_segments": [{"text": "We planted tomatoes together in the garden after lunch."}],
    }
    conversation["enrichment_state"] = {"today_card_grounding": _parallel_semantic_receipt(conversation, "parallel-v1")}

    event = build_omi_canonical_event("uid-123", conversation)

    grounding = event["metadata"]["today_card"]["grounding"]
    assert grounding["attester"] == "hermes_parallel_grounding_verifier"
    assert grounding["source_version_id"] == "parallel-v1"
    assert grounding["verifier_request_id"] == "verifier-request-a"


def test_build_omi_canonical_event_rejects_reused_parallel_verifier_identity():
    conversation = {
        "id": "grounded-parallel-reused",
        "structured": {"title": "Garden visit", "overview": "We planted tomatoes in the garden."},
        "active_summary_version_id": "parallel-v1",
        "summary_versions": [
            {
                "id": "parallel-v1",
                "title": "Garden visit",
                "overview": "We planted tomatoes in the garden.",
                "source": "hermes_parallel",
                "kind": "hermes_enriched",
            }
        ],
        "transcript_segments": [{"text": "We planted tomatoes in the garden after lunch."}],
    }
    receipt = _parallel_semantic_receipt(conversation, "parallel-v1")
    receipt["verifier_request_id"] = receipt["summary_request_id"]
    receipt["verifier_response_id"] = receipt["summary_response_id"]
    conversation["enrichment_state"] = {"today_card_grounding": receipt}

    event = build_omi_canonical_event("uid-123", conversation)

    assert event["metadata"]["today_card"]["grounding"] == {}


def test_build_omi_canonical_event_rejects_any_cross_call_identity_reuse():
    conversation = {
        "id": "grounded-parallel-cross-reuse",
        "structured": {"title": "Garden visit", "overview": "We planted tomatoes in the garden."},
        "active_summary_version_id": "parallel-v1",
        "summary_versions": [
            {
                "id": "parallel-v1",
                "title": "Garden visit",
                "overview": "We planted tomatoes in the garden.",
                "source": "hermes_parallel",
                "kind": "hermes_enriched",
            }
        ],
        "transcript_segments": [{"text": "We planted tomatoes in the garden after lunch."}],
    }
    receipt = _parallel_semantic_receipt(conversation, "parallel-v1")
    receipt["verifier_request_id"] = receipt["summary_response_id"]
    conversation["enrichment_state"] = {"today_card_grounding": receipt}

    event = build_omi_canonical_event("uid-123", conversation)

    assert event["metadata"]["today_card"]["grounding"] == {}


def test_build_omi_canonical_event_rejects_forged_or_stale_semantic_grounding():
    conversation = {
        "id": "grounded-stale",
        "structured": {"title": "Garden visit", "overview": "We planted tomatoes in the garden."},
        "active_summary_version_id": "summary-v2",
        "summary_versions": [
            {
                "id": "summary-v2",
                "title": "Garden visit",
                "overview": "We planted tomatoes in the garden.",
                "source": "hermes_cloud",
                "kind": "hermes_enriched",
            }
        ],
        "transcript_segments": [{"text": "We planted tomatoes in the garden after lunch."}],
    }
    receipt = _semantic_receipt(conversation, "summary-v2")
    receipt["summary_hash"] = "sha256:" + ("0" * 64)
    conversation["enrichment_state"] = {"today_card_grounding": receipt}

    event = build_omi_canonical_event("uid-123", conversation)

    assert event["metadata"]["today_card"]["grounding"] == {}


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
