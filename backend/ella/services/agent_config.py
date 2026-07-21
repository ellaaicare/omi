from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from ella.routers import resolve as resolve_router

logger = logging.getLogger(__name__)

DEFAULT_MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "openai-codex": ["gpt-5.5", "gpt-5.5-mini", "gpt-5.4", "gpt-5.4-mini"],
    "openai": ["gpt-5.5", "gpt-5.5-mini", "gpt-5.4", "gpt-5.4-mini", "gpt-4.1", "gpt-4.1-mini"],
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openrouter": ["x-ai/grok-4.1-fast", "anthropic/claude-sonnet-4-6", "openai/gpt-5.5"],
    "ollama-cloud": ["kimi-k2.6", "gpt-oss:120b", "qwen3-coder"],
}

DEFAULT_HERMES_PROVIDER = os.getenv("ELLA_AGENT_CONFIG_DEFAULT_PROVIDER", "openai-codex").strip()
DEFAULT_HERMES_MODEL = os.getenv("ELLA_AGENT_CONFIG_DEFAULT_MODEL", "gpt-5.5").strip()
HERMES_PROVISION_URL = os.getenv("HERMES_PROVISION_API_URL", "http://100.76.138.56:8210").rstrip("/")
HERMES_PROVISION_TOKEN = os.getenv(
    "HERMES_PROVISION_API_TOKEN",
    os.getenv("ELLA_PROVISION_API_TOKEN", os.getenv("ELLA_PROVISION_API_KEY", "")),
).strip()
HERMES_AGENT_ID = os.getenv("HERMES_AGENT_ID", "hermes").strip() or "hermes"


@dataclass(frozen=True)
class AgentRouting:
    platform: str
    agent_id: str
    provision_url: str
    provision_token: str
    profile: str | None = None


def models_by_provider() -> dict[str, list[str]]:
    raw = os.getenv("ELLA_AGENT_CONFIG_MODELS_BY_PROVIDER_JSON", "").strip()
    if not raw:
        return {key: list(value) for key, value in DEFAULT_MODELS_BY_PROVIDER.items()}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid ELLA_AGENT_CONFIG_MODELS_BY_PROVIDER_JSON: %s", exc)
        return {key: list(value) for key, value in DEFAULT_MODELS_BY_PROVIDER.items()}
    if not isinstance(parsed, dict):
        return {key: list(value) for key, value in DEFAULT_MODELS_BY_PROVIDER.items()}
    normalized: dict[str, list[str]] = {}
    for provider, models in parsed.items():
        if not isinstance(provider, str) or not isinstance(models, list):
            continue
        clean_models = [str(model).strip() for model in models if str(model).strip()]
        if provider.strip() and clean_models:
            normalized[provider.strip()] = clean_models
    return normalized or {key: list(value) for key, value in DEFAULT_MODELS_BY_PROVIDER.items()}


def options_payload() -> dict[str, Any]:
    options = models_by_provider()
    return {
        "providers": list(options.keys()),
        "modelsByProvider": options,
    }


def validate_provider_model(provider: str, model: str) -> tuple[str, str]:
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider:
        raise HTTPException(status_code=422, detail={"error": "provider_required"})
    if not model:
        raise HTTPException(status_code=422, detail={"error": "model_required"})
    options = models_by_provider()
    if provider not in options:
        raise HTTPException(
            status_code=422,
            detail={"error": "provider_not_allowed", "provider": provider, "allowed_providers": list(options.keys())},
        )
    if model not in options[provider]:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "model_not_allowed_for_provider",
                "provider": provider,
                "model": model,
                "allowed_models": options[provider],
            },
        )
    return provider, model


async def resolve_agent_routing(uid: str) -> AgentRouting:
    resolved = await resolve_router.resolve_user_routing(uid)
    routing = (resolved or {}).get("routing") or {}
    platform = str(routing.get("platform") or resolve_router.CHAT_PLATFORM or "openclaw").strip().lower()
    if platform != "hermes":
        return AgentRouting(
            platform=platform,
            agent_id=str(routing.get("agentId") or HERMES_AGENT_ID),
            provision_url=str(routing.get("provisionUrl") or HERMES_PROVISION_URL).rstrip("/"),
            provision_token=str(routing.get("provisionToken") or HERMES_PROVISION_TOKEN),
        )
    return AgentRouting(
        platform="hermes",
        agent_id=str(routing.get("agentId") or HERMES_AGENT_ID),
        provision_url=str(routing.get("provisionUrl") or HERMES_PROVISION_URL).rstrip("/"),
        provision_token=str(routing.get("provisionToken") or HERMES_PROVISION_TOKEN),
        profile=str(routing.get("profile") or "") or None,
    )


def _auth_headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_response(
    *,
    uid: str,
    routing: AgentRouting,
    provider: str,
    model: str,
    source: dict[str, Any],
    cache: dict[str, Any] | None = None,
    reload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "uid": uid,
        "platform": routing.platform,
        "provider": provider,
        "model": model,
        "editable": {
            "platform": False,
            "provider": routing.platform == "hermes",
            "model": routing.platform == "hermes",
        },
        "options": options_payload(),
        "source": source,
        "cache": cache or {"invalidated": False},
        "reload": reload or {"status": "not_requested"},
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def get_agent_config(uid: str) -> dict[str, Any]:
    routing = await resolve_agent_routing(uid)
    if routing.platform != "hermes":
        return _base_response(
            uid=uid,
            routing=routing,
            provider=DEFAULT_HERMES_PROVIDER,
            model=DEFAULT_HERMES_MODEL,
            source={
                "runtime": "openclaw",
                "profile": routing.profile,
                "override": "legacy_openclaw_fallback",
                "agent_id": routing.agent_id,
                "read_only_reason": "active chat routing is not Hermes",
            },
        )

    url = f"{routing.provision_url}/agent-config"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                params={"uid": uid, "agent_id": routing.agent_id},
                headers=_auth_headers(routing.provision_token),
            )
        if response.status_code == 404:
            raise RuntimeError("Hermes provision shim does not expose /agent-config")
        if response.status_code >= 400:
            raise RuntimeError(f"Hermes provision shim returned {response.status_code}: {response.text[:300]}")
        payload = response.json()
        provider = str(payload.get("provider") or DEFAULT_HERMES_PROVIDER)
        model = str(payload.get("model") or DEFAULT_HERMES_MODEL)
        return _base_response(
            uid=uid,
            routing=routing,
            provider=provider,
            model=model,
            source={
                "runtime": "hermes",
                "profile": payload.get("profile"),
                "override": payload.get("override") or "profile",
                "agent_id": routing.agent_id,
                "config_path": payload.get("config_path"),
            },
            cache=payload.get("cache") if isinstance(payload.get("cache"), dict) else None,
            reload=payload.get("reload") if isinstance(payload.get("reload"), dict) else None,
        )
    except Exception as exc:  # noqa: BLE001 - API must stay readable while Hermes shim is rolling out.
        logger.warning("Hermes agent config read failed for uid=%s agent=%s: %s", uid, routing.agent_id, exc)
        return _base_response(
            uid=uid,
            routing=routing,
            provider=DEFAULT_HERMES_PROVIDER,
            model=DEFAULT_HERMES_MODEL,
            source={
                "runtime": "hermes",
                "profile": routing.profile,
                "override": "hermes_unavailable_default",
                "agent_id": routing.agent_id,
                "warning": str(exc),
            },
        )


async def invalidate_model_cache(uid: str, agent_id: str) -> dict[str, Any]:
    try:
        pool = await resolve_router._get_pool()
        updated = await pool.execute(
            """
            UPDATE agent_configs
               SET is_stale = TRUE, updated_at = NOW()
             WHERE agent_id = $1
                OR user_id IN (SELECT id FROM users WHERE omi_uid = $2)
            """,
            agent_id,
            uid,
        )
        return {"invalidated": True, "store": "postgres.agent_configs", "result": updated}
    except Exception as exc:  # noqa: BLE001 - cache table is optional during rollout.
        logger.info("Agent config cache invalidation skipped uid=%s agent=%s: %s", uid, agent_id, exc)
        return {
            "invalidated": False,
            "store": "postgres.agent_configs",
            "reason": "cache_unavailable_or_schema_absent",
        }


async def patch_agent_config(uid: str, provider: str, model: str) -> dict[str, Any]:
    provider, model = validate_provider_model(provider, model)
    routing = await resolve_agent_routing(uid)
    if routing.platform != "hermes":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "active_platform_not_editable",
                "platform": routing.platform,
                "message": "Agent config writes are only enabled for active Hermes routing.",
            },
        )

    url = f"{routing.provision_url}/agent-config"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.patch(
                url,
                params={"uid": uid, "agent_id": routing.agent_id},
                headers=_auth_headers(routing.provision_token),
                json={"provider": provider, "model": model},
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "hermes_agent_config_patch_failed",
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )
        payload = response.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail={"error": "hermes_agent_config_unreachable", "message": str(exc)},
        ) from exc

    cache = await invalidate_model_cache(uid, routing.agent_id)
    return _base_response(
        uid=uid,
        routing=routing,
        provider=str(payload.get("provider") or provider),
        model=str(payload.get("model") or model),
        source={
            "runtime": "hermes",
            "profile": payload.get("profile"),
            "override": payload.get("override") or "profile",
            "agent_id": routing.agent_id,
            "config_path": payload.get("config_path"),
        },
        cache=cache,
        reload=payload.get("reload") if isinstance(payload.get("reload"), dict) else {"status": "unknown"},
    )
