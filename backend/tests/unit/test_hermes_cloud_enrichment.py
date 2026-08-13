import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ella.services import hermes_cloud_enrichment
from ella.services.hermes_cloud_enrichment import HermesCloudEnrichmentService
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime


def _runtime() -> IsolatedRuntime:
    return IsolatedRuntime(
        uid="synthetic-user",
        binding_id="binding-a",
        provider="hermes_cloud",
        status="internal_canary",
        profile_name="synthetic-profile",
        agent_id="hermes-cloud",
        runtime_instance_id="instance-a",
        gateway_url="https://cloud.example.test",
        gateway_token="secret",
        workspace_root="",
        honcho_workspace="workspace-a",
        observed_peer="user-a",
        observer_peer="companion-a",
        prompt_pack_version="prompt-v1",
        expected_model="model-a",
        model_context_window_tokens=16384,
        allowed_tools=(),
        required_capabilities=("responses_api",),
        model_policy_version="models-v1",
        voice_policy_version="voice-v1",
        revision=1,
    )


def _conversation(text: str = "Synthetic cafe order.") -> dict:
    return {
        "id": "conversation-a",
        "started_at": "2026-07-27T10:00:00Z",
        "transcript_segments": [
            {"is_user": True, "speaker": "SPEAKER_0", "text": text},
        ],
        "structured": {
            "title": "Initial title",
            "overview": "Initial summary",
            "emoji": "📝",
            "category": "other",
        },
        "active_summary_version_id": "version-a",
        "enrichment_state": {},
    }


def test_enrichment_identity_is_scoped_to_active_summary_version():
    first = _conversation()
    second = deepcopy(first)
    second["active_summary_version_id"] = "version-b"

    first_identity = hermes_cloud_enrichment.build_enrichment_identity(
        uid="synthetic-user",
        conversation_id=first["id"],
        conversation=first,
    )
    second_identity = hermes_cloud_enrichment.build_enrichment_identity(
        uid="synthetic-user",
        conversation_id=second["id"],
        conversation=second,
    )

    assert first_identity.transcript_sha256 == second_identity.transcript_sha256
    assert first_identity.client_interaction_id != second_identity.client_interaction_id
    assert first_identity.job_id != second_identity.job_id


def test_enrichment_resolves_exact_published_transcript_target(monkeypatch):
    repository = SimpleNamespace()
    requested = []

    async def resolve(uid, *, repository=None, target_mode=None):
        requested.append((uid, repository, target_mode))
        return _runtime()

    monkeypatch.setattr(hermes_cloud_enrichment, "resolve_isolated_runtime", resolve)
    service = HermesCloudEnrichmentService(
        repository=repository,
        event_store=SimpleNamespace(),
        conversation_reader=lambda uid, conversation_id: None,
        summary_applier=AsyncMock(),
    )

    runtime = asyncio.run(service._runtime("synthetic-user", allow_shadow=False))

    assert runtime.provider == "hermes_cloud"
    assert requested == [
        (
            "synthetic-user",
            repository,
            "hermes-cloud-transcript",
        )
    ]


class FakeRuntimeService:
    def __init__(self):
        self.calls = []

    async def run_turn(self, runtime, request, *, response_validator=None):
        self.calls.append((runtime, request))
        payload = (
            {
                "grounding": {
                    "outcome": "supported",
                    "supporting_quotes": ["Synthetic cafe order."],
                }
            }
            if request.channel == hermes_cloud_enrichment.HERMES_CLOUD_GROUNDING_CHANNEL
            else {
                "title": "Synthetic cafe order",
                "overview": "[Ella] A synthetic cafe order was discussed.",
                "emoji": "☕",
                "category": "social",
                "ella_tags": ["omi", "enriched"],
                "ella_signal": {
                    "salience": "low",
                    "memory_promotion": "none",
                    "noise_level": "none",
                    "contains_media": False,
                    "contains_user_speech": True,
                    "guardian_relevant": False,
                },
            }
        )
        text = json.dumps(payload)
        if response_validator:
            response_validator(text)
        return SimpleNamespace(
            text=text,
            response_id=f"response-{request.channel}",
            canonical_user_event_id=f"event-user-{request.channel}",
            canonical_assistant_event_id=f"event-assistant-{request.channel}",
            duplicate=False,
            usage={"input_tokens": 10, "output_tokens": 5},
            runtime_interaction_id=f"interaction-{request.channel}",
        )


def test_enrichment_uses_exact_owned_transcript_and_confirmed_writeback():
    conversation = _conversation()
    runtime_service = FakeRuntimeService()
    summary_applier = AsyncMock(
        return_value={
            "active_summary_version_id": "version-enriched",
            "canonical_confirmed": True,
        }
    )
    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: runtime_service,
        conversation_reader=lambda uid, conversation_id: deepcopy(conversation),
        summary_applier=summary_applier,
    )
    service._runtime = AsyncMock(return_value=_runtime())

    result = asyncio.run(
        service.enrich(
            uid="synthetic-user",
            conversation_id="conversation-a",
        )
    )

    runtime, request = runtime_service.calls[0]
    _, verifier_request = runtime_service.calls[1]
    provider_input = json.loads(request.user_input)
    assert runtime.provider == "hermes_cloud"
    assert request.channel == "omi_enrichment"
    assert request.user_scan_policy == "none"
    assert request.client_metadata["policy_version"] == "hermes-cloud-enrichment-v1"
    assert provider_input["transcript_segments"] == conversation["transcript_segments"]
    assert provider_input["conversation_id_sha256"] != conversation["id"]
    assert "conversation-a" not in request.user_input
    verifier_input = json.loads(verifier_request.user_input)
    assert verifier_request.channel == hermes_cloud_enrichment.HERMES_CLOUD_GROUNDING_CHANNEL
    assert verifier_request.client_interaction_id != request.client_interaction_id
    assert verifier_input["transcript_segments"] == conversation["transcript_segments"]
    assert verifier_input["candidate_summary"]["title"] == "Synthetic cafe order"
    assert "conversation-a" not in verifier_request.user_input
    assert summary_applier.await_args.kwargs["require_canonical"] is True
    assert summary_applier.await_args.kwargs["summary_source"] == "hermes_cloud"
    grounding = summary_applier.await_args.kwargs["today_card_grounding"]
    assert grounding["semantic_outcome"] == "supported"
    assert grounding["owner_hash"].startswith("sha256:")
    assert grounding["conversation_id_hash"].startswith("sha256:")
    assert grounding["runtime_interaction_id"] == "interaction-omi_enrichment"
    assert grounding["canonical_assistant_event_id"] == "event-assistant-omi_enrichment"
    assert grounding["verifier_runtime_interaction_id"] == "interaction-omi_enrichment_grounding_verifier"
    assert grounding["verifier_canonical_assistant_event_id"] == "event-assistant-omi_enrichment_grounding_verifier"
    assert result.active_summary_version_id == "version-enriched"
    assert result.provider_response_present is True
    assert result.client_interaction_id == request.client_interaction_id


def test_enrichment_rejects_transcript_change_before_writeback():
    original = _conversation()
    changed = _conversation("Changed after provider execution.")
    reads = iter([deepcopy(original), deepcopy(changed)])
    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: FakeRuntimeService(),
        conversation_reader=lambda uid, conversation_id: next(reads),
        summary_applier=AsyncMock(),
    )
    service._runtime = AsyncMock(return_value=_runtime())

    with pytest.raises(
        ProvisioningError,
        match="hermes_cloud_enrichment_transcript_changed",
    ):
        asyncio.run(
            service.enrich(
                uid="synthetic-user",
                conversation_id="conversation-a",
            )
        )

    service.summary_applier.assert_not_awaited()


@pytest.mark.parametrize(
    "overview",
    [
        "[Ella] La grabación era demasiado corta para resumir lo ocurrido.",
        "[Ella] 录音太短，无法总结发生了什么。",
    ],
)
def test_enrichment_semantic_insufficiency_cannot_write_summary_or_attestation(overview):
    conversation = _conversation(
        "We met after lunch and planned the garden beds for next spring with three neighbors joining us."
    )

    class InsufficientRuntime(FakeRuntimeService):
        async def run_turn(self, runtime, request, *, response_validator=None):
            self.calls.append((runtime, request))
            payload = (
                {"grounding": {"outcome": "insufficient", "supporting_quotes": []}}
                if request.channel == hermes_cloud_enrichment.HERMES_CLOUD_GROUNDING_CHANNEL
                else {
                    "title": "A captured moment",
                    "overview": overview,
                    "category": "other",
                }
            )
            text = json.dumps(payload)
            if response_validator:
                response_validator(text)
            return SimpleNamespace(
                text=text,
                response_id="response-insufficient",
                canonical_user_event_id="event-user-insufficient",
                canonical_assistant_event_id="event-assistant-insufficient",
                duplicate=False,
                usage={},
                runtime_interaction_id="interaction-insufficient",
            )

    summary_applier = AsyncMock(
        return_value={"active_summary_version_id": "version-enriched", "canonical_confirmed": True}
    )
    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: InsufficientRuntime(),
        conversation_reader=lambda uid, conversation_id: deepcopy(conversation),
        summary_applier=summary_applier,
    )
    service._runtime = AsyncMock(return_value=_runtime())

    with pytest.raises(
        ProvisioningError,
        match="hermes_cloud_enrichment_insufficient_grounding",
    ):
        asyncio.run(service.enrich(uid="synthetic-user", conversation_id="conversation-a"))

    summary_applier.assert_not_awaited()


def test_enrichment_rejects_supported_attestation_with_quote_from_another_transcript():
    conversation = _conversation("We planted tomatoes together in the garden after lunch.")

    class MismatchedQuoteRuntime(FakeRuntimeService):
        async def run_turn(self, runtime, request, *, response_validator=None):
            payload = (
                {
                    "grounding": {
                        "outcome": "supported",
                        "supporting_quotes": ["I won the lottery and bought a red sailboat."],
                    }
                }
                if request.channel == hermes_cloud_enrichment.HERMES_CLOUD_GROUNDING_CHANNEL
                else {
                    "title": "A lottery win",
                    "overview": "[Ella] You won the lottery and bought a red sailboat.",
                    "category": "other",
                }
            )
            text = json.dumps(payload)
            response_validator(text)
            return SimpleNamespace(
                text=text,
                response_id=f"response-{request.channel}",
                canonical_user_event_id=f"user-{request.channel}",
                canonical_assistant_event_id=f"assistant-{request.channel}",
                duplicate=False,
                usage={},
                runtime_interaction_id=f"interaction-{request.channel}",
            )

    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: MismatchedQuoteRuntime(),
        conversation_reader=lambda uid, conversation_id: deepcopy(conversation),
        summary_applier=AsyncMock(),
    )
    service._runtime = AsyncMock(return_value=_runtime())

    with pytest.raises(ValueError, match="not present in the authoritative transcript"):
        asyncio.run(service.enrich(uid="synthetic-user", conversation_id="conversation-a"))

    service.summary_applier.assert_not_awaited()


def test_enrichment_ignores_self_attestation_and_requires_independent_verifier():
    conversation = _conversation("We planted tomatoes together in the garden after lunch.")

    class FabricatedSummaryRuntime(FakeRuntimeService):
        async def run_turn(self, runtime, request, *, response_validator=None):
            self.calls.append((runtime, request))
            if request.channel == hermes_cloud_enrichment.HERMES_CLOUD_GROUNDING_CHANNEL:
                payload = {"grounding": {"outcome": "insufficient", "supporting_quotes": []}}
            else:
                payload = {
                    "title": "A lottery win",
                    "overview": "[Ella] You won the lottery and bought a red sailboat.",
                    "category": "other",
                    "grounding": {
                        "outcome": "supported",
                        "supporting_quotes": ["We planted tomatoes together"],
                    },
                }
            text = json.dumps(payload)
            if response_validator:
                response_validator(text)
            return SimpleNamespace(
                text=text,
                response_id=f"response-{request.channel}",
                canonical_user_event_id=f"user-{request.channel}",
                canonical_assistant_event_id=f"assistant-{request.channel}",
                duplicate=False,
                usage={},
                runtime_interaction_id=f"interaction-{request.channel}",
            )

    summary_applier = AsyncMock(
        return_value={"active_summary_version_id": "version-enriched", "canonical_confirmed": True}
    )
    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: FabricatedSummaryRuntime(),
        conversation_reader=lambda uid, conversation_id: deepcopy(conversation),
        summary_applier=summary_applier,
    )
    service._runtime = AsyncMock(return_value=_runtime())

    with pytest.raises(
        ProvisioningError,
        match="hermes_cloud_enrichment_insufficient_grounding",
    ):
        asyncio.run(service.enrich(uid="synthetic-user", conversation_id="conversation-a"))

    summary_applier.assert_not_awaited()


def test_enrichment_requires_confirmed_canonical_writeback():
    conversation = _conversation()
    service = HermesCloudEnrichmentService(
        repository=SimpleNamespace(),
        event_store=SimpleNamespace(),
        runtime_service_factory=lambda allow_shadow: FakeRuntimeService(),
        conversation_reader=lambda uid, conversation_id: deepcopy(conversation),
        summary_applier=AsyncMock(
            return_value={
                "active_summary_version_id": "version-enriched",
                "canonical_confirmed": False,
            }
        ),
    )
    service._runtime = AsyncMock(return_value=_runtime())

    with pytest.raises(
        ProvisioningError,
        match="hermes_cloud_enrichment_writeback_unconfirmed",
    ):
        asyncio.run(
            service.enrich(
                uid="synthetic-user",
                conversation_id="conversation-a",
            )
        )
