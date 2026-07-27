"""Fail-closed first-party adapter for the Hermes Photon sidecar."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from database.ella_provisioning import EllaProvisioningRepository, RuntimePoolClaimError
from ella.routers.canonical_events import CanonicalEventStore
from ella.services.hermes_cloud import HermesCloudClient
from ella.services.hermes_cloud_policy import cloud_synthetic_only
from ella.services.hermes_cloud_runtime import (
    HermesCloudRuntimeService,
    HermesCloudTurnRequest,
)
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime, resolve_isolated_runtime

PHOTON_CHANNEL = "photon"
PHOTON_COMMAND_TIER_VERSION = "photon-regular-v1"
PHOTON_ALLOWED_REGULAR_COMMANDS = ("/help", "/whoami")
PHOTON_ALLOWED_TOOLS = ("honcho_context", "honcho_reasoning", "honcho_search")
GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
TRUE_VALUES = {"1", "true", "yes", "on"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


@dataclass(frozen=True)
class PhotonAdapterConfig:
    enabled: bool
    internal_owner_uid: str
    identity_hmac_key: str
    approved_policy_commit_sha: str
    command_tier_version: str = PHOTON_COMMAND_TIER_VERSION
    preflight_max_age_seconds: int = 30

    @classmethod
    def from_env(cls) -> "PhotonAdapterConfig":
        return cls(
            enabled=os.getenv("ELLA_HERMES_CLOUD_PHOTON_ENABLED", "false").strip().lower() in TRUE_VALUES,
            internal_owner_uid=os.getenv("ELLA_HERMES_CLOUD_PHOTON_INTERNAL_OWNER_UID", "").strip(),
            identity_hmac_key=os.getenv("ELLA_HERMES_CLOUD_PHOTON_IDENTITY_HMAC_KEY", ""),
            approved_policy_commit_sha=os.getenv("ELLA_HERMES_CLOUD_PHOTON_APPROVED_POLICY_SHA", "").strip().lower(),
            command_tier_version=os.getenv(
                "ELLA_HERMES_CLOUD_PHOTON_COMMAND_TIER_VERSION",
                PHOTON_COMMAND_TIER_VERSION,
            ).strip(),
            preflight_max_age_seconds=int(os.getenv("ELLA_HERMES_CLOUD_PHOTON_PREFLIGHT_MAX_AGE_SECONDS", "30")),
        )

    def assert_ready(self) -> None:
        if not self.enabled:
            raise ProvisioningError("photon_disabled", retryable=False)
        if (
            not self.internal_owner_uid
            or len(self.identity_hmac_key) < 32
            or not GIT_SHA_RE.fullmatch(self.approved_policy_commit_sha)
            or self.command_tier_version != PHOTON_COMMAND_TIER_VERSION
            or not 5 <= self.preflight_max_age_seconds <= 300
        ):
            raise ProvisioningError("photon_control_policy_invalid", retryable=False)

    def opaque_key(self, namespace: str, raw_value: str) -> str:
        value = str(raw_value or "").strip()
        if not value:
            raise ProvisioningError("photon_identity_missing", retryable=False)
        return hmac.new(
            self.identity_hmac_key.encode("utf-8"),
            f"{namespace}\x1f{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True)
class PhotonSidecarPreflight:
    line_identity: str
    contact_identity: str
    connection_id: str
    oauth_expires_at: datetime
    allow_all: bool
    allowed_contact_count: int
    attachments_enabled: bool
    groups_enabled: bool
    command_tier_version: str
    allowed_regular_commands: tuple[str, ...]


@dataclass(frozen=True)
class PhotonInboundEnvelope:
    line_identity: str
    contact_identity: str
    connection_id: str
    provider_message_id: str
    text: str
    occurred_at: datetime
    conversation_initiation: bool = False
    attachment_count: int = 0
    group_message: bool = False
    synthetic: bool = True


@dataclass(frozen=True)
class PhotonDeliveryAck:
    receipt_id: str
    delivery_idempotency_key: str
    connection_id: str
    outbound_provider_message_id: str
    acknowledged_at: datetime


@dataclass(frozen=True)
class PhotonAdapterResult:
    receipt_id: str
    status: str
    duplicate: bool
    delivery_idempotency_key: Optional[str] = None
    outbound_text: Optional[str] = None
    canonical_inbound_event_id: Optional[str] = None
    canonical_outbound_event_id: Optional[str] = None


class HermesCloudPhotonAdapter:
    def __init__(
        self,
        *,
        repository: EllaProvisioningRepository,
        event_store: CanonicalEventStore,
        config: Optional[PhotonAdapterConfig] = None,
        cloud_client: Optional[HermesCloudClient] = None,
        runtime_resolver: Optional[
            Callable[[str, EllaProvisioningRepository], Awaitable[Optional[IsolatedRuntime]]]
        ] = None,
        runtime_service_factory: Optional[Callable[[], HermesCloudRuntimeService]] = None,
    ):
        self.repository = repository
        self.event_store = event_store
        self.config = config or PhotonAdapterConfig.from_env()
        self.cloud_client = cloud_client or HermesCloudClient()
        self.runtime_resolver = runtime_resolver or resolve_isolated_runtime
        self.runtime_service_factory = runtime_service_factory or (
            lambda: HermesCloudRuntimeService(
                repository=self.repository,
                event_store=self.event_store,
                cloud_client=self.cloud_client,
            )
        )

    def _binding_identity_keys(self, line_identity: str, contact_identity: str) -> tuple[str, str]:
        return (
            self.config.opaque_key("photon-line", line_identity),
            self.config.opaque_key("photon-contact", contact_identity),
        )

    async def _binding(
        self,
        *,
        line_identity: str,
        contact_identity: str,
    ) -> dict[str, Any]:
        line_key, contact_key = self._binding_identity_keys(line_identity, contact_identity)
        try:
            binding = await self.repository.resolve_photon_channel_binding(
                line_identity_key=line_key,
                contact_identity_key=contact_key,
            )
        except RuntimePoolClaimError as exc:
            raise ProvisioningError(exc.code, retryable=False) from exc
        if not binding:
            raise ProvisioningError("photon_sender_not_allowed", retryable=False)
        if (
            str(binding.get("omi_uid") or "") != self.config.internal_owner_uid
            or str(binding.get("photon_role") or "") != "internal-owner"
            or str(binding.get("photon_status") or "") != "enabled"
            or binding.get("allow_all") is not False
            or binding.get("attachments_enabled") is not False
            or binding.get("caregiver_delivery_enabled") is not False
            or int(binding.get("rollout_phase") or 0) != 3
            or str(binding.get("provider") or "") != "hermes_cloud"
            or str(binding.get("status") or "") != "internal_canary"
            or binding.get("active") is not True
        ):
            raise ProvisioningError("photon_owner_binding_invalid", retryable=False)
        if (
            str(binding.get("photon_policy_commit_sha") or "") != self.config.approved_policy_commit_sha
            or str(binding.get("command_tier_version") or "") != self.config.command_tier_version
        ):
            raise ProvisioningError("photon_policy_binding_drift", retryable=False)
        message_limit = int(binding.get("daily_message_limit") or 0)
        initiation_limit = int(binding.get("daily_initiation_limit") or 0)
        if not 2 <= message_limit < 5000 or not 0 < initiation_limit < 50:
            raise ProvisioningError("photon_quota_policy_invalid", retryable=False)
        return binding

    async def _runtime(
        self,
        binding: dict[str, Any],
    ) -> IsolatedRuntime:
        runtime = await self.runtime_resolver(self.config.internal_owner_uid, self.repository)
        if (
            runtime is None
            or runtime.provider != "hermes_cloud"
            or runtime.status != "internal_canary"
            or runtime.binding_id != str(binding.get("id") or "")
            or runtime.policy_commit_sha != self.config.approved_policy_commit_sha
            or tuple(runtime.allowed_tools) != PHOTON_ALLOWED_TOOLS
        ):
            raise ProvisioningError("photon_runtime_binding_drift", retryable=False)
        return runtime

    async def preflight(self, request: PhotonSidecarPreflight) -> dict[str, Any]:
        self.config.assert_ready()
        binding = await self._binding(
            line_identity=request.line_identity,
            contact_identity=request.contact_identity,
        )
        runtime = await self._runtime(binding)
        now = _utcnow()
        oauth_expires_at = request.oauth_expires_at.astimezone(timezone.utc)
        if oauth_expires_at <= now + timedelta(seconds=self.config.preflight_max_age_seconds):
            raise ProvisioningError("photon_oauth_expired", retryable=False)
        if (
            request.allow_all
            or request.allowed_contact_count != 1
            or request.attachments_enabled
            or request.groups_enabled
            or request.command_tier_version != self.config.command_tier_version
            or tuple(sorted(request.allowed_regular_commands)) != tuple(sorted(PHOTON_ALLOWED_REGULAR_COMMANDS))
        ):
            raise ProvisioningError("photon_sidecar_policy_drift", retryable=False)

        observed = await self.cloud_client.preflight(binding)
        if (
            observed.model != runtime.expected_model
            or tuple(observed.tools) != PHOTON_ALLOWED_TOOLS
            or tuple(observed.capabilities) != tuple(runtime.required_capabilities)
        ):
            raise ProvisioningError("photon_runtime_preflight_drift", retryable=False)
        connection_key = self.config.opaque_key("photon-connection", request.connection_id)
        receipt = {
            "status": "ok",
            "runtime_binding_id": runtime.binding_id,
            "runtime_revision": runtime.revision,
            "expected_model": runtime.expected_model,
            "policy_commit_sha": runtime.policy_commit_sha,
            "approval_manifest_sha256": runtime.approval_manifest_sha256,
            "tools": list(observed.tools),
            "capabilities": list(observed.capabilities),
            "command_tier_version": self.config.command_tier_version,
            "connection_key": connection_key,
            "verified_at": now.isoformat(),
            "content_free": True,
        }
        await self.repository.record_photon_sidecar_preflight(
            photon_binding_id=str(binding["photon_binding_id"]),
            connection_key=connection_key,
            oauth_expires_at=oauth_expires_at,
            receipt=receipt,
        )
        return receipt

    def _assert_live_preflight(
        self,
        *,
        binding: dict[str, Any],
        connection_id: str,
    ) -> dict[str, Any]:
        now = _utcnow()
        expected_connection_key = self.config.opaque_key("photon-connection", connection_id)
        connected_at = binding.get("sidecar_connected_at")
        oauth_expires_at = binding.get("oauth_expires_at")
        receipt = _json_object(binding.get("photon_preflight_receipt"))
        try:
            verified_at = datetime.fromisoformat(
                str(receipt.get("verified_at") or "").replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError as exc:
            raise ProvisioningError("photon_preflight_missing", retryable=False) from exc
        if (
            binding.get("sidecar_connection_key") != expected_connection_key
            or receipt.get("connection_key") != expected_connection_key
            or not isinstance(connected_at, datetime)
            or connected_at.astimezone(timezone.utc) < now - timedelta(seconds=self.config.preflight_max_age_seconds)
            or verified_at < now - timedelta(seconds=self.config.preflight_max_age_seconds)
            or not isinstance(oauth_expires_at, datetime)
            or oauth_expires_at.astimezone(timezone.utc) <= now
            or receipt.get("status") != "ok"
            or receipt.get("policy_commit_sha") != self.config.approved_policy_commit_sha
            or receipt.get("command_tier_version") != self.config.command_tier_version
            or tuple(receipt.get("tools") or ()) != PHOTON_ALLOWED_TOOLS
        ):
            raise ProvisioningError("photon_sidecar_not_ready", retryable=False)
        return receipt

    @staticmethod
    def _assert_message_shape(request: PhotonInboundEnvelope) -> None:
        text = request.text.strip()
        if not text or len(text.encode("utf-8")) > 32768:
            raise ProvisioningError("photon_message_invalid", retryable=False)
        if request.attachment_count or request.group_message:
            raise ProvisioningError("photon_message_scope_forbidden", retryable=False)
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0].lower()
            if command not in PHOTON_ALLOWED_REGULAR_COMMANDS:
                raise ProvisioningError("photon_command_forbidden", retryable=False)

    @staticmethod
    def _source_identity(uid: str, client_interaction_id: str) -> str:
        digest = hashlib.sha256(f"{uid}|{PHOTON_CHANNEL}|{client_interaction_id}".encode("utf-8")).hexdigest()[:32]
        return f"hermes_cloud:{PHOTON_CHANNEL}:interaction:{digest}"

    async def _replay(self, receipt: dict[str, Any]) -> PhotonAdapterResult:
        status = str(receipt.get("status") or "")
        if status == "delivered":
            return PhotonAdapterResult(
                receipt_id=str(receipt["id"]),
                status="delivered",
                duplicate=True,
                canonical_inbound_event_id=receipt.get("canonical_inbound_event_id"),
                canonical_outbound_event_id=receipt.get("canonical_outbound_event_id"),
            )
        if status != "awaiting_delivery":
            raise ProvisioningError(
                "photon_message_uncertain" if status in {"failed", "uncertain"} else "photon_message_in_progress",
                retryable=status in {"claimed", "running"},
            )
        writeback = _json_object(receipt.get("writeback_receipt"))
        source_identity = str(writeback.get("source_identity") or "")
        canonical_outbound_event_id = str(receipt.get("canonical_outbound_event_id") or "")
        stored = await self.event_store.get_event(
            uid=self.config.internal_owner_uid,
            event_id=canonical_outbound_event_id,
            source_identity=source_identity,
        )
        if not stored:
            raise ProvisioningError("photon_writeback_receipt_missing", retryable=False)
        return PhotonAdapterResult(
            receipt_id=str(receipt["id"]),
            status="awaiting_delivery",
            duplicate=True,
            delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
            outbound_text=str(stored.get("text") or ""),
            canonical_inbound_event_id=receipt.get("canonical_inbound_event_id"),
            canonical_outbound_event_id=canonical_outbound_event_id,
        )

    async def handle_inbound(
        self,
        request: PhotonInboundEnvelope,
    ) -> PhotonAdapterResult:
        self.config.assert_ready()
        self._assert_message_shape(request)
        if cloud_synthetic_only() and not request.synthetic:
            raise ProvisioningError(
                "photon_real_data_not_authorized",
                retryable=False,
            )
        binding = await self._binding(
            line_identity=request.line_identity,
            contact_identity=request.contact_identity,
        )
        runtime = await self._runtime(binding)
        preflight_receipt = self._assert_live_preflight(
            binding=binding,
            connection_id=request.connection_id,
        )
        inbound_provider_key = self.config.opaque_key("photon-inbound-message", request.provider_message_id)
        payload_sha256 = hashlib.sha256(
            _stable_json(
                {
                    "line_identity_key": binding["line_identity_key"],
                    "contact_identity_key": binding["contact_identity_key"],
                    "inbound_provider_message_key": inbound_provider_key,
                    "text": request.text,
                    "occurred_at": request.occurred_at.astimezone(timezone.utc).isoformat(),
                    "conversation_initiation": request.conversation_initiation,
                    "synthetic": request.synthetic,
                }
            ).encode("utf-8")
        ).hexdigest()
        try:
            receipt = await self.repository.claim_photon_message(
                photon_binding_id=str(binding["photon_binding_id"]),
                inbound_provider_message_key=inbound_provider_key,
                inbound_payload_sha256=payload_sha256,
                command_tier_version=self.config.command_tier_version,
            )
        except RuntimePoolClaimError as exc:
            raise ProvisioningError(exc.code, retryable=False) from exc
        if not receipt.get("inserted"):
            return await self._replay(receipt)

        receipt_id = str(receipt["id"])
        provider_started = False
        try:
            await self.repository.reserve_photon_quota(
                receipt_id=receipt_id,
                photon_binding_id=str(binding["photon_binding_id"]),
                message_limit=int(binding["daily_message_limit"]),
                initiation_limit=int(binding["daily_initiation_limit"]),
                conversation_initiation=request.conversation_initiation,
            )
            runtime_request = HermesCloudTurnRequest(
                uid=self.config.internal_owner_uid,
                client_interaction_id=f"photon:{receipt_id}",
                correlation_id=f"photon:{receipt_id}",
                channel=PHOTON_CHANNEL,
                user_input=request.text,
                instructions=(
                    "Use the installed internal-owner policy. Treat this as a Photon "
                    "transport turn only. Do not message another recipient, invoke a "
                    "mutating tool, process attachments, or perform caregiver delivery."
                ),
                started_at=request.occurred_at.astimezone(timezone.utc),
                client_metadata={
                    "synthetic": request.synthetic,
                    "provider_message_key": inbound_provider_key,
                    "photon_receipt_id": receipt_id,
                    "content_free_identifiers": True,
                },
            )
            provider_started = True
            result = await self.runtime_service_factory().run_turn(runtime, runtime_request)
            source_identity = self._source_identity(
                self.config.internal_owner_uid,
                runtime_request.client_interaction_id,
            )
            writeback_receipt = {
                "status": "written",
                "source_identity": source_identity,
                "canonical_inbound_event_id": result.canonical_user_event_id,
                "canonical_outbound_event_id": result.canonical_assistant_event_id,
                "runtime_interaction_id": result.runtime_interaction_id,
                "content_free": True,
            }
            completed = await self.repository.complete_photon_message(
                receipt_id=receipt_id,
                runtime_interaction_id=result.runtime_interaction_id,
                canonical_inbound_event_id=result.canonical_user_event_id,
                canonical_outbound_event_id=result.canonical_assistant_event_id,
                runtime_revision=runtime.revision,
                expected_model=runtime.expected_model,
                policy_commit_sha=runtime.policy_commit_sha,
                usage=result.usage,
                preflight_receipt=preflight_receipt,
                writeback_receipt=writeback_receipt,
            )
            refreshed_binding = await self._binding(
                line_identity=request.line_identity,
                contact_identity=request.contact_identity,
            )
            self._assert_live_preflight(
                binding=refreshed_binding,
                connection_id=request.connection_id,
            )
            return PhotonAdapterResult(
                receipt_id=receipt_id,
                status="awaiting_delivery",
                duplicate=False,
                delivery_idempotency_key=str(completed["delivery_idempotency_key"]),
                outbound_text=result.text,
                canonical_inbound_event_id=result.canonical_user_event_id,
                canonical_outbound_event_id=result.canonical_assistant_event_id,
            )
        except ProvisioningError as exc:
            await self.repository.fail_photon_message(
                receipt_id=receipt_id,
                error_code=exc.code,
                uncertain=provider_started,
                provider_started=provider_started,
            )
            raise
        except Exception as exc:
            await self.repository.fail_photon_message(
                receipt_id=receipt_id,
                error_code="photon_adapter_failed",
                uncertain=provider_started,
                provider_started=provider_started,
            )
            raise ProvisioningError(
                "photon_adapter_failed",
                retryable=not provider_started,
            ) from exc

    async def acknowledge_delivery(
        self,
        request: PhotonDeliveryAck,
    ) -> PhotonAdapterResult:
        self.config.assert_ready()
        receipt = await self.repository.get_photon_message_receipt(receipt_id=request.receipt_id)
        if not receipt:
            raise ProvisioningError("photon_delivery_receipt_missing", retryable=False)
        connection_key = self.config.opaque_key("photon-connection", request.connection_id)
        if (
            receipt.get("photon_status") != "enabled"
            or receipt.get("sidecar_connection_key") != connection_key
            or not isinstance(receipt.get("oauth_expires_at"), datetime)
            or receipt["oauth_expires_at"].astimezone(timezone.utc) <= _utcnow()
        ):
            raise ProvisioningError("photon_sidecar_not_ready", retryable=False)
        outbound_provider_key = self.config.opaque_key("photon-outbound-message", request.outbound_provider_message_id)
        try:
            completed = await self.repository.acknowledge_photon_delivery(
                receipt_id=request.receipt_id,
                delivery_idempotency_key=request.delivery_idempotency_key,
                outbound_provider_message_key=outbound_provider_key,
                delivery_receipt={
                    "status": "acknowledged",
                    "acknowledged_at": request.acknowledged_at.astimezone(timezone.utc).isoformat(),
                    "outbound_provider_message_key": outbound_provider_key,
                    "content_free": True,
                },
            )
        except RuntimePoolClaimError as exc:
            raise ProvisioningError(exc.code, retryable=False) from exc
        return PhotonAdapterResult(
            receipt_id=str(completed["id"]),
            status="delivered",
            duplicate=str(receipt.get("status") or "") == "delivered",
            canonical_inbound_event_id=completed.get("canonical_inbound_event_id"),
            canonical_outbound_event_id=completed.get("canonical_outbound_event_id"),
        )
