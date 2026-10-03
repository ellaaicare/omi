import json
import hashlib
import logging
from datetime import datetime, timezone

import pytest
import requests

from utils.ella import canonical_omi
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


def _transport_fixture():
    return {
        "id": "private-fixture-event",
        "created_at": "2026-10-03T01:00:00Z",
        "structured": {"title": "Private fixture title", "overview": "Private fixture text"},
    }


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (requests.ConnectTimeout, "connect_timeout"),
        (requests.ReadTimeout, "read_timeout"),
        (requests.Timeout, "timeout_unknown"),
        (TimeoutError, "timeout_unknown"),
        (requests.ConnectionError, "connection_error"),
        (RuntimeError, "unexpected_error"),
    ],
)
def test_canonical_transport_diagnostic_failure_is_private_and_unconfirmed(monkeypatch, caplog, error_type, category):
    calls = []
    failure = error_type("private-fixture-error-token-url-body")

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure

    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", True)
    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_TIMEOUT", 5)
    monkeypatch.setattr(canonical_omi, "canonical_event_service_headers", lambda uid: {"Private": "fixture-secret"})
    monkeypatch.setattr(canonical_omi.requests, "post", post)
    ticks = iter((10.0, 11.25))
    monkeypatch.setattr(canonical_omi.time, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    with pytest.raises(error_type) as observed:
        canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture())

    assert observed.value is failure
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 5
    records = [record for record in caplog.records if record.getMessage() == "canonical_omi_http_result"]
    assert len(records) == 1
    record = records[0]
    assert record.stage == "request"
    assert record.outcome == "unconfirmed"
    assert record.error_category == category
    assert record.elapsed_ms == 1250
    assert record.http_status is None
    assert record.exc_info is None
    assert "private-fixture" not in repr(record.__dict__)
    assert "fixture-secret" not in repr(record.__dict__)


@pytest.mark.parametrize("timeout", [None, 0.25])
def test_canonical_transport_diagnostic_response_is_not_a_commit_receipt(monkeypatch, caplog, timeout):
    calls = []
    payload = {"ok": False}

    class Response:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", True)
    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_TIMEOUT", 5)
    monkeypatch.setattr(canonical_omi, "canonical_event_service_headers", lambda uid: {})
    monkeypatch.setattr(canonical_omi.requests, "post", lambda *args, **kwargs: calls.append(kwargs) or Response())
    ticks = iter((10.0, 10.5))
    monkeypatch.setattr(canonical_omi.time, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    assert canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture(), timeout=timeout) == {
        "ok": False,
        "latency_ms": 500,
    }
    assert len(calls) == 1
    assert calls[0]["timeout"] == (5 if timeout is None else timeout)
    record = next(record for record in caplog.records if record.getMessage() == "canonical_omi_http_result")
    assert record.outcome == "response_received"
    assert record.stage == "response_json"
    assert record.error_category == "none"
    assert record.http_status == 200
    assert record.elapsed_ms == 500


@pytest.mark.parametrize(("end_time", "elapsed_ms"), [(1e9, 86400000), (-1.0, 0)])
def test_canonical_transport_diagnostic_invalid_response_is_private_and_bounded(
    monkeypatch, caplog, end_time, elapsed_ms
):
    class Response:
        status_code = 200

        def json(self):
            raise ValueError("private-fixture-response-body")

    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", True)
    monkeypatch.setattr(canonical_omi, "canonical_event_service_headers", lambda uid: {})
    monkeypatch.setattr(canonical_omi.requests, "post", lambda *args, **kwargs: Response())
    ticks = iter((0.0, end_time))
    monkeypatch.setattr(canonical_omi.time, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    with pytest.raises(ValueError, match="private-fixture-response-body"):
        canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture())

    record = next(record for record in caplog.records if record.getMessage() == "canonical_omi_http_result")
    assert record.stage == "response_json"
    assert record.outcome == "unconfirmed"
    assert record.error_category == "unexpected_error"
    assert record.http_status == 200
    assert record.elapsed_ms == elapsed_ms
    assert "private-fixture" not in repr(record.__dict__)


@pytest.mark.parametrize("status", [403, 500])
def test_canonical_transport_diagnostic_http_failure_does_not_log_body(monkeypatch, caplog, status):
    class Response:
        status_code = status
        text = "private-fixture-response-body"

    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", True)
    monkeypatch.setattr(canonical_omi, "canonical_event_service_headers", lambda uid: {})
    monkeypatch.setattr(canonical_omi.requests, "post", lambda *args, **kwargs: Response())
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    with pytest.raises(RuntimeError, match=f"canonical_events_http_{status}"):
        canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture())

    record = next(record for record in caplog.records if record.getMessage() == "canonical_omi_http_result")
    assert record.stage == "response_status"
    assert record.outcome == "unconfirmed"
    assert record.http_status == status
    assert record.error_category == "unexpected_error"
    assert "private-fixture" not in repr(record.__dict__)


@pytest.mark.parametrize("remote_committed", [False, True])
def test_canonical_transport_timeout_never_infers_remote_commit_or_retries(monkeypatch, caplog, remote_committed):
    calls = []
    remote_state = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        if remote_committed:
            remote_state.append("synthetic_commit")
        raise TimeoutError("synthetic response lost")

    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", True)
    monkeypatch.setattr(canonical_omi, "canonical_event_service_headers", lambda uid: {})
    monkeypatch.setattr(canonical_omi.requests, "post", post)
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    with pytest.raises(TimeoutError):
        canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture())

    # This models transport uncertainty only, not canonical store idempotency.
    assert len(calls) == 1
    assert bool(remote_state) is remote_committed
    record = next(record for record in caplog.records if record.getMessage() == "canonical_omi_http_result")
    assert record.outcome == "unconfirmed"
    assert record.error_category == "timeout_unknown"
    assert record.http_status is None


def test_canonical_transport_diagnostic_disabled_path_has_no_http_or_log(monkeypatch, caplog):
    monkeypatch.setattr(canonical_omi, "CANONICAL_OMI_WRITE_ENABLED", False)
    monkeypatch.setattr(canonical_omi.requests, "post", lambda *args, **kwargs: pytest.fail("disabled HTTP"))
    caplog.set_level(logging.INFO, logger="utils.ella.canonical_omi")

    assert canonical_omi.write_omi_canonical_event("private-fixture-owner", _transport_fixture()) == {
        "ok": False,
        "skipped": True,
        "reason": "disabled",
    }
    assert not [record for record in caplog.records if record.name == "utils.ella.canonical_omi"]
