"""Only trusted host may resolve/deliver already admitted opaque scanner work."""

import os
from fastapi import APIRouter, Depends, Header, HTTPException
from utils.ella.exact_firebase_auth import get_exact_service_authority, ELLA_SUBJECT_UID_HEADER
from ella.services.whisper_delivery import DeliveryConflict, DeliveryNotFound
from ella.services.whisper_tool_factory import create_tool_service

router = APIRouter(prefix='/v1/ella/guardian/delivery', tags=['ella'])


def require_tool_host(
    service_key: str | None = Header(None, alias='X-Ella-Callback-Service-Key'),
    subject_uid: str | None = Header(None, alias=ELLA_SUBJECT_UID_HEADER),
):
    return get_exact_service_authority(
        provided_service_key=service_key,
        configured_service_key=os.getenv('ELLA_CALLBACK_SERVICE_KEY', '').strip(),
        service_subject_uid=subject_uid,
        service='ella_callback',
    )


async def dispatch(operation, body, authority, response_deadline_unix=None):
    uid = authority.require_uid(None, feature='Guardian admitted tool delivery')
    service = await create_tool_service()
    try:
        if operation in {'authority', 'deliver'}:
            return await getattr(service, operation)(uid, body, response_deadline_unix)
        return await getattr(service, operation)(uid, body)
    except DeliveryNotFound:
        raise HTTPException(404, detail={'code': 'tool_work_unknown'}) from None
    except DeliveryConflict:
        raise HTTPException(409, detail={'code': 'tool_delivery_held'}) from None
    except Exception:
        raise HTTPException(503, detail={'code': 'tool_delivery_unknown'}) from None


@router.post('/tool-authority')
async def tool_authority(
    body: dict,
    authority=Depends(require_tool_host),
    response_deadline_unix: float | None = Header(None, alias='X-Ella-Response-Deadline'),
):
    return await dispatch('authority', body, authority, response_deadline_unix)


@router.post('/tool-deliver')
async def tool_deliver(
    body: dict,
    authority=Depends(require_tool_host),
    response_deadline_unix: float | None = Header(None, alias='X-Ella-Response-Deadline'),
):
    return await dispatch('deliver', body, authority, response_deadline_unix)


@router.post('/tool-close')
async def tool_close(body: dict, authority=Depends(require_tool_host)):
    return await dispatch('close', body, authority)


@router.post('/tool-status')
async def tool_status(body: dict, authority=Depends(require_tool_host)):
    return await dispatch('status', body, authority)
