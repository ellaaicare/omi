"""
Auto-provision module for new OMI app users.

When a user connects via /v4/listen websocket and has no agent cluster,
this module automatically provisions one via the Ella provision API.

Also handles identity sync — writing phone/email back to the users table
after provisioning so OpenClaw identity links stay up to date.
"""

import json
import logging
import os
import re
from typing import Optional

import asyncpg
import httpx

logger = logging.getLogger("ella.auto_provision")

# Database connection pool (shared pattern from resolve.py)
_pool: Optional[asyncpg.Pool] = None

PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")


def _slugify_user_id(name: str, uid: str) -> str:
    """Generate a stable, human-readable userId from display name + UID suffix.

    Examples:
        _slugify_user_id("Greg Lindberg", "WynjpitC0wb1uxBAWWEUVnTWRa62")
        → "greg-lindberg-ra62"

        _slugify_user_id("Mary Jane Watson-Parker", "abc123XYZ")
        → "mary-jane-watson-parker-3xyz"
    """
    if not name or not name.strip():
        name = "user"

    # Lowercase, replace whitespace with hyphens
    slug = name.lower().strip()
    slug = re.sub(r'\s+', '-', slug)

    # Strip non-alphanumeric (keep hyphens)
    slug = re.sub(r'[^a-z0-9-]', '', slug)

    # Collapse multiple hyphens
    slug = re.sub(r'-+', '-', slug).strip('-')

    # Truncate to 30 chars
    slug = slug[:30].rstrip('-')

    # Append last 4 chars of uid (lowercased) for uniqueness
    suffix = uid[-4:].lower() if uid and len(uid) >= 4 else uid.lower() if uid else "0000"
    slug = f"{slug}-{suffix}"

    return slug


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

    Fetches email, phone, conditions, medications from Postgres before
    calling the provision API with a full payload including identity data.

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
        # 1. Fetch user record from Postgres
        pool = await _get_pool()
        row = await pool.fetchrow(
            """
            SELECT email, name, identities, timezone, conditions, medications
            FROM users WHERE omi_uid = $1
            """,
            uid,
        )

        if not row:
            logger.warning(f"User {uid} not found in DB, falling back to minimal provision")
            # Fall back to minimal provision (email/phone may not be available)
            return await _provision_with_payload(uid, {"userId": _slugify_user_id(name, uid), "omiUid": uid, "label": name})

        # 2. Extract identity data
        email = row["email"]
        identities = json.loads(row["identities"]) if row["identities"] else {}
        phone = identities.get("phone")
        user_name = row["name"] or name
        tz = row["timezone"] or "America/Los_Angeles"
        conditions = row["conditions"] or []
        medications = row["medications"] or []

        if not email and not phone:
            logger.warning(f"No email or phone for uid={uid}, provisioning with label only")

        # 3. Build full provision payload
        user_id = _slugify_user_id(user_name, uid)
        payload = {
            "userId": user_id,
            "omiUid": uid,
            "label": user_name,
        }

        # Only include identity fields if they have values
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        if tz:
            payload["timezone"] = tz
        if conditions:
            payload["conditions"] = conditions if isinstance(conditions, list) else [conditions]
        if medications:
            payload["medications"] = medications if isinstance(medications, list) else [medications]

        # 4. Call provision API
        result = await _provision_with_payload(uid, payload)

        # 5. Write phone back to users.identities if present and provision succeeded
        if result.get("success") and phone:
            try:
                await pool.execute(
                    """
                    UPDATE users SET identities = COALESCE(identities, '{}'::jsonb) || $1::jsonb
                    WHERE omi_uid = $2
                    """,
                    json.dumps({"phone": phone}),
                    uid,
                )
            except Exception as e:
                logger.warning(f"Failed to write phone to identities for uid={uid}: {e}")

        return result

    except Exception as e:
        error_msg = f"Auto-provision error: {str(e)}"
        logger.error(f"Auto-provision error for uid={uid}: {e}")
        return {"success": False, "error": error_msg}


async def _provision_with_payload(uid: str, payload: dict) -> dict:
    """Send a provision request with the given payload.

    Args:
        uid: Firebase UID (for logging)
        payload: Full provision payload dict

    Returns:
        dict with success/error/cluster keys
    """
    try:
        headers = {}
        if PROVISION_API_TOKEN:
            headers["Authorization"] = f"Bearer {PROVISION_API_TOKEN}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{PROVISION_API_URL}/provision",
                headers=headers,
                json=payload,
            )

            if response.status_code == 200:
                data = response.json()
                logger.info(f"Successfully provisioned uid={uid} as userId={payload.get('userId')}")
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
