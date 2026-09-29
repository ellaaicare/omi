"""Fail-closed ordering between Firestore consent and PostgreSQL authority."""

from __future__ import annotations

import os
from typing import Any

from fastapi.concurrency import run_in_threadpool

from database import managed_cloud_consent
from ella.services.ai_consent import (
    AiConsentService,
    ConsentAuthorityUnavailable,
    ConsentSubmission,
    LEGACY_POLICY_VERSION_V10,
    SUPPORTED_CONSENT_POLICY_CONTRACTS,
    consent_policy_contract,
    is_exact_v11_upgrade_decline,
    managed_cloud_real_data_enabled,
    managed_runtime_policy_contracts,
)
from ella.services.provisioning import self_hosted_fresh_uid_relax_enabled


class ArtworkConsentErasureUnavailable(RuntimeError):
    """Content-free denial when revoked artwork bytes cannot be proven absent."""


async def _erase_artwork_for_denial(uid: str) -> None:
    try:
        from utils.ella.memory_artwork_storage import (
            acquire_memory_artwork_publication_lock,
            delete_user_artwork_for_consent,
        )

        async with acquire_memory_artwork_publication_lock(uid) as lock_proof:
            delete_user_artwork_for_consent(uid, lock_proof=lock_proof)
    except Exception as exc:
        raise ArtworkConsentErasureUnavailable("artwork_consent_erasure_unavailable") from exc


def _managed_authority_required(uid: str) -> bool:
    cloud_enabled = os.getenv(
        "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    cloud_uids = {
        value.strip()
        for value in os.getenv(
            "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
            "",
        ).split(",")
        if value.strip()
    }
    self_hosted_enabled = os.getenv(
        "ELLA_SELF_HOSTED_PROVISIONING_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    return managed_cloud_real_data_enabled(uid) or cloud_enabled or uid in cloud_uids or self_hosted_enabled


def _supported_grant_from_status(
    uid: str,
    status: dict[str, Any],
) -> managed_cloud_consent.ManagedCloudGrant | None:
    current = dict(status.get("consent") or {})
    processor_ids = current.get("processor_ids")
    contract = consent_policy_contract(
        current.get("policy_version"),
        current.get("processor_set_hash"),
        current.get("scope_version"),
        current.get("scope_hash"),
    )
    if not (
        status.get("authority_state") == "authorized"
        and current.get("decision") == "granted"
        and contract in SUPPORTED_CONSENT_POLICY_CONTRACTS.values()
        and isinstance(processor_ids, list)
        and tuple(processor_ids) == contract.processor_ids
    ):
        return None
    return managed_cloud_consent.ManagedCloudGrant.from_mapping(uid, current)


def _compatible_runtime_contracts() -> tuple[managed_cloud_consent.ConsentContract, ...]:
    return tuple(
        (
            contract.version,
            contract.processor_set_hash,
            contract.scope_version,
            contract.scope_hash,
        )
        for contract in SUPPORTED_CONSENT_POLICY_CONTRACTS.values()
    )


async def _publish_preserved_runtime_grant(
    *,
    uid: str,
    payload: dict[str, Any],
    service: AiConsentService,
) -> None:
    consent = dict(payload.get("consent") or {})
    grant = managed_cloud_consent.ManagedCloudGrant.from_mapping(uid, consent)

    async def grant_is_current() -> bool:
        status = await run_in_threadpool(service.status, uid)
        current = dict(status.get("consent") or {})
        return bool(
            status.get("authority_state") == "authorized"
            and current.get("decision") == "granted"
            and current.get("receipt_id") == grant.consent_receipt_id
        )

    await managed_cloud_consent.synchronize_grant(
        grant=grant,
        grant_is_current=grant_is_current,
        compatible_runtime_contracts=_compatible_runtime_contracts(),
        preserve_runtime_required=True,
    )


def _retained_v10_authority(payload: dict[str, Any]) -> bool:
    consent = dict(payload.get("consent") or {})
    return consent.get("receipt_kind") == "policy_upgrade_retained_v10"


async def _record_v11_upgrade_decline_if_current_grant(
    *,
    uid: str,
    submission: ConsentSubmission,
    service: AiConsentService,
    managed: bool,
) -> dict[str, Any] | None:
    if not is_exact_v11_upgrade_decline(submission):
        return None

    if await run_in_threadpool(service.is_policy_upgrade_decline_replay, uid, submission):
        payload = await run_in_threadpool(
            service.record_policy_upgrade_decline,
            uid,
            submission,
            expected_current_receipt_id="",
        )
        if managed and payload.get("authorized") is True and _retained_v10_authority(payload):
            await _publish_preserved_runtime_grant(uid=uid, payload=payload, service=service)
        return payload

    status = await run_in_threadpool(service.status, uid)
    if status.get("authority_state") == "unavailable":
        raise ConsentAuthorityUnavailable("ai_consent_authority_unavailable")
    grant = _supported_grant_from_status(uid, status)
    if grant is None:
        if await run_in_threadpool(service.is_policy_upgrade_decline_replay, uid, submission):
            return await run_in_threadpool(
                service.record_policy_upgrade_decline,
                uid,
                submission,
                expected_current_receipt_id="",
            )
        return None

    payload: dict[str, Any] | None = None

    async def record_decline() -> None:
        nonlocal payload
        payload = await run_in_threadpool(
            service.record_policy_upgrade_decline,
            uid,
            submission,
            expected_current_receipt_id=grant.consent_receipt_id,
        )

    if managed:
        preserved = await managed_cloud_consent.run_with_exact_grant_current(
            grant=grant,
            action=record_decline,
            allowed_successor_contracts=(
                _compatible_runtime_contracts() if grant.policy_version == LEGACY_POLICY_VERSION_V10 else ()
            ),
        )
        if not preserved:
            raise ConsentAuthorityUnavailable("ai_consent_upgrade_authority_changed")
    else:
        await record_decline()
    if payload is None:
        raise ConsentAuthorityUnavailable("ai_consent_authority_unavailable")
    if managed and payload.get("authorized") is True and _retained_v10_authority(payload):
        await _publish_preserved_runtime_grant(uid=uid, payload=payload, service=service)
    return payload


async def submit_with_managed_cloud_authority(
    *,
    uid: str,
    submission: ConsentSubmission,
    service: AiConsentService,
    verified_email: str = "",
) -> dict[str, Any]:
    """Record consent with asymmetric ordering that fails closed on partial work.

    Decline/revocation commits PostgreSQL denial and quarantine first, so a
    Firestore error cannot leave usable Cloud authority. A grant records the
    immutable Firestore receipt first and publishes it to PostgreSQL second, so
    a PostgreSQL error cannot create usable authority without a receipt.
    """
    managed = _managed_authority_required(uid)
    upgrade_decline = await _record_v11_upgrade_decline_if_current_grant(
        uid=uid,
        submission=submission,
        service=service,
        managed=managed,
    )
    if upgrade_decline is not None:
        return upgrade_decline
    if managed and submission.decision in {"declined", "revoked"}:
        await managed_cloud_consent.synchronize_denial(
            uid=uid,
            decision=submission.decision,
            verified_email=verified_email,
        )

    payload = service.submit(uid, submission)
    if submission.decision in {"declined", "revoked"}:
        await _erase_artwork_for_denial(uid)
    if managed and submission.decision == "granted":
        receipt = dict(payload.get("receipt") or {})
        receipt_id = str(receipt.get("receipt_id") or "")
        runtime_contracts = managed_runtime_policy_contracts(
            receipt.get("policy_version"),
            receipt.get("processor_set_hash"),
            receipt.get("scope_version"),
            receipt.get("scope_hash"),
        )

        async def grant_is_current() -> bool:
            status = await run_in_threadpool(service.status, uid)
            current = dict(status.get("consent") or {})
            return bool(
                status.get("authority_state") == "authorized"
                and current.get("decision") == "granted"
                and receipt_id
                and current.get("receipt_id") == receipt_id
            )

        await managed_cloud_consent.synchronize_grant(
            grant=managed_cloud_consent.ManagedCloudGrant.from_mapping(
                uid,
                payload.get("receipt"),
            ),
            allow_fresh_uid_bootstrap=self_hosted_fresh_uid_relax_enabled(),
            bootstrap_email=verified_email,
            grant_is_current=grant_is_current,
            compatible_runtime_contracts=tuple(
                (
                    contract.version,
                    contract.processor_set_hash,
                    contract.scope_version,
                    contract.scope_hash,
                )
                for contract in runtime_contracts
            ),
        )
    return payload
