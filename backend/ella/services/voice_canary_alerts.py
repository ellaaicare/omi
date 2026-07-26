"""Secret-safe alert delivery for voice spend and authentication anomalies."""

import hashlib
import logging
import os
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)
VOICE_ALERT_WEBHOOK_URL = os.getenv("ELLA_VOICE_ALERT_WEBHOOK_URL", "").strip()
VOICE_ALERT_MIN_INTERVAL_SECONDS = int(os.getenv("ELLA_VOICE_ALERT_MIN_INTERVAL_SECONDS", "300"))
VOICE_SPEND_ANOMALY_MICROUSD = int(os.getenv("ELLA_VOICE_SPEND_ANOMALY_MICROUSD", "500000"))
_LAST_ALERT_AT: dict[str, float] = {}


def pseudonymous_uid(uid: str) -> str:
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:12]


async def send_canary_alert(
    kind: str,
    *,
    uid: str,
    code: str,
    details: dict[str, Any] | None = None,
) -> bool:
    dedupe_key = f"{kind}:{code}:{pseudonymous_uid(uid)}"
    now = time.monotonic()
    if now - _LAST_ALERT_AT.get(dedupe_key, 0) < VOICE_ALERT_MIN_INTERVAL_SECONDS:
        return False
    _LAST_ALERT_AT[dedupe_key] = now

    safe_details = {
        key: value
        for key, value in (details or {}).items()
        if key
        in {
            "count",
            "provider",
            "model",
            "estimated_cost_microusd",
            "daily_used_s",
            "monthly_used_s",
        }
    }
    payload = {
        "text": (
            f"Ella voice canary alert: {kind}\n" f"user={pseudonymous_uid(uid)} code={code}\n" f"details={safe_details}"
        )
    }
    if not VOICE_ALERT_WEBHOOK_URL:
        logger.warning(
            "voice_canary_alert_not_delivered kind=%s uid_hash=%s code=%s",
            kind,
            pseudonymous_uid(uid),
            code,
        )
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(VOICE_ALERT_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning(
            "voice_canary_alert_delivery_failed kind=%s uid_hash=%s error=%s",
            kind,
            pseudonymous_uid(uid),
            type(exc).__name__,
        )
        return False
