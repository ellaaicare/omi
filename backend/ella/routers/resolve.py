"""
Ella Resolve Router - User identity to active Ella agent resolution.

Resolves the exact Firebase bearer subject to non-secret runtime availability.

Endpoints:
- GET  /v1/ella/resolve?uid={firebase_uid}

Used by authenticated clients that need a non-secret runtime readiness signal.
Chat history uses the first-party /v1/ella/chat/history endpoint directly.
"""

import json
import logging
import os
from typing import Optional

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from database.ella_provisioning import EllaProvisioningRepository
from database.honcho_attestation import authority_credential
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import resolve_isolated_runtime, retained_owner_uid_configured
from utils.ella.exact_firebase_auth import get_exact_firebase_uid, require_matching_firebase_uid
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-resolve"])

# Database connection pool (shared with other Ella routers)
_pool: Optional[asyncpg.Pool] = None

OPENCLAW_GATEWAY_TOKEN = authority_credential("OPENCLAW_GATEWAY_TOKEN", strip=False)
PROVISION_API_KEY = authority_credential("ELLA_PROVISION_API_KEY", "ELLA_PROVISION_API_TOKEN", strip=False)
PROVISION_API_URL = os.getenv("ELLA_PROVISION_URL", "http://100.76.138.56:8200")
DEFAULT_GATEWAY_URL = os.getenv("OPENCLAW_URL", "http://100.76.138.56:19001")
PUBLIC_GATEWAY_URL = os.getenv("OPENCLAW_PUBLIC_URL", "https://gateway.ella-ai-care.com")
CHAT_PLATFORM = os.getenv("ELLA_CHAT_PLATFORM", "openclaw").strip().lower()
HERMES_AGENT_ID = os.getenv("HERMES_AGENT_ID", "hermes")
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_PUBLIC_URL", "").strip()
HERMES_GATEWAY_TOKEN = authority_credential("HERMES_API_SERVER_KEY", "API_SERVER_KEY", strip=False)
HERMES_PROVISION_URL = os.getenv("HERMES_PROVISION_API_URL", "http://100.76.138.56:8210")


async def _get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database="ella_ai",
            min_size=2,
            max_size=10,
        )
    return _pool


async def resolve_user_routing(uid: str) -> Optional[dict]:
    """Resolve a Firebase UID to active agent routing info.

    Shared helper used by both /resolve endpoint and chat.py dynamic routing.
    Returns None if user not found.
    """
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.id, u.name, u.omi_uid, u.status, u.guardian_mode, u.timezone,
               u.conditions, u.medications,
               ac.agents, ac.status AS cluster_status
        FROM users u
        LEFT JOIN agent_clusters ac ON ac.user_id = u.id
        WHERE u.omi_uid = $1
        """,
        uid,
    )
    if not row:
        return None

    runtime = await resolve_isolated_runtime(uid, EllaProvisioningRepository(pool), target_mode="hermes-cloud-chat")
    if runtime:
        cloud = runtime.provider == "hermes_cloud"
        routing = {
            "agentId": runtime.agent_id,
            "sessionKey": f"ella:omi:{uid.lower()}:canonical",
            "historyUrl": "/v1/ella/chat/history",
            "platform": runtime.provider,
            "profileName": runtime.profile_name,
            "bindingRevision": runtime.revision,
            "modelPolicyVersion": runtime.model_policy_version,
            "voicePolicyVersion": runtime.voice_policy_version,
        }
        if cloud:
            routing.update(
                {
                    "chatUrl": "/v1/ella/chat/stream",
                    "runtimeBound": True,
                }
            )
        else:
            routing.update(
                {
                    "gatewayUrl": runtime.gateway_url,
                    "token": runtime.gateway_token,
                }
            )
        return {
            "user": {
                "id": str(row["id"]),
                "name": row["name"],
                "omiUid": row["omi_uid"],
                "status": row["status"],
                "guardianMode": row["guardian_mode"],
                "timezone": row["timezone"],
                "conditions": row["conditions"],
                "medications": row["medications"],
            },
            "routing": routing,
        }

    if not retained_owner_uid_configured(uid):
        return {
            "user": {
                "id": str(row["id"]),
                "name": row["name"],
                "omiUid": row["omi_uid"],
                "status": row["status"],
                "guardianMode": row["guardian_mode"],
                "timezone": row["timezone"],
                "conditions": row["conditions"],
                "medications": row["medications"],
            },
            "routing": None,
        }

    agents_raw = row["agents"]
    agents = json.loads(agents_raw) if isinstance(agents_raw, str) else agents_raw

    routing = None
    workspace = str(agents.get("workspace") or "").strip() if isinstance(agents, dict) else ""
    if agents and workspace:
        gateway_url = agents.get("gatewayUrl", DEFAULT_GATEWAY_URL)
        routing = {
            "agentId": agents.get("userAgentId"),
            "caregiverAgentId": agents.get("caregiverAgentId"),
            "scannerAgentId": agents.get("scannerAgentId"),
            "summarizerAgentId": agents.get("summarizerAgentId"),
            "sessionKey": (
                f"agent:{agents.get('userAgentId')}:direct:ella:omi-{row['omi_uid'].lower()}"
                if row["omi_uid"] and agents.get("userAgentId")
                else None
            ),
            "gatewayUrl": PUBLIC_GATEWAY_URL,
            "scannerGatewayUrl": agents.get("scannerGatewayUrl", gateway_url),
            "token": agents.get("gatewayToken") or OPENCLAW_GATEWAY_TOKEN,
            "provisionToken": PROVISION_API_KEY,
            "provisionUrl": PROVISION_API_URL,
            "clusterStatus": row["cluster_status"],
            "workspace": workspace,
            "historyUrl": f"/v1/ella/chat/history/{agents.get('userAgentId', '')}",
        }
        if CHAT_PLATFORM == "hermes" and HERMES_GATEWAY_URL and HERMES_GATEWAY_TOKEN:
            # iOS Pattern C still sends model=openclaw:{agentId}; Hermes accepts that
            # OpenAI-compatible model label, so expose "hermes" as the routed agent.
            routing.update(
                {
                    "agentId": HERMES_AGENT_ID,
                    "sessionKey": f"ella:omi:{row['omi_uid'].lower()}:canonical",
                    "gatewayUrl": HERMES_GATEWAY_URL.rstrip("/"),
                    "scannerGatewayUrl": HERMES_GATEWAY_URL.rstrip("/"),
                    "token": HERMES_GATEWAY_TOKEN,
                    "provisionToken": PROVISION_API_KEY,
                    "provisionUrl": HERMES_PROVISION_URL,
                    "workspace": workspace,
                    "historyUrl": "/v1/ella/chat/history",
                    "platform": "hermes",
                }
            )

    return {
        "user": {
            "id": str(row["id"]),
            "name": row["name"],
            "omiUid": row["omi_uid"],
            "status": row["status"],
            "guardianMode": row["guardian_mode"],
            "timezone": row["timezone"],
            "conditions": row["conditions"],
            "medications": row["medications"],
        },
        "routing": routing,
    }


@router.get("/resolve")
async def resolve_endpoint(
    uid: Optional[str] = Query(None, description="Firebase UID (omiUid)"),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
):
    """Resolve only the exact Firebase bearer subject to a safe status view."""
    uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Ella resolve")
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.omi_uid, u.status
        FROM users u
        WHERE u.omi_uid = $1
        """,
        uid,
    )

    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found"},
        )

    try:
        runtime = await resolve_isolated_runtime(
            uid,
            EllaProvisioningRepository(pool),
            target_mode="hermes-cloud-chat",
        )
    except ProvisioningError:
        logger.info("code=ella_resolve_runtime_unavailable classification=provisioning")
        runtime = None
    except Exception:
        logger.error("code=ella_resolve_runtime_authority_error classification=unexpected")
        runtime = None

    return {
        "user": {
            "omiUid": row["omi_uid"],
            "status": row["status"],
        },
        "routing": {
            "available": runtime is not None,
            "clusterStatus": runtime.status if runtime else None,
            "platform": runtime.provider if runtime else None,
        },
    }


@router.get("/chat/history/{agent_id}")
async def proxy_chat_history(
    agent_id: str,
    limit: int = 50,
    session_key: Optional[str] = None,
    authenticated_uid: str = Depends(auth.get_current_user_uid),
):
    """Proxy chat history requests to the Provision API on Mac Mini.

    The iOS app can't reach the Mac Mini's Tailscale IP directly,
    so this endpoint forwards the request.
    """
    runtime = await resolve_isolated_runtime(
        authenticated_uid,
        EllaProvisioningRepository(await _get_pool()),
        target_mode="hermes-cloud-chat",
    )
    if runtime:
        raise HTTPException(status_code=410, detail={"code": "legacy_history_disabled"})

    resolved = await resolve_user_routing(authenticated_uid)
    if not resolved or (resolved.get("routing") or {}).get("agentId") != agent_id:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})

    provision_base = PROVISION_API_URL.rstrip('/')
    params = f"limit={limit}"
    if session_key:
        from urllib.parse import quote

        params += f"&session_key={quote(session_key)}"
    url = f"{provision_base}/chat/history/{agent_id}?{params}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"x-api-key": PROVISION_API_KEY})
            if resp.status_code != 200:
                return JSONResponse(status_code=resp.status_code, content={"error": "upstream_error"})
            return resp.json()
    except Exception:
        logger.error("code=ella_legacy_history_proxy_unavailable classification=unexpected")
        return JSONResponse(status_code=502, content={"error": "provision_unreachable"})
