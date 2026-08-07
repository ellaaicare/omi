"""Compatibility endpoint for the old iOS app onboarding flow."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from database.ella_provisioning import EllaProvisioningRepository

logger = logging.getLogger('ella.legacy_onboarding')

router = APIRouter(prefix='/api', tags=['legacy-onboarding'])


class LegacyOnboardingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    firebaseUid: str
    email: str = ''
    name: str = ''
    timezone: str = 'America/Los_Angeles'


@router.post('/onboarding')
async def legacy_onboarding(
    payload: LegacyOnboardingRequest,
) -> dict[str, Any]:
    """Compatibility shim: accept old iOS app format, return expected response."""
    uid = payload.firebaseUid.strip()
    if not uid:
        raise HTTPException(status_code=400, detail={'error': 'firebaseUid is required'})

    # Return a simple response that lets the app proceed
    # The app expects: {userId, ellaKey, agents, provisioned}
    # If provisioned: false + agents != null -> pre-provisioned, skip onboarding
    return {
        'userId': uid,
        'ellaKey': '',
        'provisioned': True,
        'agents': None,
    }
