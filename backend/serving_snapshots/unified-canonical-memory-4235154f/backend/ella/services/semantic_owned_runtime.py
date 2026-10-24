"""Explicit internal Hermes Cloud bridge; no route, queue, APPLY or fallback.

Activation contract: an owner-scoped caller supplies a frozen nine-field binding,
its fresh authority reader, and existing repository/event store. Only an already
approved hermes-cloud-transcript target is supported. This does not hydrate main
companion files or support/replace the retained self-hosted runtime. Full verified
companion context must already be present in the producer's bound request.

The existing runtime writes its own canonical operation events in the existing
enrichment channels, not the personal chat channel. Both turns disable response
continuation. No model, credential, endpoint or deployment settings are changed.
Invoke only through the durable producer ledger, which reserves START first.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ella.services.semantic_producer_ledger import BackendTurn
from ella.services.semantic_publication import PublicationBinding, SemanticPublicationRejectedError, digest

TARGET_MODE = "hermes-cloud-transcript"
CHANNELS = {"generator": "omi_enrichment", "verifier": "omi_enrichment_grounding_verifier"}
MAX_REQUEST_BYTES = 196608


@dataclass(frozen=True)
class _RuntimeDependencies:
    service: Any
    resolver: Callable
    identity: Callable
    consent: Callable
    request_type: type
    result_type: type
    event_identity: Callable


def _reject(code):
    raise SemanticPublicationRejectedError(code)


def _valid_response_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and value == value.strip()
        and all(char.isprintable() for char in value)
    )


def _parse_result(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 16000:
        _reject("semantic_runtime_output_capacity")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _reject("semantic_runtime_duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        _reject("semantic_runtime_nonfinite_json")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SemanticPublicationRejectedError("semantic_runtime_output_json") from exc
    if not isinstance(result, dict):
        _reject("semantic_runtime_output_shape")
    return result


class OwnedSemanticRuntimeBridge:
    """Internal callable implementing run_backend_turn(role=..., request=...)."""

    def __init__(self, *, binding, current_binding, repository, dependencies, expected_runtime_identity):
        if not isinstance(binding, PublicationBinding) or expected_runtime_identity is None:
            _reject("semantic_runtime_binding_invalid")
        self.binding = binding
        self.expected_runtime_identity = expected_runtime_identity
        self.current_binding = current_binding
        self.repository = repository
        self._deps = dependencies

    async def _authority(self, expected_runtime=None, expected_epoch=None):
        current = await asyncio.to_thread(self.current_binding)
        if current != self.binding:
            _reject("semantic_runtime_publication_authority_changed")
        runtime = await self._deps.resolver(self.binding.uid, repository=self.repository, target_mode=TARGET_MODE)
        if (
            runtime is None
            or runtime.uid != self.binding.uid
            or runtime.provider != "hermes_cloud"
            or runtime.runtime_target_mode != TARGET_MODE
            or runtime.status != "active"
        ):
            _reject("semantic_existing_cloud_transcript_target_required")
        identity = self._deps.identity(runtime)
        if identity != self.expected_runtime_identity:
            _reject("semantic_runtime_hydration_target_changed")
        epoch = self._deps.consent(runtime)
        if not isinstance(epoch, str) or not epoch:
            _reject("semantic_runtime_consent_missing")
        if expected_runtime is not None and identity != expected_runtime:
            _reject("semantic_runtime_target_changed")
        if expected_epoch is not None and epoch != expected_epoch:
            _reject("semantic_runtime_consent_changed")
        return runtime, identity, epoch

    async def __call__(self, *, role, request):
        if role not in CHANNELS or not isinstance(request, dict):
            _reject("semantic_runtime_request_invalid")
        request = copy.deepcopy(request)
        if request.get("policy") != "ella-semantic-summary-v1" or not isinstance(request.get("instructions"), str):
            _reject("semantic_runtime_policy_invalid")
        wire = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(wire.encode("utf-8")) > MAX_REQUEST_BYTES:
            _reject("semantic_runtime_request_capacity")
        key = digest(
            {
                "domain": "ella-owned-semantic-turn-v1",
                "binding": asdict(self.binding),
                "role": role,
                "request_sha256": digest(request),
            }
        )
        interaction = "semantic-" + role + "-" + key
        channel = CHANNELS[role]
        runtime, identity, epoch = await self._authority()
        turn_request = self._deps.request_type(
            uid=self.binding.uid,
            client_interaction_id=interaction,
            correlation_id=interaction,
            channel=channel,
            user_input=wire,
            instructions=request["instructions"],
            started_at=datetime.now(timezone.utc),
            consent_grant_epoch=epoch,
            client_metadata={
                "source": "semantic_publication_v1",
                "role": role,
                "binding_sha256": digest(asdict(self.binding)),
                "request_sha256": digest(request),
            },
            user_scan_policy="none",
            allow_previous_response=False,
        )

        async def before_send():
            await self._authority(identity, epoch)

        # No response_validator: the existing service's format-retry lane must
        # not create a second provider attempt. Invalid output leaves START held.
        turn = await self._deps.service.run_turn(runtime, turn_request, before_provider_call=before_send)
        await self._authority(identity, epoch)
        _, expected_user, expected_assistant = self._deps.event_identity(
            uid=self.binding.uid, channel=channel, client_interaction_id=interaction
        )
        if (
            not isinstance(turn, self._deps.result_type)
            or turn.canonical_user_event_id != expected_user
            or turn.canonical_assistant_event_id != expected_assistant
            or not isinstance(turn.runtime_interaction_id, str)
            or not turn.runtime_interaction_id
            or not _valid_response_id(turn.response_id)
        ):
            _reject("semantic_runtime_returned_provenance_invalid")
        return BackendTurn(_parse_result(turn.text), turn.runtime_interaction_id, turn.canonical_assistant_event_id)


def create_owned_runtime_bridge(*, binding, current_binding, repository, event_store, expected_runtime_identity):
    """Use existing production classes; construction does not invoke a turn.

    No caller-selectable model, credential, host, shadow mode or transport. Resolver
    and the runtime retain their existing consent/target/provider-send fences.
    """
    from ella.services.hermes_cloud_runtime import (
        HermesCloudRuntimeService,
        HermesCloudTurnRequest,
        HermesCloudTurnResult,
        assert_runtime_managed_consent,
        hermes_cloud_event_identity,
    )
    from ella.services.runtime_resolver import resolve_isolated_runtime, cloud_runtime_authority_identity

    dependencies = _RuntimeDependencies(
        HermesCloudRuntimeService(repository=repository, event_store=event_store, allow_shadow=False),
        resolve_isolated_runtime,
        cloud_runtime_authority_identity,
        assert_runtime_managed_consent,
        HermesCloudTurnRequest,
        HermesCloudTurnResult,
        hermes_cloud_event_identity,
    )
    return OwnedSemanticRuntimeBridge(
        binding=binding,
        current_binding=current_binding,
        repository=repository,
        dependencies=dependencies,
        expected_runtime_identity=expected_runtime_identity,
    )
