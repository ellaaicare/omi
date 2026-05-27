"""
Ella Resolve Router - User identity to active Ella agent resolution.

Resolves any user identifier (Firebase UID, email, phone) to the correct
Hermes or legacy OpenClaw agent routing info (agentId, sessionKey, gatewayUrl,
token).

Endpoints:
- GET  /v1/ella/resolve?uid={firebase_uid}
- GET  /v1/ella/resolve?email={email}
- GET  /v1/ella/resolve?phone={phone}

Used by:
- iOS Flutter app for history/config discovery. Production chat should use
  /v1/ella/chat/stream so the backend can hydrate from and write to the
  canonical timeline.
- E2E Flow Debugger
- Any client that needs to discover the active agent runtime
"""

import json
import logging
import os
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-resolve"])

# Database connection pool (shared with other Ella routers)
_pool: Optional[asyncpg.Pool] = None

OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
PROVISION_API_KEY = os.getenv("ELLA_PROVISION_API_KEY", os.getenv("ELLA_PROVISION_API_TOKEN", ""))
PROVISION_API_URL = os.getenv("ELLA_PROVISION_URL", "http://100.76.138.56:8200")
DEFAULT_GATEWAY_URL = os.getenv("OPENCLAW_URL", "http://100.76.138.56:19001")
PUBLIC_GATEWAY_URL = os.getenv("OPENCLAW_PUBLIC_URL", "https://gateway.ella-ai-care.com")
CHAT_PLATFORM = os.getenv("ELLA_CHAT_PLATFORM", "openclaw").strip().lower()
HERMES_AGENT_ID = os.getenv("HERMES_AGENT_ID", "hermes")
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_PUBLIC_URL", "https://api.ella-ai-care.com/hermes")
HERMES_GATEWAY_TOKEN = os.getenv("HERMES_API_SERVER_KEY", os.getenv("API_SERVER_KEY", ""))
HERMES_PROVISION_URL = os.getenv("HERMES_PROVISION_API_URL", "http://100.76.138.56:8210")
HERMES_WORKSPACE = os.getenv("HERMES_WORKSPACE", "/Users/ellaai/.hermes/profiles/plato-eval/workspace")


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

    agents = json.loads(row["agents"]) if row["agents"] else None

    routing = None
    if agents:
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
            "workspace": agents.get("workspace"),
            "historyUrl": f"/v1/ella/chat/history/{agents.get('userAgentId', '')}",
        }
        if CHAT_PLATFORM == "hermes":
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
                    "workspace": HERMES_WORKSPACE,
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
    email: Optional[str] = Query(None, description="User email"),
    phone: Optional[str] = Query(None, description="User phone (E.164)"),
):
    """Resolve a user identifier to active agent routing info.

    Accepts one of: uid, email, phone. Returns the user's agent cluster
    routing information including agentId, sessionKey, gatewayUrl, and token.
    """
    if not uid and not email and not phone:
        raise HTTPException(
            status_code=400,
            detail="One of uid, email, or phone is required",
        )

    pool = await _get_pool()

    # Build query based on which identifier was provided
    if uid:
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
    elif email:
        row = await pool.fetchrow(
            """
            SELECT u.id, u.name, u.omi_uid, u.status, u.guardian_mode, u.timezone,
                   u.conditions, u.medications,
                   ac.agents, ac.status AS cluster_status
            FROM users u
            LEFT JOIN agent_clusters ac ON ac.user_id = u.id
            WHERE u.email = $1
            """,
            email,
        )
    else:
        # Phone lookup via identities JSONB
        row = await pool.fetchrow(
            """
            SELECT u.id, u.name, u.omi_uid, u.status, u.guardian_mode, u.timezone,
                   u.conditions, u.medications,
                   ac.agents, ac.status AS cluster_status
            FROM users u
            LEFT JOIN agent_clusters ac ON ac.user_id = u.id
            WHERE u.identities->>'phone' = $1
            """,
            phone,
        )

    if not row:
        identifier = uid or email or phone
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "identifier": identifier},
        )

    agents = json.loads(row["agents"]) if row["agents"] else None

    routing = None
    if agents:
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
            "workspace": agents.get("workspace"),
            "historyUrl": f"/v1/ella/chat/history/{agents.get('userAgentId', '')}",
        }
        if CHAT_PLATFORM == "hermes":
            routing.update(
                {
                    "agentId": HERMES_AGENT_ID,
                    "sessionKey": f"ella:omi:{row['omi_uid'].lower()}:canonical",
                    "gatewayUrl": HERMES_GATEWAY_URL.rstrip("/"),
                    "scannerGatewayUrl": HERMES_GATEWAY_URL.rstrip("/"),
                    "token": HERMES_GATEWAY_TOKEN,
                    "provisionToken": PROVISION_API_KEY,
                    "provisionUrl": HERMES_PROVISION_URL,
                    "clusterStatus": row["cluster_status"],
                    "workspace": HERMES_WORKSPACE,
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


@router.get("/chat/history/{agent_id}")
async def proxy_chat_history(agent_id: str, limit: int = 50, session_key: Optional[str] = None):
    """Proxy chat history requests to the Provision API on Mac Mini.

    The iOS app can't reach the Mac Mini's Tailscale IP directly,
    so this endpoint forwards the request.
    """
    import httpx

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
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=resp.status_code, content={"error": "upstream_error"})
            return resp.json()
    except Exception as e:
        logger.error(f"Chat history proxy error: {e}")
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=502, content={"error": "provision_unreachable"})
