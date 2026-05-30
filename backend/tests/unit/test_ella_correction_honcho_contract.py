import asyncio
from datetime import datetime, timezone
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))

from ella.services import correction_honcho_contract as contract


def _conversation(conversation_id="conv-1", uid="user-123", version="v1"):
    return {
        "id": conversation_id,
        "uid": uid,
        "created_at": datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc),
        "active_summary_version_id": version,
        "structured": {
            "title": "Teacher conversation",
            "overview": "[Ella] The summary called Mei Xin a teen.",
            "category": "education",
        },
    }


def _candidate(**overrides):
    candidate = contract.build_honcho_fact_candidate(
        uid="user-123",
        correction_id="corr-123",
        trace_id="trace-123",
        correction_text="Actually the speaker is Mei Xin, also called Rain.",
        correction_type="identity",
        source_conversation=_conversation("source"),
        related_conversation=_conversation("related"),
        confidence=0.96,
    )
    assert candidate is not None
    return (
        candidate.model_copy(update=overrides) if hasattr(candidate, "model_copy") else candidate.copy(update=overrides)
    )


def test_build_honcho_fact_candidate_uses_stable_session_key_and_idempotency():
    candidate = _candidate()

    assert candidate.uid == "user-123"
    assert candidate.source_conversation_id == "source"
    assert candidate.related_conversation_id == "related"
    assert candidate.entity_type == "person"
    assert candidate.session_key == "ella:omi:user-123:canonical"
    assert candidate.idempotency_key.startswith("omi-correction-honcho-fact:user-123:corr-123:")
    assert candidate.durable_owner == "honcho/hermes"


def test_build_honcho_fact_candidate_rejects_cross_user_related_conversation():
    candidate = contract.build_honcho_fact_candidate(
        uid="user-123",
        correction_id="corr-123",
        trace_id="trace-123",
        correction_text="Actually the speaker is Mei Xin.",
        correction_type="identity",
        source_conversation=_conversation("source", uid="user-123"),
        related_conversation=_conversation("related", uid="user-999"),
        confidence=0.96,
    )

    assert candidate is None


def test_create_honcho_fact_candidate_proposal_is_idempotent_memory_note():
    calls = []
    candidate = _candidate()

    def fake_create_proposal(**kwargs):
        calls.append(kwargs)
        return {"created": True, "deduped": False, "proposal": {"proposal_id": "proposal-1"}}

    result = contract.create_honcho_fact_candidate_proposal(candidate, create_proposal=fake_create_proposal)

    assert result["proposal"]["proposal_id"] == "proposal-1"
    assert calls[0]["tool_name"] == contract.HONCHO_FACT_TOOL_NAME
    assert calls[0]["proposal_type"] == "memory_note"
    assert calls[0]["idempotency_key"] == candidate.idempotency_key
    assert calls[0]["session_claims"]["profile_uid"] == "user-123"
    assert calls[0]["payload"]["target"]["kind"] == "honcho_fact_candidate"
    assert calls[0]["payload"]["target"]["durable_owner"] == "honcho/hermes"
    assert calls[0]["payload"]["write_policy"] == "proposal_only"


def test_write_honcho_fact_candidate_defaults_to_no_durable_write(monkeypatch):
    candidate = _candidate()

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", False)
    decision = asyncio.run(contract.write_honcho_fact_candidate(candidate))

    assert decision.action == "skip"
    assert decision.reason == "durable_write_disabled"
    assert decision.session_key == "ella:omi:user-123:canonical"


def test_write_honcho_fact_candidate_rechecks_active_summary_version(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v2"),
            token="hermes-token",
        )
    )

    assert decision.action == "skip"
    assert decision.reason == "stale_active_summary_version"


def _fake_client_factory(response):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return response

    return FakeClient, calls


class FakeResponse:
    def __init__(self, *, status_code=200, content='{"status":"written","memory_id":"mem-123"}', text=None):
        self.status_code = status_code
        self._content = content
        self.text = text if text is not None else content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_write_honcho_fact_candidate_uses_hermes_session_key_and_requires_confirmation(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, calls = _fake_client_factory(
        FakeResponse(content='{"status":"written","memory_id":"honcho-memory-123","reason":"stored"}')
    )

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            gateway_url="https://hermes.test",
            model="test-model",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "written"
    assert decision.reason == "hermes_honcho_write_confirmed"
    assert decision.response_ref["memory_id"] == "honcho-memory-123"
    assert calls[0]["url"] == "https://hermes.test/v1/chat/completions"
    assert calls[0]["headers"]["X-Hermes-Session-Key"] == "ella:omi:user-123:canonical"
    assert calls[0]["headers"]["X-Idempotency-Key"] == candidate.idempotency_key
    assert calls[0]["json"]["model"] == "test-model"


def test_write_honcho_fact_candidate_treats_chatty_200_as_uncertain(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, _ = _fake_client_factory(FakeResponse(content="Okay, I saved it."))

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "uncertain"
    assert decision.reason == "malformed_confirmation_json"


def test_write_honcho_fact_candidate_treats_malformed_json_as_uncertain(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, _ = _fake_client_factory(FakeResponse(content='{"status":"written",'))

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "uncertain"
    assert decision.reason == "malformed_confirmation_json"


def test_write_honcho_fact_candidate_requires_durable_ref(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, _ = _fake_client_factory(FakeResponse(content='{"status":"written","reason":"ok"}'))

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "uncertain"
    assert decision.reason == "missing_durable_ref"


def test_write_honcho_fact_candidate_treats_refusal_as_uncertain(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, _ = _fake_client_factory(FakeResponse(content='{"status":"refused","reason":"policy"}'))

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "uncertain"
    assert decision.reason == "honcho_write_refused"


def test_write_honcho_fact_candidate_http_error_remains_error(monkeypatch):
    candidate = _candidate(active_summary_version_id="v1")
    fake_client, _ = _fake_client_factory(FakeResponse(status_code=502, content='{"error":"bad gateway"}'))

    monkeypatch.setattr(contract, "HONCHO_FACT_WRITE_ENABLED", True)
    decision = asyncio.run(
        contract.write_honcho_fact_candidate(
            candidate,
            current_conversation=_conversation("related", uid="user-123", version="v1"),
            token="hermes-token",
            http_client_factory=fake_client,
        )
    )

    assert decision.action == "error"
    assert decision.reason == "hermes_http_502"
