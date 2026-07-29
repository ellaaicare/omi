"""Bounded, profile-isolated Honcho reads for realtime voice."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ella.services.correction_honcho_contract import resolve_companion_honcho_target

logger = logging.getLogger(__name__)

HONCHO_BASE_URL = os.getenv("ELLA_VOICE_HONCHO_BASE_URL", "http://100.76.138.56:8320").rstrip("/")
HONCHO_API_KEY = os.getenv("ELLA_VOICE_HONCHO_API_KEY", os.getenv("HONCHO_API_KEY", "")).strip()
HONCHO_TIMEOUT_SECONDS = float(os.getenv("ELLA_VOICE_HONCHO_TIMEOUT_SECONDS", "1.5"))
HONCHO_CONTEXT_MAX_CHARS = int(os.getenv("ELLA_VOICE_HONCHO_CONTEXT_MAX_CHARS", "2400"))


@dataclass(frozen=True)
class VoiceHonchoTarget:
    uid: str
    honcho_workspace: str
    observer_peer: str
    observed_peer: str
    source: str


def resolve_voice_honcho_target(
    uid: str,
    runtime: Any = None,
    *,
    profile_map_timeout_seconds: float | None = None,
) -> tuple[VoiceHonchoTarget | None, str]:
    """Prefer an isolated runtime receipt; otherwise require an exact UID profile."""
    uid = str(uid or "").strip()
    if runtime is not None:
        runtime_uid = str(getattr(runtime, "uid", "") or "").strip()
        if runtime_uid and runtime_uid != uid:
            return None, "runtime_honcho_owner_mismatch"
        if str(getattr(runtime, "provider", "") or "").strip().lower() == "hermes_cloud":
            return None, "hermes_cloud_profile_memory_builtin"
        workspace = str(getattr(runtime, "honcho_workspace", "") or "").strip()
        observer = str(getattr(runtime, "observer_peer", "") or "").strip()
        observed = str(getattr(runtime, "observed_peer", "") or "").strip()
        if not all((workspace, observer, observed)):
            return None, "runtime_honcho_binding_missing"
        return (
            VoiceHonchoTarget(
                uid=uid,
                honcho_workspace=workspace,
                observer_peer=observer,
                observed_peer=observed,
                source="isolated_runtime_receipt",
            ),
            "",
        )

    if profile_map_timeout_seconds is None:
        target, reason = resolve_companion_honcho_target(uid)
    else:
        target, reason = resolve_companion_honcho_target(
            uid,
            remote_timeout_seconds=profile_map_timeout_seconds,
        )
    if not target:
        return None, reason
    return (
        VoiceHonchoTarget(
            uid=uid,
            honcho_workspace=target["workspace"],
            observer_peer=target["observer_peer_id"],
            observed_peer=target["observed_peer_id"],
            source=str(target.get("source") or "companion_profile"),
        ),
        "",
    )


def _target(runtime: Any) -> tuple[str, str, str] | None:
    workspace = str(getattr(runtime, "honcho_workspace", "") or "").strip()
    observer = str(getattr(runtime, "observer_peer", "") or "").strip()
    observed = str(getattr(runtime, "observed_peer", "") or "").strip()
    if not all((workspace, observer, observed)):
        return None
    return workspace, observer, observed


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if HONCHO_API_KEY:
        headers["Authorization"] = f"Bearer {HONCHO_API_KEY}"
    return headers


def _bounded_context(body: Any, max_chars: int) -> str:
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    representation = str(body.get("representation") or "").strip()
    if representation:
        parts.append(representation)
    peer_card = body.get("peer_card")
    if isinstance(peer_card, list):
        cards = [str(item).strip() for item in peer_card if str(item).strip()]
        if cards:
            parts.append("Known user facts:\n" + "\n".join(f"- {item}" for item in cards))
    return "\n\n".join(parts)[: max(0, max_chars)].strip()


async def fetch_voice_honcho_context(
    runtime: Any,
    *,
    query: str,
    top_k: int = 12,
    max_chars: int = HONCHO_CONTEXT_MAX_CHARS,
) -> dict[str, Any]:
    """Return semantic Honcho context or a degraded result without raising."""
    target = _target(runtime)
    if target is None:
        return {"available": False, "reason": "runtime_honcho_binding_missing", "context": ""}

    workspace, observer, observed = target
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HONCHO_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{HONCHO_BASE_URL}/v3/workspaces/{quote(workspace, safe='')}"
                f"/peers/{quote(observer, safe='')}/context",
                headers=_headers(),
                params={
                    "target": observed,
                    "search_query": query[:1200],
                    "search_top_k": min(max(top_k, 1), 100),
                    "include_most_frequent": "true",
                    "max_conclusions": min(max(top_k, 1), 100),
                },
            )
        if response.status_code != 200:
            logger.warning(
                "[FLOW:VOICE-HONCHO] context unavailable uid=%s status=%s",
                getattr(runtime, "uid", ""),
                response.status_code,
            )
            return {
                "available": False,
                "reason": f"honcho_http_{response.status_code}",
                "context": "",
            }
        body = response.json()
        if str(body.get("peer_id") or "") != observer or str(body.get("target_id") or "") != observed:
            logger.warning(
                "[FLOW:VOICE-HONCHO] context target mismatch uid=%s",
                getattr(runtime, "uid", ""),
            )
            return {"available": False, "reason": "honcho_target_mismatch", "context": ""}
        context = _bounded_context(body, max_chars)
        return {
            "available": bool(context),
            "reason": "ok" if context else "honcho_context_empty",
            "context": context,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "source": "honcho",
        }
    except Exception as exc:
        logger.warning(
            "[FLOW:VOICE-HONCHO] context degraded uid=%s error=%s",
            getattr(runtime, "uid", ""),
            type(exc).__name__,
        )
        return {
            "available": False,
            "reason": "honcho_timeout" if isinstance(exc, httpx.TimeoutException) else "honcho_unavailable",
            "context": "",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


async def search_voice_honcho(runtime: Any, query: str, limit: int) -> list[dict[str, Any]]:
    """Semantically search the exact receipt-bound Honcho peer pair."""
    target = _target(runtime)
    if target is None:
        return []

    workspace, observer, observed = target
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HONCHO_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{HONCHO_BASE_URL}/v3/workspaces/{quote(workspace, safe='')}/conclusions/query",
                headers=_headers(),
                json={
                    "query": query[:1200],
                    "top_k": min(max(limit, 1), 10),
                    "filters": {"observer": observer, "observed": observed},
                },
            )
        if response.status_code != 200:
            logger.warning(
                "[FLOW:VOICE-HONCHO] search unavailable uid=%s status=%s",
                getattr(runtime, "uid", ""),
                response.status_code,
            )
            return []
        body = response.json()
        if not isinstance(body, list):
            return []
        latency_ms = int((time.monotonic() - started) * 1000)
        results: list[dict[str, Any]] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("observer_id") or item.get("observer") or "") != observer
                or str(item.get("observed_id") or item.get("observed") or "") != observed
            ):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            created_at = str(item.get("created_at") or "")
            results.append(
                {
                    "source": "honcho",
                    "title": "Related user context",
                    "content": content[:900],
                    "timestamp": created_at,
                    "score": 90,
                    "metadata": {
                        "provenance": "honcho_conclusion",
                        "conclusion_id": str(item.get("id") or ""),
                        "latency_ms": latency_ms,
                    },
                }
            )
        return results[:limit]
    except Exception as exc:
        logger.warning(
            "[FLOW:VOICE-HONCHO] search degraded uid=%s error=%s",
            getattr(runtime, "uid", ""),
            type(exc).__name__,
        )
        return []
