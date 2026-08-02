"""Service-layer consent and rollout authority for invitation redemption."""

from __future__ import annotations

import os

from database.invitations import (
    InvitationPilotAdmission,
    InvitePilotGateDenied,
)
from database.runtime_targets import (
    CLOUD_RUNTIME_MODEL,
    CLOUD_RUNTIME_PROVIDER,
    SELF_HOSTED_RUNTIME_MODEL,
    SELF_HOSTED_RUNTIME_PROVIDER,
)
from ella.services.ai_consent import (
    CURRENT_POLICY_VERSION,
    CURRENT_PROCESSOR_SET_HASH,
    CURRENT_SCOPE_HASH,
    CURRENT_SCOPE_VERSION,
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud_policy import (
    cloud_synthetic_only,
    current_cloud_authority,
)

PILOT_UID_ALLOWLISTS = (
    "ELLA_RUNTIME_BINDINGS_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_SYNTHETIC_UIDS",
    "ELLA_AI_CONSENT_ENFORCEMENT_UIDS",
)
PILOT_GLOBAL_FLAGS_REQUIRED_FALSE = (
    "ELLA_RUNTIME_BINDINGS_ENABLED",
    "ELLA_HERMES_PROVISIONING_ENABLED",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
    "ELLA_AI_CONSENT_ENFORCEMENT_ENABLED",
    "ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED",
    "ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED",
    "ELLA_ISOLATED_VOICE_ROUTING_ENABLED",
    "ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED",
    "ELLA_INVITE_APP_REVIEW_ENABLED",
)

SELF_HOSTED_GLOBAL_FLAGS_REQUIRED_TRUE = (
    "ELLA_SELF_HOSTED_PROVISIONING_ENABLED",
    "ELLA_AI_CONSENT_ENFORCEMENT_ENABLED",
)
SELF_HOSTED_GLOBAL_FLAGS_REQUIRED_FALSE = (
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
    "ELLA_HERMES_CLOUD_SYNTHETIC_UIDS",
    "ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED",
)

TRUE_VALUES = {"1", "true", "yes", "on"}


def _exact_allowlist(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def _global_flag_enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in TRUE_VALUES


def assert_invitation_pilot_rollout(uid: str) -> None:
    """Require the exact synthetic canary while every global rollout remains off."""
    if (
        not uid
        or not cloud_synthetic_only()
        or any(_global_flag_enabled(name) for name in PILOT_GLOBAL_FLAGS_REQUIRED_FALSE)
        or any(uid not in _exact_allowlist(name) for name in PILOT_UID_ALLOWLISTS)
    ):
        raise InvitePilotGateDenied("invite_pilot_identity_not_allowed")


def authorize_invitation_pilot(uid: str) -> InvitationPilotAdmission:
    """Authorize the exact synthetic profile before redemption can mutate SQL."""
    assert_invitation_pilot_rollout(uid)
    try:
        authority = current_cloud_authority(
            uid,
            profile_class="synthetic",
            profile_uid=uid,
            runtime_provider=CLOUD_RUNTIME_PROVIDER,
            model_route=f"openai-codex/{CLOUD_RUNTIME_MODEL}",
            memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
            photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
        )
    except Exception as exc:
        raise InvitePilotGateDenied("invite_pilot_consent_required") from exc
    return InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id=authority.consent_receipt_id,
        profile_binding_id=authority.profile_binding_id,
        policy_version=authority.lineage.policy_version,
        processor_set_hash=authority.lineage.processor_set_hash,
        scope_version=authority.lineage.scope_version,
        scope_hash=authority.lineage.scope_hash,
    )


async def revalidate_invitation_pilot(
    pilot_admission: InvitationPilotAdmission,
) -> InvitationPilotAdmission:
    """Require the initial admission to remain the exact current authority."""
    if not isinstance(pilot_admission, InvitationPilotAdmission):
        raise InvitePilotGateDenied("invite_pilot_authority_changed")
    current_admission = authorize_invitation_pilot(pilot_admission.account_uid)
    if current_admission != pilot_admission:
        raise InvitePilotGateDenied("invite_pilot_authority_changed")
    return current_admission


def assert_self_hosted_invitation_rollout(uid: str) -> None:
    """Require the invite-gated self-hosted rollout and consent enforcement."""
    if (
        not uid
        or not all(_global_flag_enabled(name) for name in SELF_HOSTED_GLOBAL_FLAGS_REQUIRED_TRUE)
        or any(_global_flag_enabled(name) for name in SELF_HOSTED_GLOBAL_FLAGS_REQUIRED_FALSE)
    ):
        raise InvitePilotGateDenied("invite_pilot_identity_not_allowed")


def authorize_self_hosted_invitation(uid: str, verified_email: str) -> InvitationPilotAdmission:
    """Authorize a verified identity for a consent-pending invitation bind."""
    assert_self_hosted_invitation_rollout(uid)
    normalized_email = verified_email.strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise InvitePilotGateDenied("invite_verified_email_required")
    return InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id="",
        profile_binding_id="",
        policy_version=CURRENT_POLICY_VERSION,
        processor_set_hash=CURRENT_PROCESSOR_SET_HASH,
        scope_version=CURRENT_SCOPE_VERSION,
        scope_hash=CURRENT_SCOPE_HASH,
        verified_email=normalized_email,
        required_profile_class="real",
        consent_pending=True,
    )


async def revalidate_self_hosted_invitation(
    pilot_admission: InvitationPilotAdmission,
) -> InvitationPilotAdmission:
    """Require the initial admission to remain the exact current authority."""
    if not isinstance(pilot_admission, InvitationPilotAdmission):
        raise InvitePilotGateDenied("invite_pilot_authority_changed")
    current_admission = authorize_self_hosted_invitation(
        pilot_admission.account_uid,
        pilot_admission.verified_email,
    )
    if current_admission != pilot_admission:
        raise InvitePilotGateDenied("invite_pilot_authority_changed")
    return current_admission
