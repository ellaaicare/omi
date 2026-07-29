"""Service-layer consent and rollout authority for invitation redemption."""

from __future__ import annotations

import os

from database.invitations import (
    InvitationPilotAdmission,
    InvitePilotGateDenied,
)
from database.runtime_targets import CLOUD_RUNTIME_MODEL, CLOUD_RUNTIME_PROVIDER
from ella.services.ai_consent import (
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud_policy import (
    cloud_synthetic_only,
    current_cloud_authority,
)


def _exact_allowlist(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def authorize_invitation_pilot(uid: str) -> InvitationPilotAdmission:
    """Authorize the exact synthetic profile before redemption can mutate SQL."""
    if (
        not cloud_synthetic_only()
        or uid not in _exact_allowlist("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS")
        or uid not in _exact_allowlist("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS")
    ):
        raise InvitePilotGateDenied("invite_pilot_identity_not_allowed")
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
