"""Exact self-hosted Hermes chat authority for explicit summary operations."""

from __future__ import annotations

import hmac
from typing import Optional

from database.ella_provisioning import EllaProvisioningRepository
from ella.services import runtime_resolver
from ella.services.ai_consent import assert_current_ai_consent
from ella.services.runtime_errors import ProvisioningError


async def require_summary_runtime(
    uid: str,
    repository: Optional[EllaProvisioningRepository] = None,
) -> runtime_resolver.IsolatedRuntime:
    """Retain capture's compatibility resolver, but verify this lane's result."""
    assert_current_ai_consent(uid)
    runtime = await runtime_resolver.require_isolated_runtime(uid, repository=repository, target_mode="hermes-chat")
    if (
        runtime.uid != uid
        or runtime.provider != "hermes"
        or runtime.runtime_target_mode != "hermes-chat"
        or not runtime.binding_id
        or not runtime.account_user_id
        or not runtime.profile_user_id
    ):
        raise ProvisioningError("summary_runtime_binding_required", retryable=False)
    runtime_resolver.runtime_authority_identity(runtime)
    assert_current_ai_consent(uid)
    return runtime


async def revalidate_summary_runtime_authority(
    identity: runtime_resolver.CloudRuntimeAuthorityIdentity,
    repository: Optional[EllaProvisioningRepository] = None,
) -> runtime_resolver.IsolatedRuntime:
    current = await require_summary_runtime(identity.uid, repository=repository)
    current_identity = runtime_resolver.runtime_authority_identity(current)
    if not hmac.compare_digest(current_identity.digest, identity.digest):
        raise ProvisioningError("summary_runtime_authority_changed", retryable=False)
    return current
