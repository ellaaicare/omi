from datetime import datetime, timedelta, timezone

import pytest

from ella.services.generated_images import (
    GeneratedImageAdmissionError,
    GeneratedImageAdmissionRequest,
    GeneratedImageAuthority,
    GeneratedImageConsentGrant,
    GeneratedImageJobState,
    GeneratedImageSourceSnapshot,
    GeneratedImageStoredOutput,
    GeneratedImageSubjectKind,
    admit_generated_image_job,
    bind_generated_image_output,
    confirm_generated_image_receipt,
    sha256_digest,
    start_generated_image_job,
)
from ella.services.today_card import TodayCardPresentation
from models.generated_image import GeneratedImageAssetRef

NOW = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
PROMPT = "A non-photoreal watercolor scene grounded in the selected memory."


def _source(*, generation: int = 4, source_version_id: str = "summary-v4") -> GeneratedImageSourceSnapshot:
    return GeneratedImageSourceSnapshot(
        authority=GeneratedImageAuthority(
            owner_uid="owner-fixture",
            profile_binding_id="profile-binding-fixture",
            authority_generation=7,
        ),
        kind=GeneratedImageSubjectKind.memory,
        subject_id="memory-card-fixture",
        conversation_id="conversation-fixture",
        memory_id="memory-fixture",
        source_version_id=source_version_id,
        source_digest=sha256_digest("grounded-source-fixture"),
        grounding_receipt_digest=sha256_digest("grounding-receipt-fixture"),
        generation=generation,
    )


def _request(source: GeneratedImageSourceSnapshot | None = None, *, provider_id: str = "image-provider-fixture"):
    return GeneratedImageAdmissionRequest(
        source=source or _source(),
        prompt_contract_version="ella.memory-image.prompt.v1",
        prompt_digest=sha256_digest(PROMPT),
        provider_id=provider_id,
        processor_id="image-processor-fixture",
        processor_name="Named Image Processor Fixture",
        model_id="image-model-fixture-v1",
        consent_policy_version="image-data-processors-v1",
        consent_processor_set_hash=sha256_digest("processor-set-fixture"),
        consent_scope_version="generated-memory-images-v1",
        consent_scope_hash=sha256_digest("scope-fixture"),
    )


def _consent(request: GeneratedImageAdmissionRequest | None = None, **updates) -> GeneratedImageConsentGrant:
    request = request or _request()
    values = {
        "receipt_id": "consent-receipt-fixture",
        "decision": "granted",
        "subject_uid": request.source.authority.owner_uid,
        "profile_binding_id": request.source.authority.profile_binding_id,
        "authority_generation": request.source.authority.authority_generation,
        "provider_id": request.provider_id,
        "processor_id": request.processor_id,
        "processor_name": request.processor_name,
        "model_id": request.model_id,
        "policy_version": "image-data-processors-v1",
        "processor_set_hash": sha256_digest("processor-set-fixture"),
        "scope_version": "generated-memory-images-v1",
        "scope_hash": sha256_digest("scope-fixture"),
        "decided_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(updates)
    return GeneratedImageConsentGrant(**values)


def _admit(request: GeneratedImageAdmissionRequest | None = None):
    request = request or _request()
    return admit_generated_image_job(
        request,
        consent=_consent(request),
        current_source=request.source,
        now=NOW,
    )


def _output(*, moderation_status="approved") -> GeneratedImageStoredOutput:
    return GeneratedImageStoredOutput(
        asset_id="00000000-0000-4000-8000-000000000001",
        generation_request_id="provider-request-fixture",
        storage_key="private/generated-images/fixture/output.webp",
        media_type="image/webp",
        sha256=sha256_digest(b"generated-image-fixture"),
        width=1024,
        height=768,
        moderation_status=moderation_status,
        alt_text="A gentle watercolor scene inspired by the selected memory.",
    )


@pytest.mark.parametrize("decision", [None, "declined", "revoked"])
def test_admission_requires_an_exact_granted_image_consent_receipt(decision):
    request = _request()
    consent = None if decision is None else _consent(request, decision=decision)

    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_consent_required"):
        admit_generated_image_job(request, consent=consent, current_source=request.source, now=NOW)


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"subject_uid": "different-owner"}, "generated_image_consent_authority_mismatch"),
        ({"authority_generation": 8}, "generated_image_consent_authority_mismatch"),
        ({"processor_name": "Different Processor"}, "generated_image_consent_processor_mismatch"),
        ({"model_id": "different-model"}, "generated_image_consent_processor_mismatch"),
        ({"scope_hash": sha256_digest("different-scope")}, "generated_image_consent_processor_mismatch"),
        ({"expires_at": NOW}, "generated_image_consent_expired"),
    ],
)
def test_admission_fails_closed_on_authority_processor_or_expiry_drift(updates, code):
    request = _request()
    with pytest.raises(GeneratedImageAdmissionError, match=code):
        admit_generated_image_job(
            request,
            consent=_consent(request, **updates),
            current_source=request.source,
            now=NOW,
        )


def test_admission_rejects_stale_sources_and_codex_development_provider():
    request = _request()
    stale = _source(generation=5)
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_source_stale"):
        admit_generated_image_job(request, consent=_consent(request), current_source=stale, now=NOW)

    development_request = _request(provider_id="codex-imagegen")
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_development_provider_forbidden"):
        admit_generated_image_job(
            development_request,
            consent=_consent(development_request),
            current_source=development_request.source,
            now=NOW,
        )


def test_job_binds_prompt_provider_request_output_and_canonical_receipt_before_attachment():
    job = _admit()
    assert job.state == GeneratedImageJobState.queued
    assert job.consent_receipt_ref.startswith("sha256:")

    started, provider_request = start_generated_image_job(
        job,
        consent=_consent(),
        prompt=PROMPT,
        generation_request_id="provider-request-fixture",
        current_source=job.source,
        expected_generation=4,
        now=NOW,
    )
    assert started.state == GeneratedImageJobState.generating
    assert provider_request.model_dump() == {
        "job_id": job.job_id,
        "generation_request_id": "provider-request-fixture",
        "provider_id": "image-provider-fixture",
        "model_id": "image-model-fixture-v1",
    }

    awaiting, pending_receipt = bind_generated_image_output(
        started,
        output=_output(),
        current_source=job.source,
        expected_generation=4,
        now=NOW,
    )
    assert awaiting.state == GeneratedImageJobState.awaiting_canonical
    assert pending_receipt is not None
    assert pending_receipt.canonical_status == "pending"

    mismatched_receipt = pending_receipt.model_copy(update={"source": _source(source_version_id="summary-v5")})
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_receipt_binding_mismatch"):
        confirm_generated_image_receipt(
            awaiting,
            mismatched_receipt,
            _output(),
            canonical_event_id="canonical-event-fixture",
            current_source=job.source,
            expected_generation=4,
            now=NOW,
        )

    ready, confirmed_receipt, asset = confirm_generated_image_receipt(
        awaiting,
        pending_receipt,
        _output(),
        canonical_event_id="canonical-event-fixture",
        current_source=job.source,
        expected_generation=4,
        now=NOW,
    )
    assert ready.state == GeneratedImageJobState.ready
    assert confirmed_receipt.canonical_status == "confirmed"
    assert asset.moderation_status == "approved"
    assert asset.delivery_path.endswith(asset.asset_id)
    assert "storage_key" not in asset.model_dump()
    assert "provider" not in asset.delivery_path


def test_generation_and_prompt_cas_rejects_stale_or_changed_work():
    job = _admit()
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_prompt_digest_mismatch"):
        start_generated_image_job(
            job,
            consent=_consent(),
            prompt="changed prompt",
            generation_request_id="provider-request-fixture",
            current_source=job.source,
            expected_generation=4,
            now=NOW,
        )
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_generation_stale"):
        start_generated_image_job(
            job,
            consent=_consent(),
            prompt=PROMPT,
            generation_request_id="provider-request-fixture",
            current_source=_source(generation=5),
            expected_generation=4,
            now=NOW,
        )


def test_rejected_moderation_never_creates_a_receipt():
    job = _admit()
    started, _ = start_generated_image_job(
        job,
        consent=_consent(),
        prompt=PROMPT,
        generation_request_id="provider-request-fixture",
        current_source=job.source,
        expected_generation=4,
        now=NOW,
    )
    rejected, receipt = bind_generated_image_output(
        started,
        output=_output(moderation_status="rejected"),
        current_source=job.source,
        expected_generation=4,
        now=NOW,
    )
    assert rejected.state == GeneratedImageJobState.rejected
    assert receipt is None


def test_pre_egress_check_rejects_revoked_or_different_consent_receipt():
    job = _admit()
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_consent_required"):
        start_generated_image_job(
            job,
            consent=_consent(decision="revoked"),
            prompt=PROMPT,
            generation_request_id="provider-request-fixture",
            current_source=job.source,
            expected_generation=4,
            now=NOW,
        )
    with pytest.raises(GeneratedImageAdmissionError, match="generated_image_consent_receipt_mismatch"):
        start_generated_image_job(
            job,
            consent=_consent(receipt_id="different-consent-receipt"),
            prompt=PROMPT,
            generation_request_id="provider-request-fixture",
            current_source=job.source,
            expected_generation=4,
            now=NOW,
        )


def test_memory_and_daily_note_fields_are_optional_and_accept_only_attachable_assets():
    assert TodayCardPresentation().background_image is None
    asset = GeneratedImageAssetRef(
        asset_id="asset-fixture",
        job_id="job-fixture",
        receipt_id="receipt-fixture",
        generation=1,
        media_type="image/webp",
        sha256=sha256_digest("asset-fixture"),
        width=100,
        height=100,
        alt_text="Watercolor fallback fixture.",
        delivery_path="/v1/ella/generated-image-assets/asset-fixture",
    )
    assert TodayCardPresentation(background_image=asset).background_image == asset

    with pytest.raises(ValueError, match="generated_image_delivery_asset_mismatch"):
        GeneratedImageAssetRef(
            **{
                **asset.model_dump(),
                "delivery_path": "/v1/ella/generated-image-assets/different-asset",
            }
        )
