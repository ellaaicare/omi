"""Fail-closed ordering between Firestore consent and PostgreSQL authority."""

from __future__ import annotations

import os
from typing import Any

from database import managed_cloud_consent
from ella.services.ai_consent import (
    AiConsentService,
    ConsentSubmission,
    managed_cloud_real_data_enabled,
)


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


async def submit_with_managed_cloud_authority(
    *,
    uid: str,
    submission: ConsentSubmission,
    service: AiConsentService,
) -> dict[str, Any]:
    """Record consent with asymmetric ordering that fails closed on partial work.

    Decline/revocation commits PostgreSQL denial and quarantine first, so a
    Firestore error cannot leave usable Cloud authority. A grant records the
    immutable Firestore receipt first and publishes it to PostgreSQL second, so
    a PostgreSQL error cannot create usable authority without a receipt.
    """
    managed = _managed_authority_required(uid)
    if managed and submission.decision in {"declined", "revoked"}:
        await managed_cloud_consent.synchronize_denial(
            uid=uid,
            decision=submission.decision,
        )

    payload = service.submit(uid, submission)
    if managed and submission.decision == "granted":
        await managed_cloud_consent.synchronize_grant(
            grant=managed_cloud_consent.ManagedCloudGrant.from_mapping(
                uid,
                payload.get("receipt"),
            )
        )
    return payload
