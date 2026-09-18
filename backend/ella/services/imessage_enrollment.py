"""Fail-closed owner enrollment for the self-hosted Photon text-DM lane."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlparse

import httpx

from database.imessage_enrollment import (
    ImessageAuthorityError,
    ImessageConsentContract,
    ImessageConsentInput,
    ImessageEnrollmentRepository,
    ImessageRuntimeSnapshot,
)
from database.honcho_attestation import authority_credential, retain_authority_credential
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import (
    IsolatedRuntime,
    resolve_isolated_runtime,
    revalidate_runtime_authority,
    runtime_authority_identity,
)

CONTRACT_ID = "ella.imessage_enrollment.v1"
CONSENT_RECEIPT_SCHEMA = "ella.imessage_consent_receipt.v1"
CONSENT_POLICY_VERSION = "ella-imessage-data-v1"
CONSENT_SCOPE_VERSION = "ella.imessage_text_dm.v1"
CANONICAL_PROCESSORS = "ella-self-hosted-hermes:reasoning|honcho-self-hosted:memory|photon:imessage-transport"
CONSENT_PROCESSOR_SET_HASH = f"sha256:{hashlib.sha256(CANONICAL_PROCESSORS.encode()).hexdigest()}"
CANONICAL_SCOPE = (
    "channel=imessage|mode=text_dm|groups=false|attachments=false|caregiver=false|"
    "owner=firebase_exact_subject|runtime=hermes-chat|fallback=none"
)
CONSENT_SCOPE_HASH = f"sha256:{hashlib.sha256(CANONICAL_SCOPE.encode()).hexdigest()}"
CONSENT_CONTRACT = ImessageConsentContract(
    policy_version=CONSENT_POLICY_VERSION,
    processor_set_hash=CONSENT_PROCESSOR_SET_HASH,
    scope_version=CONSENT_SCOPE_VERSION,
    scope_hash=CONSENT_SCOPE_HASH,
)

_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_CODE_RE = re.compile(r"^[0-9]{6}$")
_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")


class ImessageEnrollmentError(RuntimeError):
    """Content-free typed service failure."""

    def __init__(self, code: str, *, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class RegistrarError(RuntimeError):
    def __init__(self, code: str, *, ambiguous: bool) -> None:
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


@dataclass(frozen=True)
class RegistrarResult:
    registration_ref: str
    assigned_destination: str


class Registrar(Protocol):
    async def register(
        self,
        *,
        handset_e164: str,
        provider_request_id: str,
    ) -> RegistrarResult: ...


class UnavailableRegistrar:
    def __init__(self, code: str = "imessage_registrar_not_configured") -> None:
        self.code = code

    async def register(self, *, handset_e164: str, provider_request_id: str) -> RegistrarResult:
        del handset_e164, provider_request_id
        raise RegistrarError(self.code, ambiguous=False)


class PhotonRegistrarClient:
    """Fixed-route registrar client; owner and runtime selectors are never sent."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = 8.0) -> None:
        parsed = urlparse(base_url)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        if (
            not base_url
            or len(token) < 32
            or token != token.strip()
            or (parsed.scheme != "https" and not loopback_http)
        ):
            raise RegistrarError("imessage_registrar_not_configured", ambiguous=False)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RegistrarError("imessage_registrar_authority_invalid", ambiguous=False)
        self._endpoint = f"{base_url.rstrip('/')}/v1/registrations"
        self._token = retain_authority_credential(token) or ""
        self._timeout = httpx.Timeout(timeout_seconds)

    @classmethod
    def from_environment(cls) -> "PhotonRegistrarClient":
        return cls(
            base_url=os.getenv("ELLA_IMESSAGE_REGISTRAR_URL", "").strip(),
            token=authority_credential("ELLA_IMESSAGE_REGISTRAR_TOKEN", strip=False) or "",
        )

    async def register(self, *, handset_e164: str, provider_request_id: str) -> RegistrarResult:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Idempotency-Key": provider_request_id,
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(
                        {"channel": "imessage", "mode": "text_dm", "handset_e164": handset_e164},
                        separators=(",", ":"),
                    ),
                ) as response:
                    if response.status_code not in {200, 201}:
                        ambiguous = response.status_code >= 500
                        raise RegistrarError(
                            "imessage_registrar_response_uncertain" if ambiguous else "imessage_registrar_rejected",
                            ambiguous=ambiguous,
                        )
                    chunks: list[bytes] = []
                    response_size = 0
                    async for chunk in response.aiter_bytes():
                        response_size += len(chunk)
                        if response_size > 32_768:
                            raise RegistrarError("imessage_registrar_response_invalid", ambiguous=True)
                        chunks.append(chunk)
                    response_content = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise RegistrarError("imessage_registrar_transport_uncertain", ambiguous=True) from exc
        try:
            body = json.loads(response_content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RegistrarError("imessage_registrar_response_invalid", ambiguous=True) from exc
        if not isinstance(body, dict):
            raise RegistrarError("imessage_registrar_response_invalid", ambiguous=True)
        registration_ref = str(body.get("registration_id") or "")
        assigned_destination = str(body.get("assigned_destination") or "")
        if not registration_ref or not _E164_RE.fullmatch(assigned_destination):
            raise RegistrarError("imessage_registrar_response_invalid", ambiguous=True)
        return RegistrarResult(
            registration_ref=registration_ref,
            assigned_destination=assigned_destination,
        )


def enrollment_enabled() -> bool:
    return os.getenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "false").strip().lower() == "true"


def consent_policy() -> dict[str, Any]:
    return {
        "policy_version": CONSENT_POLICY_VERSION,
        "processor_set_hash": CONSENT_PROCESSOR_SET_HASH,
        "scope_version": CONSENT_SCOPE_VERSION,
        "scope_hash": CONSENT_SCOPE_HASH,
        "recipients": [
            "Ella self-hosted Hermes and Honcho",
            "Photon iMessage transport",
        ],
        "data_classes": [
            "the text messages you send to Ella",
            "Ella's text replies",
            "messaging delivery identifiers",
        ],
        "text_dm_only": True,
    }


class ImessageEnrollmentService:
    def __init__(
        self,
        *,
        repository: ImessageEnrollmentRepository,
        registrar: Registrar,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        hmac_key: Optional[bytes] = None,
        proof_key: Optional[bytes] = None,
    ) -> None:
        self.repository = repository
        self.registrar = registrar
        self.now = now
        self.hmac_key = (
            hmac_key
            if hmac_key is not None
            else (authority_credential("ELLA_IMESSAGE_BINDING_HMAC_KEY", strip=False) or "").encode()
        )
        self.proof_key = (
            proof_key
            if proof_key is not None
            else (authority_credential("ELLA_IMESSAGE_PROOF_KEY", strip=False) or "").encode()
        )

    @classmethod
    async def create(cls) -> "ImessageEnrollmentService":
        try:
            registrar: Registrar = PhotonRegistrarClient.from_environment()
        except RegistrarError as exc:
            registrar = UnavailableRegistrar(exc.code)
        return cls(
            repository=await ImessageEnrollmentRepository.create(),
            registrar=registrar,
        )

    async def submit_consent(
        self,
        *,
        uid: str,
        decision: str,
        policy_version: str,
        processor_set_hash: str,
        scope_version: str,
        scope_hash: str,
        request_id: uuid.UUID,
        app_version: str,
        build_number: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        await self._require_schema()
        if (
            policy_version != CONSENT_POLICY_VERSION
            or processor_set_hash != CONSENT_PROCESSOR_SET_HASH
            or scope_version != CONSENT_SCOPE_VERSION
            or scope_hash != CONSENT_SCOPE_HASH
        ):
            raise ImessageEnrollmentError("imessage_consent_policy_mismatch", status_code=409)
        try:
            receipt = await self.repository.submit_consent(
                uid=uid,
                submission=ImessageConsentInput(
                    request_id=request_id,
                    decision=decision,
                    policy_version=policy_version,
                    processor_set_hash=processor_set_hash,
                    scope_version=scope_version,
                    scope_hash=scope_hash,
                    app_version=app_version,
                    build_number=build_number,
                ),
            )
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc
        return self._receipt(receipt)

    async def status(self, *, uid: str) -> dict[str, Any]:
        if not enrollment_enabled():
            return self._status(state="temporarily_unavailable", reason="rollout_disabled")
        self._require_keys()
        await self._require_schema()
        try:
            state = await self.repository.get_owner_state(uid=uid)
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc
        if str(state.get("user_status") or "") != "ACTIVE":
            raise ImessageEnrollmentError("imessage_owner_not_active", status_code=403)
        decision = str(state.get("consent_decision") or "")
        if not decision:
            return self._status(state="not_connected", reason="consent_required")
        if decision == "revoked":
            return self._status(state="revoked", reason="consent_revoked")
        if decision != "granted":
            return self._status(state="not_connected", reason="consent_required")
        if (
            str(state.get("policy_version") or "") != CONSENT_POLICY_VERSION
            or str(state.get("processor_set_hash") or "") != CONSENT_PROCESSOR_SET_HASH
            or str(state.get("scope_version") or "") != CONSENT_SCOPE_VERSION
            or str(state.get("scope_hash") or "") != CONSENT_SCOPE_HASH
        ):
            return self._status(state="not_connected", reason="consent_policy_stale")
        binding_status = str(state.get("binding_status") or "")
        if not binding_status:
            return self._status(state="not_connected", reason="not_enrolled")
        if binding_status == "revoked":
            return self._binding_status(state, status="revoked", reason="binding_revoked")
        if binding_status == "quarantined":
            return self._binding_status(state, status="temporarily_unavailable", reason="binding_quarantined")
        if binding_status == "verification_pending":
            challenge_expires_at = state.get("challenge_expires_at")
            if isinstance(challenge_expires_at, datetime) and challenge_expires_at <= self.now():
                try:
                    binding = await self.repository.retire_expired_pending_binding(
                        uid=uid,
                        binding_id=state["binding_id"],
                        now=self.now(),
                    )
                except ImessageAuthorityError as exc:
                    raise self._authority_error(exc) from exc
                if str(binding.get("status") or "") == "quarantined":
                    return self._binding_status(
                        binding,
                        status="temporarily_unavailable",
                        reason="binding_quarantined",
                    )
                state = {
                    **state,
                    **binding,
                    "binding_id": binding.get("id"),
                    "binding_status": binding.get("status"),
                    "binding_revision": binding.get("revision"),
                }
                binding_status = str(binding.get("status") or "")
                if binding_status == "revoked":
                    return self._binding_status(state, status="revoked", reason="binding_revoked")
                if binding_status not in {"verification_pending", "active"}:
                    return self._binding_status(
                        state,
                        status="temporarily_unavailable",
                        reason="binding_quarantined",
                    )
            if binding_status == "verification_pending":
                return self._binding_status(state, status="verification_pending", reason="verification_pending")
        try:
            runtime = await self._runtime(uid)
        except ImessageEnrollmentError:
            return self._binding_status(state, status="temporarily_unavailable", reason="runtime_unavailable")
        if not hmac.compare_digest(str(state.get("runtime_authority_digest") or ""), runtime[1].authority_digest):
            return self._binding_status(state, status="temporarily_unavailable", reason="authority_stale")
        healthy_at = state.get("last_transport_healthy_at")
        max_age = max(30, int(os.getenv("ELLA_IMESSAGE_HEALTH_MAX_AGE_SECONDS", "300")))
        if not isinstance(healthy_at, datetime) or healthy_at < self.now() - timedelta(seconds=max_age):
            return self._binding_status(state, status="temporarily_unavailable", reason="transport_unhealthy")
        return self._binding_status(state, status="ready", reason="ready")

    async def start(
        self,
        *,
        uid: str,
        handset_e164: str,
        consent_receipt_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> tuple[dict[str, Any], bool]:
        self._require_enabled()
        self._require_keys()
        await self._require_schema()
        if not _E164_RE.fullmatch(handset_e164):
            raise ImessageEnrollmentError("imessage_handset_invalid", status_code=422)
        _, runtime_snapshot, runtime_identity = await self._runtime(uid)
        handset_ref = self._reference("handset", handset_e164)
        try:
            attempt, created = await self.repository.prepare_registration(
                uid=uid,
                idempotency_key=idempotency_key,
                handset_ref_hmac=handset_ref,
                consent_receipt_id=consent_receipt_id,
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime_snapshot,
            )
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc
        if str(attempt["state"]) in {"uncertain", "quarantined"}:
            raise ImessageEnrollmentError("imessage_registration_manual_reconciliation_required", status_code=503)
        if str(attempt["state"]) == "failed":
            raise ImessageEnrollmentError("imessage_registration_failed", status_code=409)
        if str(attempt["state"]) == "finalized":
            binding = await self.repository.get_binding_for_attempt(uid=uid, attempt_id=attempt["id"])
            if not binding:
                raise ImessageEnrollmentError("imessage_registration_finalization_incomplete", status_code=503)
            binding_status = str(binding.get("status") or "")
            if binding_status == "active":
                raise ImessageEnrollmentError("imessage_binding_already_exists", status_code=409)
            if binding_status != "verification_pending":
                raise ImessageEnrollmentError("imessage_registration_state_invalid", status_code=409)
            challenge_expires_at = binding.get("challenge_expires_at")
            if not isinstance(challenge_expires_at, datetime) or challenge_expires_at <= self.now():
                try:
                    await self.repository.retire_expired_pending_binding(
                        uid=uid,
                        binding_id=binding["id"],
                        now=self.now(),
                    )
                except ImessageAuthorityError as exc:
                    raise self._authority_error(exc) from exc
                raise ImessageEnrollmentError("imessage_registration_proof_window_expired", status_code=409)
            code = self._challenge_code(attempt["id"])
            return (
                {
                    "status": self._binding_status(
                        binding,
                        status="verification_pending",
                        reason="verification_pending",
                    ),
                    "proof": {"code": code, "expires_at": binding["challenge_expires_at"].isoformat()},
                },
                False,
            )
        if str(attempt["state"]) == "provider_accepted":
            provider_registration_ref_hmac = str(attempt["provider_registration_ref_hmac"])
            assigned_destination = str(attempt["assigned_destination_e164"])
            assigned_destination_ref_hmac = str(attempt["assigned_destination_ref_hmac"])
        else:
            try:
                registered = await self.registrar.register(
                    handset_e164=handset_e164,
                    provider_request_id=str(attempt["provider_request_id"]),
                )
            except RegistrarError as exc:
                try:
                    if exc.ambiguous:
                        await self.repository.mark_registration_uncertain(uid=uid, attempt_id=attempt["id"])
                    else:
                        await self.repository.mark_registration_failed(
                            uid=uid,
                            attempt_id=attempt["id"],
                            error_code=exc.code,
                        )
                except ImessageAuthorityError:
                    pass
                raise ImessageEnrollmentError(exc.code, status_code=503 if exc.ambiguous else 409) from exc
            provider_registration_ref_hmac = self._reference("registration", registered.registration_ref)
            assigned_destination = registered.assigned_destination
            assigned_destination_ref_hmac = self._reference("destination", registered.assigned_destination)
            try:
                attempt = await self.repository.mark_provider_accepted(
                    uid=uid,
                    attempt_id=attempt["id"],
                    provider_registration_ref_hmac=provider_registration_ref_hmac,
                    assigned_destination_e164=assigned_destination,
                    assigned_destination_ref_hmac=assigned_destination_ref_hmac,
                )
            except ImessageAuthorityError as exc:
                try:
                    await self.repository.mark_registration_uncertain(uid=uid, attempt_id=attempt["id"])
                except ImessageAuthorityError:
                    pass
                raise self._authority_error(exc) from exc
        try:
            await revalidate_runtime_authority(runtime_identity)
        except Exception as exc:
            try:
                await self.repository.mark_registration_uncertain(uid=uid, attempt_id=attempt["id"])
            except ImessageAuthorityError:
                pass
            raise ImessageEnrollmentError("imessage_runtime_authority_changed", status_code=503) from exc
        code = self._challenge_code(attempt["id"])
        salt = secrets.token_hex(16)
        challenge_hash = self._challenge_hash(salt=salt, code=code)
        expires_at = attempt["created_at"] + timedelta(minutes=15)
        if expires_at <= self.now():
            try:
                await self.repository.mark_registration_uncertain(uid=uid, attempt_id=attempt["id"])
            except ImessageAuthorityError:
                pass
            raise ImessageEnrollmentError("imessage_registration_proof_window_expired", status_code=503)
        try:
            binding, binding_created = await self.repository.finalize_registration(
                uid=uid,
                attempt_id=attempt["id"],
                runtime=runtime_snapshot,
                provider_registration_ref_hmac=provider_registration_ref_hmac,
                assigned_destination_e164=assigned_destination,
                assigned_destination_ref_hmac=assigned_destination_ref_hmac,
                challenge_salt=salt,
                challenge_hash=challenge_hash,
                challenge_expires_at=expires_at,
                consent_contract=CONSENT_CONTRACT,
            )
        except Exception as exc:
            try:
                await self.repository.mark_registration_uncertain(uid=uid, attempt_id=attempt["id"])
            except ImessageAuthorityError:
                pass
            if isinstance(exc, ImessageAuthorityError):
                raise self._authority_error(exc) from exc
            raise ImessageEnrollmentError("imessage_registration_finalization_incomplete", status_code=503) from exc
        return (
            {
                "status": self._binding_status(binding, status="verification_pending", reason="verification_pending"),
                "proof": {"code": code, "expires_at": binding["challenge_expires_at"].isoformat()},
            },
            created and binding_created,
        )

    async def verify_proof(
        self,
        *,
        assigned_destination: str,
        handset_e164: str,
        code: str,
        provider_message_id: str,
        line_identity: str,
        contact_identity: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        self._require_keys()
        await self._require_schema()
        if (
            not _E164_RE.fullmatch(assigned_destination)
            or not _E164_RE.fullmatch(handset_e164)
            or not _CODE_RE.fullmatch(code)
            or not all((provider_message_id, line_identity, contact_identity))
        ):
            raise ImessageEnrollmentError("imessage_proof_invalid", status_code=422)
        assigned_destination_ref_hmac = self._reference("destination", assigned_destination)
        handset_ref_hmac = self._reference("handset", handset_e164)
        try:
            proof_authority = await self.repository.resolve_proof_authority(
                assigned_destination_ref_hmac=assigned_destination_ref_hmac,
                handset_ref_hmac=handset_ref_hmac,
            )
            _, runtime, _ = await self._runtime(str(proof_authority["omi_uid"]))
            if (
                proof_authority["runtime_binding_id"] != runtime.binding_id
                or proof_authority["runtime_target_id"] != runtime.target_id
                or not hmac.compare_digest(
                    str(proof_authority["runtime_authority_digest"]),
                    runtime.authority_digest,
                )
            ):
                raise ImessageEnrollmentError("imessage_runtime_authority_changed", status_code=503)
            binding = await self.repository.verify_inbound_proof(
                assigned_destination_ref_hmac=assigned_destination_ref_hmac,
                handset_ref_hmac=handset_ref_hmac,
                line_identity_hmac=self._reference("line", line_identity),
                contact_identity_hmac=self._reference("contact", contact_identity),
                provider_message_ref_hmac=self._reference("proof-message", provider_message_id),
                candidate_challenge_hash=self._challenge_hash(
                    salt=str(proof_authority["challenge_salt"]),
                    code=code,
                ),
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
                now=self.now(),
            )
        except ImessageEnrollmentError:
            raise
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc
        return {
            "schema_version": "ella.imessage_proof_receipt.v1",
            "status": "accepted",
            "authority_generation": int(binding["generation"]),
        }

    async def revoke(
        self,
        *,
        uid: str,
        expected_generation: int,
        idempotency_key: uuid.UUID,
    ) -> dict[str, Any]:
        self._require_enabled()
        self._require_keys()
        await self._require_schema()
        try:
            binding = await self.repository.revoke_binding(
                uid=uid,
                expected_generation=expected_generation,
                idempotency_key=idempotency_key,
            )
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc
        if not binding:
            return self._status(state="not_connected", reason="not_enrolled")
        return self._binding_status(binding, status="revoked", reason="binding_revoked")

    async def _runtime(self, uid: str) -> tuple[IsolatedRuntime, ImessageRuntimeSnapshot, Any]:
        try:
            runtime = await resolve_isolated_runtime(uid, target_mode="hermes-chat")
            if runtime is None or runtime.provider != "hermes":
                raise ProvisioningError("self_hosted_runtime_required", retryable=False)
            identity = runtime_authority_identity(runtime)
            snapshot = ImessageRuntimeSnapshot(
                binding_id=uuid.UUID(runtime.binding_id),
                target_id=uuid.UUID(runtime.runtime_target_id),
                authority_digest=identity.digest,
                binding_revision=runtime.revision,
                entitlement_revision=runtime.target_entitlement_revision,
                account_user_id=uuid.UUID(runtime.account_user_id),
                profile_user_id=uuid.UUID(runtime.profile_user_id),
            )
        except Exception as exc:
            raise ImessageEnrollmentError("imessage_runtime_unavailable", status_code=503) from exc
        return runtime, snapshot, identity

    async def _require_schema(self) -> None:
        try:
            await self.repository.assert_schema_ready()
        except ImessageAuthorityError as exc:
            raise self._authority_error(exc) from exc

    def _require_enabled(self) -> None:
        if not enrollment_enabled():
            raise ImessageEnrollmentError("imessage_enrollment_disabled", status_code=503)

    def _require_keys(self) -> None:
        if (
            len(self.hmac_key) < 32
            or len(self.proof_key) < 32
            or self.hmac_key != self.hmac_key.strip()
            or self.proof_key != self.proof_key.strip()
            or hmac.compare_digest(self.hmac_key, self.proof_key)
        ):
            raise ImessageEnrollmentError("imessage_enrollment_key_unavailable", status_code=503)

    def _reference(self, domain: str, value: str) -> str:
        return hmac.new(self.hmac_key, f"ella-imessage-v1:{domain}:{value}".encode(), hashlib.sha256).hexdigest()

    def _challenge_code(self, attempt_id: uuid.UUID) -> str:
        digest = hmac.new(self.proof_key, f"ella-imessage-proof-v1:{attempt_id}".encode(), hashlib.sha256).digest()
        return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"

    def _challenge_hash(self, *, salt: str, code: str) -> str:
        return hmac.new(
            self.proof_key,
            f"ella-imessage-proof-hash-v1:{salt}:{code}".encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": CONSENT_RECEIPT_SCHEMA,
            "receipt_id": str(receipt["id"]),
            "decision": str(receipt["decision"]),
            "policy_version": str(receipt["policy_version"]),
            "processor_set_hash": str(receipt["processor_set_hash"]),
            "scope_version": str(receipt["scope_version"]),
            "scope_hash": str(receipt["scope_hash"]),
            "authority_revision": int(receipt["authority_revision"]),
            "decided_at": receipt["decided_at"].isoformat(),
        }

    @staticmethod
    def _status(*, state: str, reason: str) -> dict[str, Any]:
        return {
            "schema_version": CONTRACT_ID,
            "state": state,
            "reason_code": reason,
            "authority_generation": 0,
            "binding_revision": None,
            "binding_fingerprint": None,
            "assigned_destination": None,
            "last_verified_at": None,
            "verification_expires_at": None,
            "support_code": None,
            "features": {
                "text_dm": state == "ready",
                "groups": False,
                "attachments": False,
                "caregiver_delivery": False,
            },
        }

    @classmethod
    def _binding_status(cls, binding: dict[str, Any], *, status: str, reason: str) -> dict[str, Any]:
        binding_id = str(binding.get("binding_id") or binding.get("id") or "")
        response = cls._status(state=status, reason=reason)
        response.update(
            {
                "authority_generation": int(binding.get("generation") or 0),
                "binding_revision": int(binding.get("binding_revision") or binding.get("revision") or 1),
                "binding_fingerprint": hashlib.sha256(binding_id.encode()).hexdigest()[:16] if binding_id else None,
                "assigned_destination": binding.get("assigned_destination_e164"),
                "last_verified_at": (
                    binding["verified_at"].isoformat() if isinstance(binding.get("verified_at"), datetime) else None
                ),
                "verification_expires_at": (
                    binding["challenge_expires_at"].isoformat()
                    if isinstance(binding.get("challenge_expires_at"), datetime)
                    else None
                ),
            }
        )
        response["features"]["text_dm"] = status == "ready"
        return response

    @staticmethod
    def _authority_error(exc: ImessageAuthorityError) -> ImessageEnrollmentError:
        if exc.code in {
            "imessage_consent_required",
            "imessage_consent_receipt_stale",
            "imessage_consent_authority_changed",
            "imessage_consent_policy_stale",
            "imessage_consent_idempotency_conflict",
            "imessage_enrollment_idempotency_conflict",
            "imessage_binding_already_exists",
            "imessage_binding_generation_conflict",
            "imessage_registration_state_invalid",
            "imessage_registration_failed",
            "imessage_identity_already_bound",
            "imessage_provider_identity_conflict",
            "imessage_provider_acceptance_conflict",
        }:
            return ImessageEnrollmentError(exc.code, status_code=409)
        if exc.code in {
            "imessage_owner_not_active",
            "imessage_runtime_owner_mismatch",
            "imessage_proof_invalid",
            "imessage_proof_replayed",
        }:
            return ImessageEnrollmentError(exc.code, status_code=403)
        if exc.code in {"imessage_proof_expired", "imessage_proof_binding_not_found"}:
            return ImessageEnrollmentError(exc.code, status_code=409)
        if exc.code == "imessage_enrollment_rate_limited":
            return ImessageEnrollmentError(exc.code, status_code=429)
        return ImessageEnrollmentError(exc.code, status_code=503)
