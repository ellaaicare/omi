"""Authority-bound foundation for generated memory and Daily Note imagery.

This module has no provider implementation and performs no network or storage
I/O. It defines the admission, provider, asset, CAS, and receipt boundaries a
future server-side worker must satisfy before any user content can leave Ella.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.generated_image import GENERATED_IMAGE_DELIVERY_PREFIX, GeneratedImageAssetRef

GENERATED_IMAGE_JOB_CONTRACT_VERSION = "ella.generated_image.job.v1"
GENERATED_IMAGE_RECEIPT_CONTRACT_VERSION = "ella.generated_image.receipt.v1"
_DEVELOPMENT_ONLY_PROVIDER_IDS = {"codex", "codex-imagegen", "imagegen"}


class GeneratedImageAdmissionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GeneratedImageJobState(str, Enum):
    pending_consent = "pending_consent"
    queued = "queued"
    generating = "generating"
    moderating = "moderating"
    awaiting_canonical = "awaiting_canonical"
    ready = "ready"
    rejected = "rejected"
    stale = "stale"
    failed = "failed"
    cancelled = "cancelled"


class GeneratedImageSubjectKind(str, Enum):
    memory = "memory"
    daily_note = "daily_note"


class GeneratedImageAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_uid: str = Field(min_length=1, max_length=256)
    profile_binding_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)


class GeneratedImageSourceSnapshot(BaseModel):
    """Exact grounded source identity captured before prompt construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: GeneratedImageAuthority
    kind: GeneratedImageSubjectKind
    subject_id: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = Field(default=None, max_length=256)
    memory_id: str | None = Field(default=None, max_length=256)
    today_card_id: str | None = Field(default=None, max_length=256)
    source_version_id: str = Field(min_length=1, max_length=256)
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    grounding_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation: int = Field(ge=1)

    def model_post_init(self, __context: object) -> None:
        if self.kind == GeneratedImageSubjectKind.memory:
            if (not self.conversation_id and not self.memory_id) or self.today_card_id:
                raise ValueError("generated_image_memory_subject_invalid")
        elif not self.today_card_id or self.conversation_id or self.memory_id:
            raise ValueError("generated_image_daily_note_subject_invalid")


class GeneratedImageConsentGrant(BaseModel):
    """Server-read consent receipt projected into the exact egress scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str = Field(min_length=1, max_length=128)
    decision: Literal["granted", "declined", "revoked"]
    subject_uid: str = Field(min_length=1, max_length=256)
    profile_binding_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)
    provider_id: str = Field(min_length=1, max_length=128)
    processor_id: str = Field(min_length=1, max_length=128)
    processor_name: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    processor_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scope_version: str = Field(min_length=1, max_length=128)
    scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decided_at: datetime
    expires_at: datetime


class GeneratedImageAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: GeneratedImageSourceSnapshot
    prompt_contract_version: str = Field(min_length=1, max_length=128)
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=128)
    processor_id: str = Field(min_length=1, max_length=128)
    processor_name: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=256)
    consent_policy_version: str = Field(min_length=1, max_length=128)
    consent_processor_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_scope_version: str = Field(min_length=1, max_length=128)
    consent_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class GeneratedImageJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ella.generated_image.job.v1"] = GENERATED_IMAGE_JOB_CONTRACT_VERSION
    job_id: str = Field(min_length=1, max_length=128)
    source: GeneratedImageSourceSnapshot
    prompt_contract_version: str
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_id: str
    processor_id: str
    processor_name: str
    model_id: str
    consent_receipt_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_policy_version: str
    consent_processor_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_scope_version: str
    consent_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: GeneratedImageJobState
    provider_request_id: str | None = Field(default=None, max_length=256)
    output_asset_id: str | None = Field(default=None, max_length=128)
    output_asset_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    moderation_status: Literal["pending", "approved", "rejected", "failed"] = "pending"
    receipt_id: str | None = Field(default=None, max_length=128)
    canonical_event_id: str | None = Field(default=None, max_length=256)
    created_at: datetime
    updated_at: datetime


class GeneratedImageProviderRequest(BaseModel):
    """In-memory provider request; prompt text must never be logged or stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    generation_request_id: str
    provider_id: str
    model_id: str
    prompt: str = Field(min_length=1, max_length=8000, repr=False, exclude=True)


class GeneratedImageProviderOutput(BaseModel):
    """Provider output before private first-party storage and moderation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_request_id: str
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    content: bytes = Field(min_length=1, repr=False, exclude=True)
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)


class GeneratedImageProvider(Protocol):
    """Implemented only by a future server-side, consent-gated adapter."""

    provider_id: str
    processor_id: str

    async def generate(self, request: GeneratedImageProviderRequest) -> GeneratedImageProviderOutput: ...


class GeneratedImageStoredOutput(BaseModel):
    """Private storage result after the provider bytes have been discarded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1, max_length=128)
    generation_request_id: str = Field(min_length=1, max_length=256)
    storage_key: str = Field(min_length=1, max_length=1024)
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    moderation_status: Literal["approved", "rejected", "failed"]
    alt_text: str = Field(min_length=1, max_length=500)


class GeneratedImageReceipt(BaseModel):
    """Content-free receipt; attachability requires canonical confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ella.generated_image.receipt.v1"] = GENERATED_IMAGE_RECEIPT_CONTRACT_VERSION
    receipt_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    source: GeneratedImageSourceSnapshot
    prompt_contract_version: str
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_id: str
    processor_id: str
    processor_name: str
    model_id: str
    generation_request_id: str
    output_asset_id: str
    output_asset_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    moderation_status: Literal["approved"] = "approved"
    consent_receipt_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_policy_version: str
    consent_processor_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_scope_version: str
    consent_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    canonical_status: Literal["pending", "confirmed", "failed"]
    canonical_event_id: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _canonical_shape(self) -> "GeneratedImageReceipt":
        if self.canonical_status == "confirmed" and not self.canonical_event_id:
            raise ValueError("generated_image_canonical_event_required")
        if self.canonical_status != "confirmed" and self.canonical_event_id:
            raise ValueError("generated_image_canonical_event_not_confirmed")
        return self


def sha256_digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def consent_receipt_ref(owner_uid: str, receipt_id: str) -> str:
    return sha256_digest(f"ella-generated-image-consent-v1\x1f{owner_uid}\x1f{receipt_id}")


def _job_id(request: GeneratedImageAdmissionRequest) -> str:
    source = request.source
    material = "\x1f".join(
        (
            GENERATED_IMAGE_JOB_CONTRACT_VERSION,
            source.authority.owner_uid,
            source.authority.profile_binding_id,
            str(source.authority.authority_generation),
            source.kind.value,
            source.subject_id,
            source.conversation_id or "",
            source.memory_id or "",
            source.today_card_id or "",
            source.source_version_id,
            source.source_digest,
            source.grounding_receipt_digest,
            str(source.generation),
            request.prompt_contract_version,
            request.prompt_digest,
            request.provider_id,
            request.processor_id,
            request.model_id,
            request.consent_policy_version,
            request.consent_processor_set_hash,
            request.consent_scope_version,
            request.consent_scope_hash,
        )
    )
    return "igj_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _source_is_current(expected: GeneratedImageSourceSnapshot, current: GeneratedImageSourceSnapshot) -> bool:
    return expected == current


def _validate_consent(
    *,
    source: GeneratedImageSourceSnapshot,
    consent: GeneratedImageConsentGrant | None,
    provider_id: str,
    processor_id: str,
    processor_name: str,
    model_id: str,
    policy_version: str,
    processor_set_hash: str,
    scope_version: str,
    scope_hash: str,
    now: datetime,
    expected_receipt_ref: str | None = None,
) -> GeneratedImageConsentGrant:
    if consent is None or consent.decision != "granted":
        raise GeneratedImageAdmissionError("generated_image_consent_required")
    if (
        consent.decided_at.tzinfo is None
        or consent.decided_at > now
        or consent.expires_at.tzinfo is None
        or consent.expires_at <= now
    ):
        raise GeneratedImageAdmissionError("generated_image_consent_expired")
    authority = source.authority
    if (
        consent.subject_uid != authority.owner_uid
        or consent.profile_binding_id != authority.profile_binding_id
        or consent.authority_generation != authority.authority_generation
    ):
        raise GeneratedImageAdmissionError("generated_image_consent_authority_mismatch")
    if (
        consent.provider_id != provider_id
        or consent.processor_id != processor_id
        or consent.processor_name != processor_name
        or consent.model_id != model_id
        or consent.policy_version != policy_version
        or consent.processor_set_hash != processor_set_hash
        or consent.scope_version != scope_version
        or consent.scope_hash != scope_hash
    ):
        raise GeneratedImageAdmissionError("generated_image_consent_processor_mismatch")
    receipt_ref = consent_receipt_ref(authority.owner_uid, consent.receipt_id)
    if expected_receipt_ref is not None and receipt_ref != expected_receipt_ref:
        raise GeneratedImageAdmissionError("generated_image_consent_receipt_mismatch")
    return consent


def admit_generated_image_job(
    request: GeneratedImageAdmissionRequest,
    *,
    consent: GeneratedImageConsentGrant | None,
    current_source: GeneratedImageSourceSnapshot,
    now: datetime | None = None,
) -> GeneratedImageJob:
    """Admit only an exact current source under an exact image-specific grant."""

    admitted_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if request.provider_id.casefold() in _DEVELOPMENT_ONLY_PROVIDER_IDS:
        raise GeneratedImageAdmissionError("generated_image_development_provider_forbidden")
    if not _source_is_current(request.source, current_source):
        raise GeneratedImageAdmissionError("generated_image_source_stale")
    consent = _validate_consent(
        source=request.source,
        consent=consent,
        provider_id=request.provider_id,
        processor_id=request.processor_id,
        processor_name=request.processor_name,
        model_id=request.model_id,
        policy_version=request.consent_policy_version,
        processor_set_hash=request.consent_processor_set_hash,
        scope_version=request.consent_scope_version,
        scope_hash=request.consent_scope_hash,
        now=admitted_at,
    )
    authority = request.source.authority

    return GeneratedImageJob(
        job_id=_job_id(request),
        source=request.source,
        prompt_contract_version=request.prompt_contract_version,
        prompt_digest=request.prompt_digest,
        provider_id=request.provider_id,
        processor_id=request.processor_id,
        processor_name=request.processor_name,
        model_id=request.model_id,
        consent_receipt_ref=consent_receipt_ref(authority.owner_uid, consent.receipt_id),
        consent_policy_version=request.consent_policy_version,
        consent_processor_set_hash=request.consent_processor_set_hash,
        consent_scope_version=request.consent_scope_version,
        consent_scope_hash=request.consent_scope_hash,
        state=GeneratedImageJobState.queued,
        created_at=admitted_at,
        updated_at=admitted_at,
    )


def start_generated_image_job(
    job: GeneratedImageJob,
    *,
    consent: GeneratedImageConsentGrant | None,
    prompt: str,
    generation_request_id: str,
    current_source: GeneratedImageSourceSnapshot,
    expected_generation: int,
    now: datetime | None = None,
) -> tuple[GeneratedImageJob, GeneratedImageProviderRequest]:
    if job.state != GeneratedImageJobState.queued:
        raise GeneratedImageAdmissionError("generated_image_job_not_queued")
    if expected_generation != job.source.generation or not _source_is_current(job.source, current_source):
        raise GeneratedImageAdmissionError("generated_image_generation_stale")
    updated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _validate_consent(
        source=job.source,
        consent=consent,
        provider_id=job.provider_id,
        processor_id=job.processor_id,
        processor_name=job.processor_name,
        model_id=job.model_id,
        policy_version=job.consent_policy_version,
        processor_set_hash=job.consent_processor_set_hash,
        scope_version=job.consent_scope_version,
        scope_hash=job.consent_scope_hash,
        now=updated_at,
        expected_receipt_ref=job.consent_receipt_ref,
    )
    if sha256_digest(prompt) != job.prompt_digest:
        raise GeneratedImageAdmissionError("generated_image_prompt_digest_mismatch")
    request_id = generation_request_id.strip()
    if not request_id:
        raise GeneratedImageAdmissionError("generated_image_request_id_required")
    started = job.model_copy(
        update={
            "state": GeneratedImageJobState.generating,
            "provider_request_id": request_id,
            "updated_at": updated_at,
        }
    )
    provider_request = GeneratedImageProviderRequest(
        job_id=job.job_id,
        generation_request_id=request_id,
        provider_id=job.provider_id,
        model_id=job.model_id,
        prompt=prompt,
    )
    return started, provider_request


def bind_generated_image_output(
    job: GeneratedImageJob,
    *,
    output: GeneratedImageStoredOutput,
    current_source: GeneratedImageSourceSnapshot,
    expected_generation: int,
    now: datetime | None = None,
) -> tuple[GeneratedImageJob, GeneratedImageReceipt | None]:
    if job.state not in {GeneratedImageJobState.generating, GeneratedImageJobState.moderating}:
        raise GeneratedImageAdmissionError("generated_image_job_not_generating")
    if expected_generation != job.source.generation or not _source_is_current(job.source, current_source):
        raise GeneratedImageAdmissionError("generated_image_generation_stale")
    if not job.provider_request_id or output.generation_request_id != job.provider_request_id:
        raise GeneratedImageAdmissionError("generated_image_provider_request_mismatch")
    updated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if output.moderation_status != "approved":
        rejected = job.model_copy(
            update={
                "state": GeneratedImageJobState.rejected,
                "moderation_status": output.moderation_status,
                "updated_at": updated_at,
            }
        )
        return rejected, None

    receipt_id = (
        "igir_" + hashlib.sha256(f"{job.job_id}\x1f{output.asset_id}\x1f{output.sha256}".encode("utf-8")).hexdigest()
    )
    pending = job.model_copy(
        update={
            "state": GeneratedImageJobState.awaiting_canonical,
            "output_asset_id": output.asset_id,
            "output_asset_digest": output.sha256,
            "moderation_status": "approved",
            "receipt_id": receipt_id,
            "updated_at": updated_at,
        }
    )
    receipt = GeneratedImageReceipt(
        receipt_id=receipt_id,
        job_id=job.job_id,
        source=job.source,
        prompt_contract_version=job.prompt_contract_version,
        prompt_digest=job.prompt_digest,
        provider_id=job.provider_id,
        processor_id=job.processor_id,
        processor_name=job.processor_name,
        model_id=job.model_id,
        generation_request_id=output.generation_request_id,
        output_asset_id=output.asset_id,
        output_asset_digest=output.sha256,
        consent_receipt_ref=job.consent_receipt_ref,
        consent_policy_version=job.consent_policy_version,
        consent_processor_set_hash=job.consent_processor_set_hash,
        consent_scope_version=job.consent_scope_version,
        consent_scope_hash=job.consent_scope_hash,
        canonical_status="pending",
        created_at=updated_at,
    )
    return pending, receipt


def confirm_generated_image_receipt(
    job: GeneratedImageJob,
    receipt: GeneratedImageReceipt,
    output: GeneratedImageStoredOutput,
    *,
    canonical_event_id: str,
    current_source: GeneratedImageSourceSnapshot,
    expected_generation: int,
    now: datetime | None = None,
) -> tuple[GeneratedImageJob, GeneratedImageReceipt, GeneratedImageAssetRef]:
    if job.state != GeneratedImageJobState.awaiting_canonical or receipt.canonical_status != "pending":
        raise GeneratedImageAdmissionError("generated_image_receipt_not_pending")
    if expected_generation != job.source.generation or not _source_is_current(job.source, current_source):
        raise GeneratedImageAdmissionError("generated_image_generation_stale")
    if (
        receipt.job_id != job.job_id
        or receipt.receipt_id != job.receipt_id
        or receipt.source != job.source
        or receipt.prompt_contract_version != job.prompt_contract_version
        or receipt.prompt_digest != job.prompt_digest
        or receipt.provider_id != job.provider_id
        or receipt.processor_id != job.processor_id
        or receipt.processor_name != job.processor_name
        or receipt.model_id != job.model_id
        or receipt.consent_receipt_ref != job.consent_receipt_ref
        or receipt.consent_policy_version != job.consent_policy_version
        or receipt.consent_processor_set_hash != job.consent_processor_set_hash
        or receipt.consent_scope_version != job.consent_scope_version
        or receipt.consent_scope_hash != job.consent_scope_hash
        or receipt.output_asset_id != output.asset_id
        or receipt.output_asset_digest != output.sha256
        or receipt.generation_request_id != output.generation_request_id
        or output.moderation_status != "approved"
    ):
        raise GeneratedImageAdmissionError("generated_image_receipt_binding_mismatch")
    event_id = canonical_event_id.strip()
    if not event_id:
        raise GeneratedImageAdmissionError("generated_image_canonical_event_required")
    updated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    confirmed_receipt = receipt.model_copy(update={"canonical_status": "confirmed", "canonical_event_id": event_id})
    ready_job = job.model_copy(
        update={
            "state": GeneratedImageJobState.ready,
            "canonical_event_id": event_id,
            "updated_at": updated_at,
        }
    )
    asset = GeneratedImageAssetRef(
        asset_id=output.asset_id,
        job_id=job.job_id,
        receipt_id=receipt.receipt_id,
        generation=job.source.generation,
        media_type=output.media_type,
        sha256=output.sha256,
        width=output.width,
        height=output.height,
        alt_text=output.alt_text,
        delivery_path=GENERATED_IMAGE_DELIVERY_PREFIX + output.asset_id,
    )
    return ready_job, confirmed_receipt, asset
