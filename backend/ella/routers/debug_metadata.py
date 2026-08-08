"""
Ella debug metadata proxy.

Developer-only endpoints for viewing OpenClaw Observer sidecars without leaking
internal scanner/routing metadata into user-facing summaries or reports.
"""

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ella.routers.resolve import PROVISION_API_KEY, PROVISION_API_URL, resolve_user_routing
from utils.ella.exact_firebase_auth import get_exact_firebase_uid, require_matching_firebase_uid

router = APIRouter(prefix="/v1/ella/debug", tags=["ella-debug"])

DEBUG_METADATA_ENABLED = os.getenv("ELLA_DEBUG_METADATA_ENABLED", "false").lower() == "true"


def _debug_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Ella-Visibility"] = "internal"


async def _agent_id_for_uid(uid: str) -> str:
    if not DEBUG_METADATA_ENABLED:
        raise HTTPException(status_code=404, detail="Debug metadata is disabled")
    if not uid:
        raise HTTPException(status_code=400, detail="uid query parameter required")

    resolved = await resolve_user_routing(uid)
    routing = resolved.get("routing") if resolved else None
    agent_id = routing.get("agentId") if routing else None
    if not agent_id:
        raise HTTPException(status_code=404, detail="No OpenClaw agent found for uid")
    return agent_id


async def _fetch_provision_metadata(path: str, params: Optional[dict] = None) -> dict:
    headers = {}
    if PROVISION_API_KEY:
        headers["Authorization"] = f"Bearer {PROVISION_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{PROVISION_API_URL}{path}", params=params or {}, headers=headers)
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Provision API unavailable")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Observer metadata not found")
    if resp.status_code in {401, 403}:
        raise HTTPException(status_code=502, detail="Provision API rejected debug metadata request")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Provision API error: {resp.status_code}")

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Provision API returned invalid JSON")


@router.get("/conversations/metadata")
async def list_conversation_metadata(
    response: Response,
    uid: str = Query(..., description="Firebase/OMI user UID"),
    limit: int = Query(50, ge=1, le=200),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
):
    """List recent Observer sidecar metadata for a user's OpenClaw agent."""
    uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Debug metadata")
    _debug_response_headers(response)
    agent_id = await _agent_id_for_uid(uid)
    data = await _fetch_provision_metadata(
        f"/workspace/{agent_id}/metadata/conversations",
        params={"limit": limit},
    )
    data["uid"] = uid
    data["agent_id"] = agent_id
    return data


@router.get("/conversations/{conversation_id}/metadata")
async def read_conversation_metadata(
    conversation_id: str,
    response: Response,
    uid: str = Query(..., description="Firebase/OMI user UID"),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
):
    """Read one Observer sidecar metadata document for a user's conversation."""
    uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Debug metadata")
    _debug_response_headers(response)
    agent_id = await _agent_id_for_uid(uid)
    data = await _fetch_provision_metadata(f"/workspace/{agent_id}/metadata/conversations/{conversation_id}")
    data["uid"] = uid
    data["agent_id"] = agent_id
    return data
