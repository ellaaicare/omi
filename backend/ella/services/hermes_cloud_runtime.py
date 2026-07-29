"""Durable, idempotent Hermes Cloud turn execution."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

from database import voice_canary as voice_canary_db
from database.ella_provisioning import EllaProvisioningRepository, RuntimePoolClaimError
from ella.routers.canonical_events import CanonicalEventIn, CanonicalEventStore
from ella.services.ai_consent import (
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud import (
    HermesCloudClient,
    estimate_max_turn_cost_microusd,
    estimate_turn_cost_microusd,
)
from ella.services.hermes_cloud_policy import current_cloud_authority
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime

HERMES_CLOUD_CHAT_MODE = "hermes-cloud-chat"
HERMES_CLOUD_ENRICHMENT_MODE = "hermes-cloud-enrichment"
HERMES_CLOUD_PHOTON_MODE = "hermes-cloud-photon"
HERMES_CLOUD_ENRICHMENT_CHANNEL = "omi_enrichment"
DEFAULT_MAX_INPUT_TOKENS = 8192
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_MAX_TOOL_CALLS = 2
INVALID_OUTPUT_ERROR = "hermes_cloud_enrichment_output_invalid"


def assert_runtime_managed_consent(runtime: IsolatedRuntime) -> str:
    authority = current_cloud_authority(
        runtime.uid,
        profile_class=runtime.profile_class,
        profile_uid=runtime.uid,
        runtime_provider=runtime.provider,
        model_route=f"openai-codex/{runtime.expected_model}",
        memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
        photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
    )
    if not authority.grant_epoch:
        raise ProvisioningError("managed_cloud_consent_stale", retryable=False)
    return authority.grant_epoch


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ProvisioningError("hermes_cloud_turn_budget_invalid", retryable=False) from exc
    if not minimum <= value <= maximum:
        raise ProvisioningError("hermes_cloud_turn_budget_invalid", retryable=False)
    return value


def _turn_budget(runtime: IsolatedRuntime) -> dict[str, int]:
    model_context_window_tokens = int(runtime.model_context_window_tokens)
    if model_context_window_tokens <= 0:
        raise ProvisioningError("hermes_cloud_model_context_invalid", retryable=False)
    max_tool_calls = min(
        len(runtime.allowed_tools),
        _bounded_env_int(
            "ELLA_HERMES_CLOUD_MAX_TOOL_CALLS",
            DEFAULT_MAX_TOOL_CALLS,
            0,
            32,
        ),
    )
    max_input_tokens = _bounded_env_int(
        "ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS",
        DEFAULT_MAX_INPUT_TOKENS,
        256,
        262144,
    )
    max_output_tokens = _bounded_env_int(
        "ELLA_HERMES_CLOUD_MAX_OUTPUT_TOKENS",
        DEFAULT_MAX_OUTPUT_TOKENS,
        1,
        32768,
    )
    if max_output_tokens > model_context_window_tokens:
        raise ProvisioningError("hermes_cloud_turn_budget_invalid", retryable=False)
    provider_call_upper_bound = max_tool_calls + 1
    return {
        "max_input_tokens": min(max_input_tokens, model_context_window_tokens),
        "max_output_tokens": max_output_tokens,
        "max_tool_calls": max_tool_calls,
        # The pinned model enforces its context ceiling per provider round.
        # Reserving every tool-enabled round deliberately overprices
        # input+output overlap, but covers all Hermes-owned prompt/tool/memory
        # layers and each possible follow-up model call before execution.
        "provider_input_token_upper_bound": (model_context_window_tokens * provider_call_upper_bound),
        "provider_output_token_upper_bound": (max_output_tokens * provider_call_upper_bound),
    }


def _usage_token_count(usage: Any, field: str) -> int:
    if not isinstance(usage, dict) or field not in usage:
        raise ProvisioningError("hermes_cloud_previous_usage_missing", retryable=False)
    try:
        value = int(usage[field])
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("hermes_cloud_previous_usage_invalid", retryable=False) from exc
    if value < 0:
        raise ProvisioningError("hermes_cloud_previous_usage_invalid", retryable=False)
    return value


def conservative_client_input_token_upper_bound(
    *,
    user_input: str,
    instructions: str,
    previous_response_id: Optional[str],
    previous_response_usage: Any,
) -> int:
    """Bound caller-visible and predecessor input for local admission.

    UTF-8 byte length is a deterministic upper bound for tokens contributed by
    the new JSON text fields. A chained response also replays the predecessor's
    complete input and output, so its provider-reported usage is carried
    forward. Remote Hermes prompt, policy, tool, memory, and profile layers are
    covered separately by reserving the signed model context-window ceiling.
    """
    current_envelope = {
        "input": user_input,
        "instructions": instructions,
    }
    current_bound = len(
        json.dumps(
            current_envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if not previous_response_id:
        return current_bound
    return (
        current_bound
        + _usage_token_count(previous_response_usage, "input_tokens")
        + _usage_token_count(previous_response_usage, "output_tokens")
    )


@dataclass(frozen=True)
class HermesCloudTurnRequest:
    uid: str
    client_interaction_id: str
    correlation_id: str
    channel: str
    user_input: str
    instructions: str
    started_at: datetime
    client_metadata: dict[str, Any]
    consent_grant_epoch: Optional[str] = None
    user_scan_policy: str = "immediate"


@dataclass(frozen=True)
class HermesCloudTurnResult:
    text: str
    response_id: str
    canonical_user_event_id: str
    canonical_assistant_event_id: str
    duplicate: bool
    usage: dict[str, Any]
    runtime_interaction_id: str


def _request_hash(request: HermesCloudTurnRequest) -> str:
    material = "\x1f".join(
        (
            request.uid,
            request.channel,
            request.client_interaction_id,
            request.user_input,
            request.instructions,
            request.consent_grant_epoch or "",
            request.user_scan_policy,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_provider_response(
    response_validator: Optional[Callable[[str], None]],
    text: str,
) -> None:
    if response_validator is None:
        return
    try:
        response_validator(text)
    except ProvisioningError:
        raise
    except Exception as exc:
        raise ProvisioningError(
            INVALID_OUTPUT_ERROR,
            retryable=True,
        ) from exc


def _event_identity(request: HermesCloudTurnRequest) -> tuple[str, str, str]:
    digest = hashlib.sha256(
        f"{request.uid}|{request.channel}|{request.client_interaction_id}".encode("utf-8")
    ).hexdigest()[:32]
    source_identity = f"hermes_cloud:{request.channel}:interaction:{digest}"
    return (
        source_identity,
        f"hermes-cloud:{digest}:user",
        f"hermes-cloud:{digest}:assistant",
    )


def _event(
    *,
    request: HermesCloudTurnRequest,
    session_key: str,
    source_identity: str,
    event_id: str,
    role: str,
    text: str,
    provider_response_id: Optional[str] = None,
) -> CanonicalEventIn:
    now = datetime.now(timezone.utc)
    started_at = request.started_at if role == "user" else now
    return CanonicalEventIn(
        uid=request.uid,
        canonical_identity=request.uid,
        event_id=event_id,
        session_id=session_key,
        channel=request.channel,
        provider="hermes_cloud",
        role=role,
        text=text,
        started_at=started_at,
        ended_at=now if role == "assistant" else None,
        privacy_scope="user_private",
        scan_policy=request.user_scan_policy if role == "user" else "none",
        source_ref={
            "source_identity": source_identity,
            "client_interaction_id": request.client_interaction_id,
            "provider_response_id": provider_response_id,
        },
        metadata={
            "adapter": "hermes-cloud",
            "correlation_id": request.correlation_id,
            "client": request.client_metadata,
            "event_revision": 1,
        },
    )


class HermesCloudRuntimeService:
    def __init__(
        self,
        *,
        repository: EllaProvisioningRepository,
        event_store: CanonicalEventStore,
        cloud_client: Optional[HermesCloudClient] = None,
        voice_policy: Any = voice_canary_db,
        cost_estimator: Any = estimate_turn_cost_microusd,
        max_cost_estimator: Any = estimate_max_turn_cost_microusd,
        allow_shadow: bool = False,
    ):
        self.repository = repository
        self.event_store = event_store
        self.cloud_client = cloud_client or HermesCloudClient()
        self.voice_policy = voice_policy
        self.cost_estimator = cost_estimator
        self.max_cost_estimator = max_cost_estimator
        self.allow_shadow = allow_shadow

    async def run_turn(
        self,
        runtime: IsolatedRuntime,
        request: HermesCloudTurnRequest,
        *,
        before_provider_call: Optional[Callable[[], Awaitable[None]]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
    ) -> HermesCloudTurnResult:
        if runtime.provider != "hermes_cloud" or runtime.uid != request.uid:
            raise ProvisioningError("hermes_cloud_runtime_required", retryable=False)
        profile_class = await self.repository.get_cloud_profile_class(request.uid)
        if profile_class != runtime.profile_class:
            raise ProvisioningError("hermes_cloud_profile_class_changed", retryable=False)
        current_grant_epoch = assert_runtime_managed_consent(runtime)
        if request.consent_grant_epoch and not hmac.compare_digest(
            request.consent_grant_epoch,
            current_grant_epoch,
        ):
            raise ProvisioningError(
                "managed_cloud_consent_grant_changed",
                retryable=False,
            )
        if runtime.status == "shadow" and not self.allow_shadow:
            raise ProvisioningError("hermes_cloud_shadow_not_routable", retryable=False)
        if not request.client_interaction_id.strip():
            raise ProvisioningError("client_interaction_id_required", retryable=False)
        if request.user_scan_policy not in {"immediate", "none"}:
            raise ProvisioningError(
                "hermes_cloud_scan_policy_invalid",
                retryable=False,
            )
        stable_user_request = request
        scope = await self.repository.get_or_create_runtime_scope(
            uid=request.uid,
            binding_id=runtime.binding_id,
            role="user",
            channel=request.channel,
            allow_shadow=self.allow_shadow,
        )
        if response_validator is not None:
            invalid_attempts = await self.repository.count_runtime_interaction_failures(
                scope_id=str(scope["id"]),
                client_interaction_id=request.client_interaction_id,
                error_code=INVALID_OUTPUT_ERROR,
            )
            max_attempts = _bounded_env_int(
                "ELLA_HERMES_CLOUD_ENRICHMENT_MAX_OUTPUT_ATTEMPTS",
                3,
                1,
                10,
            )
            if invalid_attempts >= max_attempts:
                raise ProvisioningError(
                    "hermes_cloud_enrichment_output_exhausted",
                    retryable=False,
                )
            if invalid_attempts:
                request = replace(
                    request,
                    client_interaction_id=(f"{request.client_interaction_id}:format-retry:" f"{invalid_attempts}"),
                )
        user_source_identity, user_event_id, _ = _event_identity(stable_user_request)
        assistant_source_identity, _, assistant_event_id = _event_identity(request)
        request_hash = _request_hash(request)
        try:
            interaction = await self.repository.get_or_create_runtime_interaction(
                scope_id=str(scope["id"]),
                client_interaction_id=request.client_interaction_id,
                request_hash=request_hash,
                correlation_id=request.correlation_id,
                canonical_user_event_id=user_event_id,
                canonical_assistant_event_id=assistant_event_id,
            )
        except RuntimePoolClaimError as exc:
            raise ProvisioningError(str(exc), retryable=False) from exc

        if interaction.get("status") == "completed":
            stored = await self.event_store.get_event(
                uid=request.uid,
                event_id=assistant_event_id,
                source_identity=assistant_source_identity,
            )
            if not stored:
                raise ProvisioningError("hermes_cloud_completed_turn_missing_event", retryable=True)
            try:
                _validate_provider_response(
                    response_validator,
                    str(stored.get("text") or ""),
                )
            except ProvisioningError:
                await self.repository.invalidate_completed_runtime_interaction(
                    interaction_id=str(interaction["id"]),
                    error_code=INVALID_OUTPUT_ERROR,
                )
                raise
            return HermesCloudTurnResult(
                text=str(stored.get("text") or ""),
                response_id=str(interaction.get("provider_response_id") or ""),
                canonical_user_event_id=user_event_id,
                canonical_assistant_event_id=assistant_event_id,
                duplicate=True,
                usage=dict(interaction.get("usage") or {}),
                runtime_interaction_id=str(interaction["id"]),
            )

        claimed = await self.repository.claim_runtime_interaction(str(interaction["id"]))
        if not claimed:
            raise ProvisioningError("hermes_cloud_turn_in_progress", retryable=True)
        recovered = await self.event_store.get_event(
            uid=request.uid,
            event_id=assistant_event_id,
            source_identity=assistant_source_identity,
        )
        if recovered:
            provider_response_id = str((recovered.get("source_ref") or {}).get("provider_response_id") or "")
            if not provider_response_id:
                raise ProvisioningError(
                    "hermes_cloud_recovered_turn_missing_provider_ref",
                    retryable=False,
                )
            try:
                _validate_provider_response(
                    response_validator,
                    str(recovered.get("text") or ""),
                )
            except ProvisioningError:
                await self.repository.fail_runtime_interaction(
                    interaction_id=str(claimed["id"]),
                    error_code=INVALID_OUTPUT_ERROR,
                )
                raise
            await self.repository.complete_runtime_interaction(
                interaction_id=str(claimed["id"]),
                provider_response_id=provider_response_id,
                usage=dict(claimed.get("usage") or {}),
            )
            return HermesCloudTurnResult(
                text=str(recovered.get("text") or ""),
                response_id=provider_response_id,
                canonical_user_event_id=user_event_id,
                canonical_assistant_event_id=assistant_event_id,
                duplicate=True,
                usage=dict(claimed.get("usage") or {}),
                runtime_interaction_id=str(claimed["id"]),
            )

        user_receipt: Optional[dict[str, Any]] = None
        assistant_receipt: Optional[dict[str, Any]] = None
        user_ingestion_completed = False
        assistant_ingestion_completed = False
        lease_open = False
        provider_response_ids: list[str] = []
        estimated_cost = 0
        reserved_cost = 0
        reservation_settled = False
        provider_started = False
        actual_tool_calls = 0
        termination_reason = "provider_error"
        normalized_error = "hermes_cloud_turn_failed"
        try:
            user_event = _event(
                request=request,
                session_key=str(scope["session_key"]),
                source_identity=user_source_identity,
                event_id=user_event_id,
                role="user",
                text=request.user_input,
            )
            user_write = await self.event_store.write_batch([user_event])
            user_receipt = await self.repository.claim_runtime_ingestion(
                binding_id=runtime.binding_id,
                canonical_event_id=user_event_id,
                source_identity=user_source_identity,
                event_revision=1,
                provenance="canonical_writeback",
            )
            await self.repository.complete_runtime_ingestion(
                receipt_id=str(user_receipt["id"]),
                status="written",
                metadata={
                    "canonical_inserted": int(user_write.get("inserted") or 0),
                    "canonical_duplicates": int(user_write.get("duplicates") or 0),
                    "content_free": True,
                },
            )
            user_ingestion_completed = True

            entitlement = await self.voice_policy.get_entitlement(request.uid)
            if not entitlement:
                raise ProvisioningError("no_entitlement", retryable=False)
            if request.channel == "photon":
                entitlement_mode = HERMES_CLOUD_PHOTON_MODE
            elif request.channel == HERMES_CLOUD_ENRICHMENT_CHANNEL:
                entitlement_mode = HERMES_CLOUD_ENRICHMENT_MODE
            else:
                entitlement_mode = HERMES_CLOUD_CHAT_MODE
            admission = await self.voice_policy.accept_session(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                correlation_id=request.correlation_id,
                entitlement_revision=int(entitlement["revision"]),
                provider="hermes_cloud",
                model=runtime.expected_model,
                mode=entitlement_mode,
            )
            if not admission.allowed:
                raise ProvisioningError(admission.code, retryable=False)
            lease_open = True

            budget = _turn_budget(runtime)
            client_input_token_upper_bound = conservative_client_input_token_upper_bound(
                user_input=request.user_input,
                instructions=request.instructions,
                previous_response_id=claimed.get("previous_response_id"),
                previous_response_usage=claimed.get("previous_response_usage"),
            )
            if client_input_token_upper_bound > budget["max_input_tokens"]:
                raise ProvisioningError(
                    "hermes_cloud_input_budget_exceeded",
                    retryable=False,
                    detail={
                        "client_input_token_upper_bound": client_input_token_upper_bound,
                        "max_input_tokens": budget["max_input_tokens"],
                    },
                )
            reserved_cost = self.max_cost_estimator(
                max_input_tokens=budget["provider_input_token_upper_bound"],
                max_output_tokens=budget["provider_output_token_upper_bound"],
            )
            reservation = await self.voice_policy.reserve_session_cost(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                reservation_microusd=reserved_cost,
            )
            if not reservation.allowed:
                raise ProvisioningError(reservation.code, retryable=False)

            # Consent may be revoked while canonical ingest/admission work is in
            # flight. Recheck at the last boundary before protected content is
            # sent to Hermes Cloud and its OpenAI model route.
            current_grant_epoch = assert_runtime_managed_consent(runtime)
            profile_class = await self.repository.get_cloud_profile_class(request.uid)
            if profile_class != runtime.profile_class:
                raise ProvisioningError("hermes_cloud_profile_class_changed", retryable=False)
            if request.consent_grant_epoch and not hmac.compare_digest(
                request.consent_grant_epoch,
                current_grant_epoch,
            ):
                raise ProvisioningError(
                    "managed_cloud_consent_grant_changed",
                    retryable=False,
                )

            async def mark_provider_send_boundary() -> None:
                nonlocal provider_started
                if before_provider_call is not None:
                    await before_provider_call()
                current_profile_class = await self.repository.get_cloud_profile_class(request.uid)
                if current_profile_class != runtime.profile_class:
                    raise ProvisioningError(
                        "hermes_cloud_profile_class_changed",
                        retryable=False,
                    )
                final_grant_epoch = assert_runtime_managed_consent(runtime)
                if request.consent_grant_epoch and not hmac.compare_digest(
                    request.consent_grant_epoch,
                    final_grant_epoch,
                ):
                    raise ProvisioningError(
                        "managed_cloud_consent_grant_changed",
                        retryable=False,
                    )
                provider_started = True

            turn = await self.cloud_client.create_response(
                {
                    "expected_model": runtime.expected_model,
                    "model_context_window_tokens": runtime.model_context_window_tokens,
                    "allowed_tools": list(runtime.allowed_tools),
                },
                session_key=str(scope["session_key"]),
                hermes_session_id=str(claimed["hermes_session_id"]),
                idempotency_key=str(claimed["idempotency_key"]),
                user_input=request.user_input,
                instructions=request.instructions,
                previous_response_id=claimed.get("previous_response_id"),
                base_url=runtime.gateway_url,
                token=runtime.gateway_token,
                max_output_tokens=budget["max_output_tokens"],
                max_tool_calls=budget["max_tool_calls"],
                before_provider_send=mark_provider_send_boundary,
            )
            provider_response_ids = [turn.response_id]
            actual_tool_calls = turn.tool_calls
            if (
                int(turn.usage["input_tokens"]) + int(turn.usage["output_tokens"])
                > budget["provider_input_token_upper_bound"]
            ):
                raise ProvisioningError(
                    "hermes_cloud_provider_context_exceeded",
                    retryable=False,
                )
            if int(turn.usage["output_tokens"]) > budget["provider_output_token_upper_bound"]:
                raise ProvisioningError(
                    "hermes_cloud_output_budget_exceeded",
                    retryable=False,
                )
            estimated_cost = self.cost_estimator(turn.usage)
            if estimated_cost > reserved_cost:
                raise ProvisioningError("hermes_cloud_cost_reservation_exceeded", retryable=False)
            await self.voice_policy.settle_session_cost(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                actual_cost_microusd=estimated_cost,
                tool_calls=turn.tool_calls,
            )
            reservation_settled = True
            await self.repository.record_runtime_provider_receipt(
                interaction_id=str(claimed["id"]),
                provider_response_id=turn.response_id,
                usage=turn.usage,
            )
            quota = await self.voice_policy.update_session(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                input_audio_s=0,
                output_audio_s=0,
                input_audio_bytes=0,
                output_audio_bytes=0,
                tool_calls=turn.tool_calls,
                reconnects=0,
                provider_request_ids=provider_response_ids,
                estimated_cost_microusd=estimated_cost,
            )
            if not quota.allowed:
                raise ProvisioningError(quota.code, retryable=False)

            _validate_provider_response(response_validator, turn.text)
            assistant_event = _event(
                request=request,
                session_key=str(scope["session_key"]),
                source_identity=assistant_source_identity,
                event_id=assistant_event_id,
                role="assistant",
                text=turn.text,
                provider_response_id=turn.response_id,
            )
            assistant_write = await self.event_store.write_batch([assistant_event])
            assistant_receipt = await self.repository.claim_runtime_ingestion(
                binding_id=runtime.binding_id,
                canonical_event_id=assistant_event_id,
                source_identity=assistant_source_identity,
                event_revision=1,
                provenance="hermes_response",
            )
            await self.repository.complete_runtime_ingestion(
                receipt_id=str(assistant_receipt["id"]),
                status="written",
                provider_ref=turn.response_id,
                metadata={
                    "canonical_inserted": int(assistant_write.get("inserted") or 0),
                    "canonical_duplicates": int(assistant_write.get("duplicates") or 0),
                    "content_free": True,
                },
            )
            assistant_ingestion_completed = True
            await self.repository.complete_runtime_interaction(
                interaction_id=str(claimed["id"]),
                provider_response_id=turn.response_id,
                usage=turn.usage,
            )
            termination_reason = "completed"
            normalized_error = ""
            return HermesCloudTurnResult(
                text=turn.text,
                response_id=turn.response_id,
                canonical_user_event_id=user_event_id,
                canonical_assistant_event_id=assistant_event_id,
                duplicate=False,
                usage=turn.usage,
                runtime_interaction_id=str(claimed["id"]),
            )
        except asyncio.CancelledError:
            termination_reason = "client_disconnect"
            normalized_error = "client_cancelled"
            await self.repository.fail_runtime_interaction(
                interaction_id=str(claimed["id"]),
                error_code=normalized_error,
            )
            raise
        except ProvisioningError as exc:
            normalized_error = exc.code
            await self.repository.fail_runtime_interaction(
                interaction_id=str(claimed["id"]),
                error_code=exc.code,
            )
            for receipt, completed in (
                (user_receipt, user_ingestion_completed),
                (assistant_receipt, assistant_ingestion_completed),
            ):
                if receipt and not completed:
                    await self.repository.complete_runtime_ingestion(
                        receipt_id=str(receipt["id"]),
                        status="failed",
                        metadata={"error_code": exc.code, "content_free": True},
                    )
            raise
        except Exception as exc:
            await self.repository.fail_runtime_interaction(
                interaction_id=str(claimed["id"]),
                error_code=normalized_error,
            )
            raise ProvisioningError(normalized_error, retryable=True) from exc
        finally:
            if lease_open:
                if reserved_cost and not reservation_settled:
                    if provider_started:
                        await self.voice_policy.settle_session_cost(
                            uid=request.uid,
                            session_id=str(claimed["hermes_session_id"]),
                            actual_cost_microusd=reserved_cost,
                            tool_calls=actual_tool_calls,
                        )
                    else:
                        await self.voice_policy.release_session_cost(
                            uid=request.uid,
                            session_id=str(claimed["hermes_session_id"]),
                        )
                await self.voice_policy.complete_session(
                    uid=request.uid,
                    session_id=str(claimed["hermes_session_id"]),
                    input_audio_s=0,
                    output_audio_s=0,
                    connection_s=0,
                    input_audio_bytes=0,
                    output_audio_bytes=0,
                    tool_calls=actual_tool_calls,
                    reconnects=0,
                    provider_request_ids=provider_response_ids,
                    termination_reason=termination_reason,
                    normalized_error_code=normalized_error or None,
                    estimated_cost_microusd=(
                        estimated_cost if reservation_settled else reserved_cost if provider_started else 0
                    ),
                )
