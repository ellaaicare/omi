#!/usr/bin/env python3
"""
Identity Sync — Sync-on-write helpers + daily reconciliation safety net.

PRIMARY SYNC happens at write-time:
    - Dashboard API routes call the corresponding Hermes identity sync
    - auto_provision.py sends full identity data during provisioning
    - OMI backend can call sync_user_identity() after user profile changes

This module provides:
    1. sync_user_identity()                — synchronous, stdlib-only, safe for any context
    2. sync_user_identity_fire_and_forget() — spawns thread, never raises, never blocks
    3. async_sync_user_identity()           — async version for FastAPI/asyncio code
    4. reconcile()                          — daily safety net (catches drift)

Daily reconciliation cron (safety net only):
    0 4 * * * /usr/bin/python3 /path/to/identity_sync.py reconcile >> /var/log/identity-sync.log 2>&1

One-off sync:
    python3 identity_sync.py sync <omi_uid> [phone] [email]

Environment variables:
    ELLA_POSTGRES_HOST       (default: 127.0.0.1)
    ELLA_POSTGRES_PORT       (default: 5433)
    ELLA_POSTGRES_USER       (default: postgres)
    ELLA_POSTGRES_PASSWORD   (default: postgres)
    ELLA_POSTGRES_DB         (default: ella_ai)
    ELLA_HERMES_PROVISION_API_URL   (required; exact reviewed endpoint)
    ELLA_HERMES_PROVISION_API_TOKEN (required)
    ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF (required env: secret reference)
    IDENTITY_SYNC_STATE_FILE (default: ~/.ella-identity-sync-state.json)
"""

import asyncio
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - the synchronous helper remains stdlib-only
    httpx = None

from ella.utils.provision_authority import (
    ProvisionAuthorityError,
    ProvisionAuthoritySnapshot,
    hermes_provision_authority,
)

logger = logging.getLogger("ella.identity_sync")

# --- Configuration ---

POSTGRES_HOST = os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.getenv("ELLA_POSTGRES_PORT", "5433"))
POSTGRES_USER = os.getenv("ELLA_POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("ELLA_POSTGRES_PASSWORD", "postgres")
POSTGRES_DB = os.getenv("ELLA_POSTGRES_DB", "ella_ai")

STATE_FILE = Path(os.getenv("IDENTITY_SYNC_STATE_FILE", os.path.expanduser("~/.ella-identity-sync-state.json")))


# --- Sync-on-Write API (stdlib only — no external deps) ---


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_IDENTITY_SYNC_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


def _identity_payload(omi_uid: str, phone: str = None, email: str = None) -> dict:
    payload = {"omiUid": omi_uid}
    if phone:
        payload["phone"] = phone
    if email:
        payload["email"] = email
    return payload


def _sync_user_identity_with_authority(
    omi_uid: str,
    *,
    phone: str = None,
    email: str = None,
    authority_snapshot: ProvisionAuthoritySnapshot | None = None,
) -> dict:
    send_snapshot: ProvisionAuthoritySnapshot | None = None
    try:
        payload_authority = hermes_provision_authority(authority_snapshot)
        payload = _identity_payload(omi_uid, phone=phone, email=email)
        authority = hermes_provision_authority(payload_authority.snapshot())
        send_snapshot = authority.snapshot()
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"status": "error", "error": exc.code}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {authority.token}",
    }
    url = f"{authority.base_url}/identity-sync"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with _IDENTITY_SYNC_OPENER.open(req, timeout=15) as resp:
            response_body = resp.read()
        hermes_provision_authority(send_snapshot)
        result = json.loads(response_body.decode("utf-8"))
        logger.info("identity_sync_succeeded")
        return result
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"status": "error", "error": exc.code}
    except urllib.error.HTTPError as exc:
        if send_snapshot is not None:
            try:
                hermes_provision_authority(send_snapshot)
            except ProvisionAuthorityError as authority_error:
                logger.error(authority_error.code)
                return {"status": "error", "error": authority_error.code}
        if exc.code == 404:
            logger.debug("identity_sync_not_provisioned")
            return {"status": "skipped", "reason": "not provisioned"}
        logger.warning("identity_sync_http_error")
        return {"status": "error", "error": "identity_sync_http_error"}
    except (urllib.error.URLError, OSError):
        if send_snapshot is not None:
            try:
                hermes_provision_authority(send_snapshot)
            except ProvisionAuthorityError as authority_error:
                logger.error(authority_error.code)
                return {"status": "error", "error": authority_error.code}
        logger.warning("identity_sync_unavailable")
        return {"status": "error", "error": "identity_sync_unavailable"}


def sync_user_identity(omi_uid: str, phone: str = None, email: str = None) -> dict:
    """Sync a user's identity data to the isolated Hermes provision authority.

    Calls POST /identity-sync with the given phone/email. Merges with
    existing identity links (doesn't replace).

    This is synchronous and uses only stdlib (urllib). Safe to call from
    any Python context — threads, asyncio callbacks, cron scripts.

    Args:
        omi_uid: Firebase UID
        phone: Phone number (e.g., "+14156402234") or None
        email: Email address or None

    Returns:
        dict with 'status' key: 'ok', 'skipped', or 'error'
    """
    if not omi_uid:
        return {"status": "skipped", "reason": "no omi_uid"}

    if not phone and not email:
        return {"status": "skipped", "reason": "no phone or email"}

    try:
        authority_snapshot = hermes_provision_authority().snapshot()
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"status": "error", "error": exc.code}
    return _sync_user_identity_with_authority(
        omi_uid,
        phone=phone,
        email=email,
        authority_snapshot=authority_snapshot,
    )


def sync_user_identity_fire_and_forget(omi_uid: str, phone: str = None, email: str = None) -> None:
    """Fire-and-forget identity sync — spawns a thread, never raises, never blocks.

    Use this from synchronous code paths where you don't need the result
    and must not block the caller (e.g., webhook handlers, middleware).
    """
    if not omi_uid or (not phone and not email):
        return

    try:
        authority_snapshot = hermes_provision_authority().snapshot()
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return

    def _do_sync():
        try:
            _sync_user_identity_with_authority(
                omi_uid,
                phone=phone,
                email=email,
                authority_snapshot=authority_snapshot,
            )
        except Exception:
            pass  # Never propagate

    try:
        hermes_provision_authority(authority_snapshot)
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return
    t = threading.Thread(target=_do_sync, daemon=True)
    try:
        hermes_provision_authority(authority_snapshot)
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return
    t.start()


async def async_sync_user_identity(omi_uid: str, phone: str = None, email: str = None) -> dict:
    """Async identity sync for FastAPI/asyncio code.

    Uses httpx if available, falls back to running sync version in a thread.
    """
    if not omi_uid or (not phone and not email):
        return {"status": "skipped", "reason": "no data"}

    try:
        authority_snapshot = hermes_provision_authority().snapshot()
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"status": "error", "error": exc.code}

    if httpx is None:
        try:
            hermes_provision_authority(authority_snapshot)
        except ProvisionAuthorityError as exc:
            logger.error(exc.code)
            return {"status": "error", "error": exc.code}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: _sync_user_identity_with_authority(
                omi_uid,
                phone=phone,
                email=email,
                authority_snapshot=authority_snapshot,
            ),
        )

    send_snapshot: ProvisionAuthoritySnapshot | None = None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            payload_authority = hermes_provision_authority(authority_snapshot)
            payload = _identity_payload(omi_uid, phone=phone, email=email)
            authority = hermes_provision_authority(payload_authority.snapshot())
            send_snapshot = authority.snapshot()
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {authority.token}",
            }
            url = f"{authority.base_url}/identity-sync"
            resp = await client.post(url, headers=headers, json=payload)
            hermes_provision_authority(send_snapshot)
            if resp.status_code == 404:
                return {"status": "skipped", "reason": "not provisioned"}
            resp.raise_for_status()
            result = resp.json()
            logger.info("identity_sync_succeeded")
            return result
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return {"status": "error", "error": exc.code}
    except Exception:
        if send_snapshot is not None:
            try:
                hermes_provision_authority(send_snapshot)
            except ProvisionAuthorityError as exc:
                logger.error(exc.code)
                return {"status": "error", "error": exc.code}
        logger.warning("identity_sync_unavailable")
        return {"status": "error", "error": "identity_sync_unavailable"}


# --- Daily Reconciliation (Safety Net) ---


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_check": "2020-01-01T00:00:00Z"}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_check": "2020-01-01T00:00:00Z"}


def _save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    tmp.replace(STATE_FILE)


def _get_changed_users(last_check: str) -> list:
    """Query users with updated_at > last_check. Requires psycopg2 or pg8000."""
    query = """
        SELECT omi_uid, email, name, identities, updated_at
        FROM users
        WHERE updated_at > %s
          AND omi_uid IS NOT NULL
        ORDER BY updated_at ASC
        LIMIT 500
    """

    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            dbname=POSTGRES_DB,
        )
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, (last_check,))
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except ImportError:
        pass

    try:
        import pg8000

        conn = pg8000.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DB,
        )
        try:
            cur = conn.cursor()
            cur.execute(query, (last_check,))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except ImportError:
        pass

    logger.error("identity_reconcile_database_driver_unavailable")
    return []


def reconcile():
    """Daily reconciliation — scan for changed users and sync their identities.

    Designed to run once daily as a safety net. Primary sync is sync-on-write
    via dashboard routes and auto_provision.py.
    """
    try:
        authority_snapshot = hermes_provision_authority().snapshot()
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return

    state = _load_state()
    try:
        hermes_provision_authority(authority_snapshot)
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return
    last_check = state.get("last_check", "2020-01-01T00:00:00Z")

    logger.info("identity_reconcile_started")

    try:
        hermes_provision_authority(authority_snapshot)
        changed_users = _get_changed_users(last_check)
        hermes_provision_authority(authority_snapshot)
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return
    except Exception:
        logger.error("identity_reconcile_database_unavailable")
        return

    if not changed_users:
        logger.info("identity_reconcile_no_changes")
        return

    logger.info("identity_reconcile_changes_detected")

    synced = 0
    errors = 0
    latest_updated_at = last_check

    for user in changed_users:
        omi_uid = user.get("omi_uid")
        if not omi_uid:
            continue

        email = user.get("email")

        # Parse identities JSONB for phone
        identities = user.get("identities")
        if isinstance(identities, str):
            try:
                identities = json.loads(identities)
            except json.JSONDecodeError:
                identities = {}
        elif not isinstance(identities, dict):
            identities = {}

        phone = identities.get("phone")

        if not email and not phone:
            continue

        result = _sync_user_identity_with_authority(
            omi_uid,
            phone=phone,
            email=email,
            authority_snapshot=authority_snapshot,
        )
        if str(result.get("error") or "").startswith(("hermes_provision_authority", "provision_authority")):
            return
        if result.get("status") in ("ok", "skipped"):
            synced += 1
        else:
            errors += 1

        # Track watermark
        user_updated = user.get("updated_at")
        if user_updated:
            if hasattr(user_updated, "isoformat"):
                user_updated = user_updated.isoformat()
            user_updated_str = str(user_updated)
            if user_updated_str > latest_updated_at:
                latest_updated_at = user_updated_str

    state["last_check"] = latest_updated_at
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_synced"] = synced
    state["last_errors"] = errors
    try:
        hermes_provision_authority(authority_snapshot)
        _save_state(state)
        hermes_provision_authority(authority_snapshot)
    except ProvisionAuthorityError as exc:
        logger.error(exc.code)
        return

    logger.info("identity_reconcile_completed")


# --- CLI ---

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [identity-sync] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 identity_sync.py reconcile              # Daily safety-net scan")
        print("  python3 identity_sync.py sync <uid> [phone] [email]  # One-off sync")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "reconcile":
        reconcile()
    elif cmd == "sync":
        if len(sys.argv) < 3:
            print("Usage: python3 identity_sync.py sync <omi_uid> [phone] [email]")
            sys.exit(1)
        uid = sys.argv[2]
        phone = sys.argv[3] if len(sys.argv) > 3 else None
        email = sys.argv[4] if len(sys.argv) > 4 else None
        result = sync_user_identity(uid, phone=phone, email=email)
        print(json.dumps(result, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: reconcile, sync")
        sys.exit(1)
