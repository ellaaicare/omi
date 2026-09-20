"""Owner-derived self-hosted Hermes execution for verified iMessage text DMs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

import httpx

from database.honcho_attestation import authority_credential
from database.imessage_runtime import (
    ImessageRuntimeAuthority,
    ImessageRuntimeRepository,
    ImessageRuntimeRepositoryError,
)
from ella.routers.canonical_events import CanonicalEventIn, CanonicalEventStore, PostgresCanonicalEventStore
from ella.services.hermes_session import canonical_omi_session_key, channel_omi_session_id
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import (
    CloudRuntimeAuthorityIdentity,
    IsolatedRuntime,
    retained_owner_uid_configured,
    resolve_imessage_retained_runtime,
    resolve_isolated_runtime,
    revalidate_imessage_runtime_authority,
    runtime_authority_identity,
)

IMESSAGE_CHANNEL = "imessage"
IMESSAGE_OUTBOUND_MAX_CHARS = 8_000


class ImessageRuntimeError(RuntimeError):
    def __init__(self, code: str, *, status_code: int, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ImessageInbound:
    line_identity: str
    contact_identity: str
    connection_id: str
    provider_message_id: str
    text: str
    occurred_at: datetime
    attachment_count: int = 0
    group_message: bool = False


@dataclass(frozen=True)
class ImessageDeliveryIdentity:
    line_identity: str
    contact_identity: str
    connection_id: str
    receipt_id: str
    delivery_idempotency_key: str
    binding_generation: int


class HermesCompletionTransport(Protocol):
    async def complete(
        self,
        *,
        runtime: IsolatedRuntime,
        user_text: str,
        session_key: str,
        memory_key: str,
    ) -> str: ...


class SelfHostedHermesCompletionClient:
    """One bounded non-stream inference against the resolved self-hosted runtime."""

    def __init__(self, *, timeout_seconds: float = 60.0, max_response_bytes: int = 65_536) -> None:
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_response_bytes = max_response_bytes

    async def complete(
        self,
        *,
        runtime: IsolatedRuntime,
        user_text: str,
        session_key: str,
        memory_key: str,
    ) -> str:
        retained_owner = not runtime.runtime_target_id and retained_owner_uid_configured(runtime.uid)
        target_runtime = runtime.runtime_target_mode == "hermes-chat" and runtime.binding_role == "user"
        retained_runtime = retained_owner and runtime.binding_role == "imessage"
        if runtime.provider != "hermes" or not (target_runtime or retained_runtime):
            raise ImessageRuntimeError("imessage_runtime_target_invalid", status_code=409)
        endpoint = f"{runtime.gateway_url.rstrip('/')}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {runtime.gateway_token}",
                        "Content-Type": "application/json",
                        "X-Hermes-Session-Id": session_key,
                        "X-Hermes-Session-Key": memory_key,
                    },
                    content=json.dumps(
                        {
                            "model": runtime.agent_id,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": (
                                        "This is a verified Ella iMessage text-DM turn. Reply only to the enrolled "
                                        "user. Do not send another message, process an attachment, address a group, "
                                        "or perform caregiver delivery."
                                    ),
                                },
                                {"role": "user", "content": user_text},
                            ],
                            "stream": False,
                        },
                        separators=(",", ":"),
                    ),
                ) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise ImessageRuntimeError("imessage_model_response_too_large", status_code=503)
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if response.status_code != 200:
                        raise ImessageRuntimeError("imessage_model_request_failed", status_code=503)
        except ImessageRuntimeError:
            raise
        except httpx.HTTPError as exc:
            raise ImessageRuntimeError("imessage_model_transport_uncertain", status_code=503) from exc
        try:
            payload = json.loads(body)
            choices = payload.get("choices") if isinstance(payload, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else None
            text = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise ImessageRuntimeError("imessage_model_response_invalid", status_code=503) from exc
        if not text or len(text) > IMESSAGE_OUTBOUND_MAX_CHARS:
            raise ImessageRuntimeError("imessage_model_response_invalid", status_code=503)
        return text


def imessage_runtime_enabled() -> bool:
    return os.getenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "false").strip().lower() == "true"


class ImessageRuntimeService:
    def __init__(
        self,
        *,
        repository: ImessageRuntimeRepository,
        event_store: CanonicalEventStore,
        completion_client: HermesCompletionTransport,
        hmac_key: Optional[bytes] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        runtime_resolver: Callable[..., Awaitable[Optional[IsolatedRuntime]]] = resolve_isolated_runtime,
        retained_runtime_resolver: Callable[..., Awaitable[Optional[IsolatedRuntime]]] = (
            resolve_imessage_retained_runtime
        ),
        runtime_revalidator: Callable[[CloudRuntimeAuthorityIdentity], Awaitable[IsolatedRuntime]] = (
            revalidate_imessage_runtime_authority
        ),
    ) -> None:
        self.repository = repository
        self.event_store = event_store
        self.completion_client = completion_client
        self.hmac_key = (
            hmac_key
            if hmac_key is not None
            else (authority_credential("ELLA_IMESSAGE_BINDING_HMAC_KEY", strip=False) or "").encode()
        )
        self.now = now
        self.runtime_resolver = runtime_resolver
        self.retained_runtime_resolver = retained_runtime_resolver
        self.runtime_revalidator = runtime_revalidator

    @classmethod
    async def create(cls) -> "ImessageRuntimeService":
        return cls(
            repository=await ImessageRuntimeRepository.create(),
            event_store=PostgresCanonicalEventStore(),
            completion_client=SelfHostedHermesCompletionClient(),
        )

    async def heartbeat(self, *, line_identity: str, contact_identity: str, connection_id: str) -> dict[str, Any]:
        await self._require_ready()
        binding = await self._binding(line_identity=line_identity, contact_identity=contact_identity)
        if not binding:
            return {"status": "unknown_sender", "model_invoked": False}
        try:
            _, _, authority = await self._runtime(binding)
            updated = await self.repository.record_heartbeat(
                binding_id=str(binding["id"]),
                generation=int(binding["generation"]),
                connection_ref_hmac=self._reference("connection", connection_id),
                authority=authority,
            )
        except (ImessageRuntimeRepositoryError, ProvisioningError) as exc:
            raise self._error(exc) from exc
        return {
            "status": "ready",
            "binding_generation": int(updated["generation"]),
            "text_dm_only": True,
        }

    async def ingest(self, request: ImessageInbound) -> dict[str, Any]:
        await self._require_ready()
        self._assert_message_shape(request)
        binding = await self._binding(
            line_identity=request.line_identity,
            contact_identity=request.contact_identity,
        )
        if not binding:
            return {
                "status": "unknown_sender",
                "model_invoked": False,
            }
        self._assert_live_connection(binding, request.connection_id)
        runtime, identity, authority = await self._runtime(binding)
        inbound_ref = self._reference("inbound-message", request.provider_message_id)
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "line": str(binding["line_identity_hmac"]),
                    "contact": str(binding["contact_identity_hmac"]),
                    "message": inbound_ref,
                    "text": request.text,
                    "occurred_at": request.occurred_at.astimezone(timezone.utc).isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        try:
            receipt = await self.repository.claim_message(
                binding=binding,
                inbound_provider_ref_hmac=inbound_ref,
                inbound_payload_sha256=payload_hash,
                message_text=request.text,
                occurred_at=request.occurred_at.astimezone(timezone.utc),
                lease_seconds=180,
                authority=authority,
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        if not receipt.get("acquired"):
            return self._receipt_status(receipt, duplicate=True)

        receipt_id = str(receipt["id"])
        lease_token = str(receipt.get("lease_token") or "")
        if not lease_token:
            raise ImessageRuntimeError("imessage_message_claim_conflict", status_code=409, retryable=True)
        source_identity = f"imessage:receipt:{receipt_id}"
        inbound_event_id = f"{source_identity}:user"
        outbound_event_id = f"{source_identity}:assistant"
        memory_key = canonical_omi_session_key(str(binding["omi_uid"]))
        session_key = (
            channel_omi_session_id(str(binding["omi_uid"]), IMESSAGE_CHANNEL)
            if runtime.binding_role == "imessage"
            else memory_key
        )
        model_started = False
        try:
            await self.event_store.write_batch(
                [
                    self._event(
                        binding=binding,
                        source_identity=source_identity,
                        event_id=inbound_event_id,
                        role="user",
                        text=request.text,
                        session_key=session_key,
                        started_at=request.occurred_at.astimezone(timezone.utc),
                    )
                ]
            )
            current_runtime = await self.runtime_revalidator(identity)
            self._assert_runtime_matches(binding, current_runtime)
            current_authority = self._authority(binding, current_runtime, runtime_authority_identity(current_runtime))
            await self.repository.mark_model_started(
                receipt_id=receipt_id,
                lease_token=lease_token,
                authority=current_authority,
            )
            model_started = True
            reply = await self.completion_client.complete(
                runtime=current_runtime,
                user_text=request.text,
                session_key=session_key,
                memory_key=memory_key,
            )
            if not reply.strip() or len(reply) > IMESSAGE_OUTBOUND_MAX_CHARS:
                raise ImessageRuntimeError("imessage_model_response_invalid", status_code=503)
            final_runtime = await self.runtime_revalidator(identity)
            self._assert_runtime_matches(binding, final_runtime)
            final_authority = self._authority(binding, final_runtime, runtime_authority_identity(final_runtime))
            ended_at = self.now()
            await self.event_store.write_batch(
                [
                    self._event(
                        binding=binding,
                        source_identity=source_identity,
                        event_id=outbound_event_id,
                        role="assistant",
                        text=reply,
                        session_key=session_key,
                        started_at=ended_at,
                        ended_at=ended_at,
                    )
                ]
            )
            completed = await self.repository.complete_model(
                receipt_id=receipt_id,
                lease_token=lease_token,
                canonical_inbound_event_id=inbound_event_id,
                canonical_outbound_event_id=outbound_event_id,
                outbound_text=reply,
                authority=final_authority,
            )
            return self._receipt_status(completed, duplicate=False)
        except (ImessageRuntimeError, ImessageRuntimeRepositoryError, ProvisioningError) as exc:
            code = getattr(exc, "code", "imessage_runtime_failed")
            await self.repository.fail_message(
                receipt_id=receipt_id,
                lease_token=lease_token,
                error_code=code,
                uncertain=model_started,
            )
            raise self._error(exc, uncertain=model_started) from exc

    async def start_delivery(self, request: ImessageDeliveryIdentity) -> dict[str, Any]:
        binding, authority = await self._authorized_delivery_binding(request)
        try:
            receipt = await self.repository.start_delivery(
                receipt_id=request.receipt_id,
                delivery_idempotency_key=request.delivery_idempotency_key,
                binding_id=str(binding["id"]),
                generation=request.binding_generation,
                connection_ref_hmac=self._reference("connection", request.connection_id),
                authority=authority,
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        return {
            "status": "sending",
            "receipt_id": str(receipt["id"]),
            "delivery_idempotency_key": str(receipt["delivery_idempotency_key"]),
            "binding_generation": int(receipt["binding_generation"]),
            "text": str(receipt["outbound_text"]),
        }

    async def acknowledge_delivery(
        self,
        request: ImessageDeliveryIdentity,
        *,
        outbound_provider_message_id: str,
    ) -> dict[str, Any]:
        await self._require_storage_ready()
        try:
            receipt = await self.repository.acknowledge_delivery(
                receipt_id=request.receipt_id,
                delivery_idempotency_key=request.delivery_idempotency_key,
                outbound_provider_ref_hmac=self._reference("outbound-message", outbound_provider_message_id),
                binding_generation=request.binding_generation,
                line_identity_hmac=self._reference("line", request.line_identity),
                contact_identity_hmac=self._reference("contact", request.contact_identity),
                connection_ref_hmac=self._reference("connection", request.connection_id),
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        return {"status": "delivered", "receipt_id": str(receipt["id"])}

    async def mark_delivery_uncertain(
        self,
        request: ImessageDeliveryIdentity,
        *,
        error_code: str,
    ) -> dict[str, Any]:
        await self._require_storage_ready()
        try:
            receipt = await self.repository.mark_delivery_uncertain(
                receipt_id=request.receipt_id,
                delivery_idempotency_key=request.delivery_idempotency_key,
                binding_generation=request.binding_generation,
                line_identity_hmac=self._reference("line", request.line_identity),
                contact_identity_hmac=self._reference("contact", request.contact_identity),
                connection_ref_hmac=self._reference("connection", request.connection_id),
                error_code=error_code,
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        return {"status": "uncertain", "receipt_id": str(receipt["id"]), "retryable": False}

    async def reconcile_pre_send_delivery(self, request: ImessageDeliveryIdentity) -> dict[str, Any]:
        """Close an old pre-send receipt without releasing its cached text."""
        await self._require_storage_ready()
        try:
            receipt = await self.repository.reconcile_pre_send_delivery(
                receipt_id=request.receipt_id,
                delivery_idempotency_key=request.delivery_idempotency_key,
                binding_generation=request.binding_generation,
                line_identity_hmac=self._reference("line", request.line_identity),
                contact_identity_hmac=self._reference("contact", request.contact_identity),
                connection_ref_hmac=self._reference("connection", request.connection_id),
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        return {
            "status": str(receipt["status"]),
            "receipt_id": str(receipt["id"]),
            "retryable": False,
        }

    async def deregister(
        self,
        *,
        line_identity: str,
        contact_identity: str,
        connection_id: str,
    ) -> dict[str, Any]:
        await self._require_ready()
        binding = await self._binding(line_identity=line_identity, contact_identity=contact_identity)
        if not binding:
            return {"status": "not_connected"}
        self._assert_live_connection(binding, connection_id)
        try:
            quarantined = await self.repository.quarantine_binding(
                binding_id=str(binding["id"]),
                generation=int(binding["generation"]),
                reason="imessage_transport_deregistered",
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc
        return {"status": "quarantined", "binding_generation": int(quarantined["generation"])}

    async def _authorized_delivery_binding(
        self, request: ImessageDeliveryIdentity
    ) -> tuple[dict[str, Any], ImessageRuntimeAuthority]:
        await self._require_ready()
        binding = await self._binding(
            line_identity=request.line_identity,
            contact_identity=request.contact_identity,
        )
        if not binding:
            raise ImessageRuntimeError("imessage_sender_not_authorized", status_code=403)
        if int(binding["generation"]) != request.binding_generation:
            raise ImessageRuntimeError("imessage_delivery_authority_changed", status_code=409)
        self._assert_live_connection(binding, request.connection_id)
        _, _, authority = await self._runtime(binding)
        return binding, authority

    async def _binding(self, *, line_identity: str, contact_identity: str) -> Optional[dict[str, Any]]:
        try:
            return await self.repository.resolve_binding(
                line_identity_hmac=self._reference("line", line_identity),
                contact_identity_hmac=self._reference("contact", contact_identity),
            )
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc

    async def _runtime(
        self, binding: dict[str, Any]
    ) -> tuple[IsolatedRuntime, CloudRuntimeAuthorityIdentity, ImessageRuntimeAuthority]:
        authority_kind = str(binding.get("runtime_authority_kind") or "")
        if authority_kind == "retained_owner":
            runtime = await self.retained_runtime_resolver(str(binding["omi_uid"]))
        else:
            runtime = await self.runtime_resolver(str(binding["omi_uid"]), target_mode="hermes-chat")
        if runtime is None:
            raise ImessageRuntimeError("imessage_runtime_unavailable", status_code=503, retryable=True)
        self._assert_runtime_matches(binding, runtime)
        identity = runtime_authority_identity(runtime)
        if not hmac.compare_digest(identity.digest, str(binding["runtime_authority_digest"])):
            raise ImessageRuntimeError("imessage_runtime_authority_changed", status_code=409)
        return runtime, identity, self._authority(binding, runtime, identity)

    @staticmethod
    def _authority(
        binding: dict[str, Any],
        runtime: IsolatedRuntime,
        identity: CloudRuntimeAuthorityIdentity,
    ) -> ImessageRuntimeAuthority:
        try:
            authority_kind = str(binding.get("runtime_authority_kind") or "")
            if authority_kind == "target":
                target_id = uuid.UUID(runtime.runtime_target_id)
                target_updated_at = datetime.fromisoformat(runtime.runtime_target_updated_at.replace("Z", "+00:00"))
                if target_updated_at.tzinfo is None or target_updated_at.utcoffset() is None:
                    raise ValueError("runtime target timestamp is naive")
                target_updated_at = target_updated_at.astimezone(timezone.utc)
                entitlement_revision = runtime.target_entitlement_revision
            elif authority_kind == "retained_owner" and retained_owner_uid_configured(runtime.uid):
                if runtime.runtime_target_id or runtime.target_entitlement_revision:
                    raise ValueError("retained runtime unexpectedly has target authority")
                target_id = None
                target_updated_at = None
                entitlement_revision = 0
            else:
                raise ValueError("runtime authority kind is invalid")
            authority = ImessageRuntimeAuthority(
                uid=runtime.uid,
                user_id=uuid.UUID(runtime.account_user_id),
                profile_user_id=uuid.UUID(runtime.profile_user_id),
                runtime_binding_id=uuid.UUID(runtime.binding_id),
                runtime_binding_role=runtime.binding_role,
                runtime_target_id=target_id,
                runtime_authority_kind=authority_kind,
                runtime_binding_revision=runtime.revision,
                runtime_target_entitlement_revision=entitlement_revision,
                runtime_target_updated_at=target_updated_at,
                runtime_authority_digest=identity.digest,
                runtime_agent_id=runtime.agent_id,
                runtime_instance_id=runtime.runtime_instance_id or None,
                runtime_profile_name=runtime.profile_name,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ImessageRuntimeError("imessage_runtime_authority_invalid", status_code=409) from exc
        if authority.user_id != binding["user_id"] or authority.profile_user_id != binding["user_id"]:
            raise ImessageRuntimeError("imessage_runtime_authority_changed", status_code=409)
        return authority

    @staticmethod
    def _assert_runtime_matches(binding: dict[str, Any], runtime: IsolatedRuntime) -> None:
        authority_kind = str(binding.get("runtime_authority_kind") or "")
        target_matches = authority_kind == "target" and (
            runtime.binding_role == "user"
            and str(binding.get("runtime_binding_role") or "") == "user"
            and runtime.runtime_target_mode == "hermes-chat"
            and runtime.runtime_target_id == str(binding["runtime_target_id"])
        )
        retained_matches = authority_kind == "retained_owner" and (
            runtime.binding_role == "imessage"
            and str(binding.get("runtime_binding_role") or "") == "imessage"
            and binding.get("runtime_target_id") is None
            and not runtime.runtime_target_id
            and retained_owner_uid_configured(runtime.uid)
        )
        if (
            runtime.provider != "hermes"
            or runtime.uid != str(binding["omi_uid"])
            or runtime.binding_id != str(binding["runtime_binding_id"])
            or not (target_matches or retained_matches)
        ):
            raise ImessageRuntimeError("imessage_runtime_authority_changed", status_code=409)

    def _assert_live_connection(self, binding: dict[str, Any], connection_id: str) -> None:
        healthy_at = binding.get("last_transport_healthy_at")
        expected = self._reference("connection", connection_id)
        if (
            not isinstance(healthy_at, datetime)
            or healthy_at.astimezone(timezone.utc) < self.now() - timedelta(minutes=2)
            or not hmac.compare_digest(str(binding.get("transport_connection_ref_hmac") or ""), expected)
        ):
            raise ImessageRuntimeError("imessage_transport_not_ready", status_code=503, retryable=True)

    async def _require_ready(self) -> None:
        if not imessage_runtime_enabled():
            raise ImessageRuntimeError("imessage_runtime_disabled", status_code=503)
        await self._require_storage_ready()

    async def _require_storage_ready(self) -> None:
        if len(self.hmac_key) < 32 or self.hmac_key != self.hmac_key.strip():
            raise ImessageRuntimeError("imessage_runtime_key_unavailable", status_code=503)
        try:
            await self.repository.assert_schema_ready()
        except ImessageRuntimeRepositoryError as exc:
            raise self._error(exc) from exc

    def _reference(self, domain: str, value: str) -> str:
        if not value or value != value.strip():
            raise ImessageRuntimeError("imessage_transport_identity_invalid", status_code=400)
        return hmac.new(
            self.hmac_key,
            f"ella-imessage-v1:{domain}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _assert_message_shape(self, request: ImessageInbound) -> None:
        if not request.text.strip() or len(request.text.encode("utf-8")) > 32_768:
            raise ImessageRuntimeError("imessage_message_invalid", status_code=400)
        if request.attachment_count or request.group_message:
            raise ImessageRuntimeError("imessage_message_scope_forbidden", status_code=409)
        if request.occurred_at.tzinfo is None or request.occurred_at.utcoffset() is None:
            raise ImessageRuntimeError("imessage_message_timestamp_invalid", status_code=400)
        occurred_at = request.occurred_at.astimezone(timezone.utc)
        now = self.now().astimezone(timezone.utc)
        if occurred_at < now - timedelta(minutes=10) or occurred_at > now + timedelta(minutes=2):
            raise ImessageRuntimeError("imessage_message_timestamp_invalid", status_code=409)

    @staticmethod
    def _event(
        *,
        binding: dict[str, Any],
        source_identity: str,
        event_id: str,
        role: str,
        text: str,
        session_key: str,
        started_at: datetime,
        ended_at: Optional[datetime] = None,
    ) -> CanonicalEventIn:
        return CanonicalEventIn(
            uid=str(binding["omi_uid"]),
            canonical_identity=str(binding["omi_uid"]),
            event_id=event_id,
            session_id=session_key,
            channel=IMESSAGE_CHANNEL,
            provider="photon",
            role=role,
            text=text,
            started_at=started_at,
            ended_at=ended_at,
            privacy_scope="user_private",
            scan_policy="immediate" if role == "user" else "none",
            source_ref={"source_identity": source_identity, "transport": "photon"},
            metadata={"adapter": "imessage-self-hosted", "event_revision": 1},
        )

    @staticmethod
    def _receipt_status(receipt: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        return {
            "status": str(receipt["status"]),
            "receipt_id": str(receipt["id"]),
            "delivery_idempotency_key": str(receipt["delivery_idempotency_key"]),
            "binding_generation": int(receipt["binding_generation"]),
            "duplicate": duplicate,
            "model_invoked": bool(receipt.get("model_started")),
        }

    @staticmethod
    def _error(exc: Exception, *, uncertain: bool = False) -> ImessageRuntimeError:
        if isinstance(exc, ImessageRuntimeError):
            if uncertain and exc.status_code < 500:
                return ImessageRuntimeError(exc.code, status_code=503, retryable=False)
            return exc
        code = getattr(exc, "code", "imessage_runtime_failed")
        return ImessageRuntimeError(
            str(code),
            status_code=503 if uncertain else 409,
            retryable=False,
        )
