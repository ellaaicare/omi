"""Read-only supplemental processing hold; never grants source/model authority.

Authenticated callers supply their server-derived UID. Existing consent, exact
runtime/source/lease/CAS guards remain mandatory. No import I/O or cached verdict.
"""
from datetime import datetime, timezone
import asyncio
from ella.services.runtime_errors import ProvisioningError


def _read_policy():
    from database._client import db
    from database.normal_enrichment_activation import reference
    snap=reference(db).get(timeout=3,retry=None)
    return snap.to_dict() if snap.exists else None


def assert_transcript_processing_active(uid):
    from ella.services.normal_account_routing import admitted_subject
    from database.normal_enrichment_activation import valid_policy,owner_allowed
    if not admitted_subject(uid):
        raise ProvisioningError('account_transcript_processing_owner_required',retryable=False)
    try:
        policy=_read_policy()
        if type(policy) is not dict:
            raise ValueError()
        now=datetime.now(timezone.utc)
        valid=valid_policy(policy,now)
        owns=owner_allowed(policy,uid)
    except Exception:
        raise ProvisioningError('account_transcript_processing_authority_unavailable',retryable=False) from None
    if policy.get('state')=='paused':
        raise ProvisioningError('account_transcript_processing_paused',retryable=False)
    if not valid or not owns:
        raise ProvisioningError('account_transcript_processing_not_admitted',retryable=False)
    if policy.get('mode') == 'per_account_lifecycle_v1':
        from database._client import db
        from database.normal_account_lifecycle import processing_status
        try:
            state=processing_status(db,uid,now=now)
        except Exception:
            raise ProvisioningError('account_transcript_processing_authority_unavailable',retryable=False) from None
        if state.get('state') not in {'available','processing'}:
            raise ProvisioningError(state.get('error_code') or 'account_transcript_processing_not_admitted',retryable=False)
    # Positive result is ONLY absence of this hold, never delivery permission.


async def require_transcript_processing_active(uid):
    await asyncio.to_thread(assert_transcript_processing_active,uid)


def assert_legacy_transcript_dispatch_allowed(uid):
    """Product lifecycle never falls through to legacy, even after flag drift."""
    try:
        policy=_read_policy()
        if type(policy) is not dict:raise ValueError()
    except Exception:
        raise ProvisioningError('account_transcript_processing_authority_unavailable',retryable=False) from None
    if policy.get('mode')=='per_account_lifecycle_v1':
        raise ProvisioningError('account_processing_legacy_fallback_disabled',retryable=False)
