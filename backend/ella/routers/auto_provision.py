"""
Auto-provision module for new OMI app users.

When a user connects via /v4/listen websocket and has no agent cluster,
this module automatically provisions one via the Ella provision API.
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
    """Check if a user has an agent cluster.

    Args:
        uid: Firebase UID (omiUid)

    Returns:
        Agent cluster data if exists, None otherwise
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

        agents = json.loads(row["agents"]) if row["agents"] else None
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
    """Auto-provision a user via the Ella provision API.

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
        headers = {}
        if PROVISION_API_TOKEN:
            headers["Authorization"] = f"Bearer {PROVISION_API_TOKEN}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{PROVISION_API_URL}/provision",
                headers=headers,
                json={"uid": uid, "name": name},
            )

            if response.status_code == 200:
                data = response.json()
                logger.info(f"Successfully provisioned uid={uid}")
                return {"success": True, "cluster": data}
            else:
                error_msg = f"Provision API returned {response.status_code}: {response.text}"
                logger.warning(f"Auto-provision failed for uid={uid}: {error_msg}")
                return {"success": False, "error": error_msg}

    except httpx.TimeoutException:
        error_msg = "Provision API timeout"
        logger.error(f"Auto-provision timeout for uid={uid}")
        return {"success": False, "error": error_msg}
    except Exception as e:
        error_msg = f"Provision API error: {str(e)}"
        logger.error(f"Auto-provision error for uid={uid}: {e}")
        return {"success": False, "error": error_msg}
