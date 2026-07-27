"""Durable, idempotent Hermes Cloud turn execution."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from database import voice_canary as voice_canary_db
from database.ella_provisioning import EllaProvisioningRepository, RuntimePoolClaimError
from ella.routers.canonical_events import CanonicalEventIn, CanonicalEventStore
from ella.services.hermes_cloud import HermesCloudClient, estimate_turn_cost_microusd
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime

HERMES_CLOUD_CHAT_MODE = "hermes-cloud-chat"


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


@dataclass(frozen=True)
class HermesCloudTurnResult:
    text: str
    response_id: str
    canonical_user_event_id: str
    canonical_assistant_event_id: str
    duplicate: bool
    usage: dict[str, Any]


def _request_hash(request: HermesCloudTurnRequest) -> str:
    material = "\x1f".join(
        (
            request.uid,
            request.channel,
            request.client_interaction_id,
            request.user_input,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
        scan_policy="immediate" if role == "user" else "none",
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
    ):
        self.repository = repository
        self.event_store = event_store
        self.cloud_client = cloud_client or HermesCloudClient()
        self.voice_policy = voice_policy
        self.cost_estimator = cost_estimator

    async def run_turn(
        self,
        runtime: IsolatedRuntime,
        request: HermesCloudTurnRequest,
    ) -> HermesCloudTurnResult:
        if runtime.provider != "hermes_cloud" or runtime.uid != request.uid:
            raise ProvisioningError("hermes_cloud_runtime_required", retryable=False)
        source_identity, user_event_id, assistant_event_id = _event_identity(request)
        request_hash = _request_hash(request)
        scope = await self.repository.get_or_create_runtime_scope(
            uid=request.uid,
            binding_id=runtime.binding_id,
            role="user",
            channel=request.channel,
        )
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
                source_identity=source_identity,
            )
            if not stored:
                raise ProvisioningError("hermes_cloud_completed_turn_missing_event", retryable=True)
            return HermesCloudTurnResult(
                text=str(stored.get("text") or ""),
                response_id=str(interaction.get("provider_response_id") or ""),
                canonical_user_event_id=user_event_id,
                canonical_assistant_event_id=assistant_event_id,
                duplicate=True,
                usage=dict(interaction.get("usage") or {}),
            )

        claimed = await self.repository.claim_runtime_interaction(str(interaction["id"]))
        if not claimed:
            raise ProvisioningError("hermes_cloud_turn_in_progress", retryable=True)
        recovered = await self.event_store.get_event(
            uid=request.uid,
            event_id=assistant_event_id,
            source_identity=source_identity,
        )
        if recovered:
            provider_response_id = str(
                (recovered.get("source_ref") or {}).get("provider_response_id") or ""
            )
            if not provider_response_id:
                raise ProvisioningError(
                    "hermes_cloud_recovered_turn_missing_provider_ref",
                    retryable=False,
                )
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
            )

        user_receipt: Optional[dict[str, Any]] = None
        assistant_receipt: Optional[dict[str, Any]] = None
        user_ingestion_completed = False
        assistant_ingestion_completed = False
        lease_open = False
        provider_response_ids: list[str] = []
        estimated_cost = 0
        termination_reason = "provider_error"
        normalized_error = "hermes_cloud_turn_failed"
        try:
            user_event = _event(
                request=request,
                session_key=str(scope["session_key"]),
                source_identity=source_identity,
                event_id=user_event_id,
                role="user",
                text=request.user_input,
            )
            user_write = await self.event_store.write_batch([user_event])
            user_receipt = await self.repository.claim_runtime_ingestion(
                binding_id=runtime.binding_id,
                canonical_event_id=user_event_id,
                source_identity=source_identity,
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
            admission = await self.voice_policy.accept_session(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                correlation_id=request.correlation_id,
                entitlement_revision=int(entitlement["revision"]),
                provider="hermes_cloud",
                model=runtime.expected_model,
                mode=HERMES_CLOUD_CHAT_MODE,
            )
            if not admission.allowed:
                raise ProvisioningError(admission.code, retryable=False)
            lease_open = True

            turn = await self.cloud_client.create_response(
                {
                    "expected_model": runtime.expected_model,
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
            )
            provider_response_ids = [turn.response_id]
            estimated_cost = self.cost_estimator(turn.usage)
            quota = await self.voice_policy.update_session(
                uid=request.uid,
                session_id=str(claimed["hermes_session_id"]),
                input_audio_s=0,
                output_audio_s=0,
                input_audio_bytes=0,
                output_audio_bytes=0,
                tool_calls=0,
                reconnects=0,
                provider_request_ids=provider_response_ids,
                estimated_cost_microusd=estimated_cost,
            )
            if not quota.allowed:
                raise ProvisioningError(quota.code, retryable=False)

            assistant_event = _event(
                request=request,
                session_key=str(scope["session_key"]),
                source_identity=source_identity,
                event_id=assistant_event_id,
                role="assistant",
                text=turn.text,
                provider_response_id=turn.response_id,
            )
            assistant_write = await self.event_store.write_batch([assistant_event])
            assistant_receipt = await self.repository.claim_runtime_ingestion(
                binding_id=runtime.binding_id,
                canonical_event_id=assistant_event_id,
                source_identity=source_identity,
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
                await self.voice_policy.complete_session(
                    uid=request.uid,
                    session_id=str(claimed["hermes_session_id"]),
                    input_audio_s=0,
                    output_audio_s=0,
                    connection_s=0,
                    input_audio_bytes=0,
                    output_audio_bytes=0,
                    tool_calls=0,
                    reconnects=0,
                    provider_request_ids=provider_response_ids,
                    termination_reason=termination_reason,
                    normalized_error_code=normalized_error or None,
                    estimated_cost_microusd=estimated_cost,
                )
