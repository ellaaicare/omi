"""
Auto-provision module for new OMI app users.

When a user connects via /v4/listen websocket and has no agent cluster,
this module automatically provisions one via the Ella provision API.

Also handles identity sync — writing phone/email back to the users table
after provisioning so isolated Hermes identity links stay up to date.
"""

import json
import logging
import os
import re
from typing import Optional

import asyncpg
import httpx

from ella.utils.provision_authority import ProvisionAuthority, ProvisionAuthorityError, hermes_provision_authority

logger = logging.getLogger("ella.auto_provision")

# Database connection pool (shared pattern from resolve.py)
_pool: Optional[asyncpg.Pool] = None


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
    except Exception:
        logger.error("agent_cluster_lookup_failed")
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
        try:
            authority = hermes_provision_authority()
        except ProvisionAuthorityError as exc:
            logger.error(exc.code)
            return {"success": False, "error": exc.code}

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
            logger.warning("auto_provision_identity_missing")
            # Fall back to minimal provision (email/phone may not be available)
            return await _provision_with_payload(
                uid,
                {"userId": _slugify_user_id(name, uid), "omiUid": uid, "label": name},
                authority=authority,
            )

        # 2. Extract identity data
        email = row["email"]
        identities = json.loads(row["identities"]) if row["identities"] else {}
        phone = identities.get("phone")
        user_name = row["name"] or name
        tz = row["timezone"] or "America/Los_Angeles"
        conditions = row["conditions"] or []
        medications = row["medications"] or []

        if not email and not phone:
            logger.warning("auto_provision_contact_missing")

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
        result = await _provision_with_payload(uid, payload, authority=authority)

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
            except Exception:
                logger.warning("auto_provision_identity_write_failed")

        return result

    except Exception:
        logger.error("auto_provision_internal_error")
        return {"success": False, "error": "auto_provision_internal_error"}


async def _provision_with_payload(
    uid: str,
    payload: dict,
    *,
    authority: Optional[ProvisionAuthority] = None,
) -> dict:
    """Send a provision request with the given payload.

    Args:
        uid: Firebase UID (retained for caller compatibility)
        payload: Full provision payload dict

    Returns:
        dict with success/error/cluster keys
    """
    del uid
    try:
        resolved_authority = authority or hermes_provision_authority()
        headers = {"Authorization": f"Bearer {resolved_authority.token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{resolved_authority.base_url}/provision",
                headers=headers,
                json=payload,
            )

            if response.status_code == 200:
                data = response.json()
                logger.info("auto_provision_succeeded")
                return {"success": True, "cluster": data}
            logger.warning("auto_provision_request_rejected")
            return {"success": False, "error": "auto_provision_request_rejected"}

    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"success": False, "error": exc.code}
    except httpx.TimeoutException:
        logger.error("auto_provision_timeout")
        return {"success": False, "error": "auto_provision_timeout"}
    except Exception:
        logger.error("auto_provision_unavailable")
        return {"success": False, "error": "auto_provision_unavailable"}
