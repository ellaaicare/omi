"""
Auto-provision module for new OMI app users.

When a user connects via /v4/listen websocket and has no agent cluster,
this module automatically provisions one via the Ella provision API on Mac Mini,
then stores the returned agent IDs back into the dashboard database.

Two-phase flow:
  1. Call Mac Mini provision API → creates workspace + agents
  2. Store agent IDs in agent_clusters table → chat.py resolves correctly
"""

import json
import logging
import os
from typing import Optional

import asyncpg
import httpx

logger = logging.getLogger("ella.auto_provision")

# Database connection pool (shared pattern from resolve.py)
_pool: Optional[asyncpg.Pool] = None

PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_URL", "http://100.76.138.56:19001")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")


def _slugify(s: str) -> str:
    """Create a safe ID slug from a string."""
    import re
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:40]


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


async def get_agent_cluster(uid: str) -> Optional[dict]:
    """Check if a user has a COMPLETE agent cluster (with userAgentId).

    Args:
        uid: Firebase UID (omiUid)

    Returns:
        Agent cluster data if exists AND has userAgentId, None otherwise.
        A cluster without userAgentId is considered incomplete and will
        trigger re-provisioning.
    """
    try:
        pool = await _get_pool()
        row = await pool.fetchrow(
            """
            SELECT ac.id, ac.agents, ac.status, ac.user_id
            FROM users u
            LEFT JOIN agent_clusters ac ON ac.user_id = u.id
            WHERE u.omi_uid = $1
            """,
            uid,
        )

        if not row or not row["agents"]:
            return None

        agents = json.loads(row["agents"]) if isinstance(row["agents"], str) else row["agents"]

        # A cluster without userAgentId is incomplete — treat as missing
        if not agents.get("userAgentId"):
            logger.warning(
                f"Cluster for uid={uid} exists but missing userAgentId — needs re-provision"
            )
            return None

        return {
            "id": str(row["id"]) if row["id"] else None,
            "agents": agents,
            "status": row["status"],
            "user_id": str(row["user_id"]) if row["user_id"] else None,
        }
    except Exception as e:
        logger.error(f"Error checking agent cluster for uid={uid}: {e}")
        return None


async def auto_provision_user(uid: str, name: str = "User") -> dict:
    """Auto-provision a user: call Mac Mini API + store agent IDs in DB.

    Two-phase flow:
      1. Look up user in DB to get email/phone for provision payload
      2. Call Mac Mini provision API with correct payload (userId, email, label)
      3. Store returned agent IDs back into agent_clusters table

    Args:
        uid: Firebase UID (omiUid)
        name: User's display name (default: "User")

    Returns:
        dict with keys:
            - success: bool
            - error: str (if success=False)
            - cluster: dict (if success=True)
    """
    try:
        pool = await _get_pool()

        # Phase 0: Look up user record for email/phone
        user_row = await pool.fetchrow(
            """
            SELECT u.id, u.name, u.email, u.identities, u.timezone,
                   u.conditions, u.medications,
                   ac.id AS cluster_id
            FROM users u
            LEFT JOIN agent_clusters ac ON ac.user_id = u.id
            WHERE u.omi_uid = $1
            """,
            uid,
        )

        user_name = name
        user_email = None
        user_phone = None
        user_db_id = None
        cluster_id = None
        timezone = "America/Los_Angeles"
        conditions = []
        medications = []

        if user_row:
            user_db_id = str(user_row["id"])
            cluster_id = str(user_row["cluster_id"]) if user_row["cluster_id"] else None
            user_name = user_row["name"] or name
            user_email = user_row["email"]
            timezone = user_row["timezone"] or timezone
            conditions = user_row["conditions"] or []
            medications = user_row["medications"] or []
            identities = user_row["identities"]
            if identities:
                if isinstance(identities, str):
                    identities = json.loads(identities)
                user_phone = identities.get("phone")

        # Build the OpenClaw userId slug
        openclaw_user_id = "omi-" + _slugify(uid)[:36]

        # Phase 1: Call Mac Mini provision API with correct payload
        provision_payload = {
            "userId": openclaw_user_id,
            "label": user_name,
            "omiUid": uid,
            "firebaseUid": uid,
            "profile": {
                "preferredName": user_name,
                "timezone": timezone,
                "conditions": conditions if isinstance(conditions, list) else [],
                "medications": medications if isinstance(medications, list) else [],
            },
        }

        # Add email/phone if available (at least one required by provision API)
        if user_email:
            provision_payload["email"] = user_email
        if user_phone:
            provision_payload["phone"] = user_phone

        # If neither email nor phone, use a placeholder email
        if not user_email and not user_phone:
            provision_payload["email"] = f"{uid[:8]}@ella-ai-auto.com"

        headers = {"Content-Type": "application/json"}
        if PROVISION_API_TOKEN:
            headers["Authorization"] = f"Bearer {PROVISION_API_TOKEN}"

        logger.info(f"Provisioning uid={uid} as openclaw_user_id={openclaw_user_id}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{PROVISION_API_URL}/provision",
                headers=headers,
                json=provision_payload,
            )

            if response.status_code != 200:
                error_msg = f"Provision API returned {response.status_code}: {response.text}"
                logger.warning(f"Auto-provision failed for uid={uid}: {error_msg}")
                return {"success": False, "error": error_msg}

            provision_result = response.json()

        logger.info(f"Provision API success for uid={uid}: workspace={provision_result.get('workspace')}")

        # Phase 2: Store agent IDs back into DB.
        # IMPORTANT: Use values from provision API, not local fallbacks, for agent IDs
        # and gatewayToken. The provision API returns authoritative values from Mac Mini.
        # Local fallbacks would overwrite correct DB values on re-auth.
        # Postgres UPDATE uses agents || $1 (merge) so fields absent here are preserved.
        # Incident: ella-ai#501 (Apr 2026) - full replace + wrong fallbacks wiped routing.
        gateway_url = provision_result.get("gatewayUrl", OPENCLAW_GATEWAY_URL)
        scanner_gateway_url = provision_result.get("scannerGatewayUrl", OPENCLAW_GATEWAY_URL)
        cluster_agents_dict = {
            "provider": "openclaw",
            "gatewayUrl": gateway_url,
            "scannerGatewayUrl": scanner_gateway_url,
            "workspace": provision_result.get("workspace", ""),
            "userId": openclaw_user_id,
            "provisionedAt": provision_result.get("provisionedAt")
                or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        for field, fallback in [
            ("userAgentId", f"ella-{openclaw_user_id}"),
            ("caregiverAgentId", f"ella-cg-{openclaw_user_id}"),
            ("scannerAgentId", f"ella-scanner-{openclaw_user_id}"),
            ("summarizerAgentId", "summarizer"),
        ]:
            cluster_agents_dict[field] = provision_result.get(field) or fallback
        cluster_agents_dict["gatewayToken"] = (
            provision_result.get("gatewayToken") or OPENCLAW_GATEWAY_TOKEN
        )
        cluster_agents = json.dumps(cluster_agents_dict)

        if user_db_id:
            if cluster_id:
                # Update existing cluster
                await pool.execute(
                    """
                    UPDATE agent_clusters
                    -- IMPORTANT: Use || merge, NOT plain assignment.
                    -- Plain SET agents = $1 wipes userAgentId/scannerAgentId/gatewayToken on
                    -- re-authentication (sign-out → sign-in). Incident: ella-ai#501 (Apr 2026).
                    SET agents = agents || $1::jsonb, status = 'ACTIVE',
                        last_health_check = NOW(), health_status = 'Auto-provisioned'
                    WHERE id = $2::uuid
                    """,
                    cluster_agents,
                    cluster_id,
                )
                logger.info(f"Updated cluster {cluster_id} with agent IDs for uid={uid}")
            else:
                # Create new cluster
                await pool.execute(
                    """
                    INSERT INTO agent_clusters (user_id, agents, status, last_health_check, health_status)
                    VALUES ($1::uuid, $2::jsonb, 'ACTIVE', NOW(), 'Auto-provisioned')
                    """,
                    user_db_id,
                    cluster_agents,
                )
                logger.info(f"Created new cluster for user {user_db_id} uid={uid}")
        else:
            logger.warning(f"No user record found for uid={uid} — agents provisioned on Mac Mini but not stored in DB")

        return {"success": True, "cluster": provision_result}

    except httpx.TimeoutException:
        logger.error(f"Auto-provision timeout for uid={uid}")
        return {"success": False, "error": "Provision API timeout"}
    except Exception as e:
        logger.error(f"Auto-provision error for uid={uid}: {e}", exc_info=True)
        return {"success": False, "error": f"Provision API error: {str(e)}"}
