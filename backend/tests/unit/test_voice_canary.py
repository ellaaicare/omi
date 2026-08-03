import asyncio
import sys
from datetime import datetime, timezone
from types import ModuleType

import jwt
import pytest
from fastapi import HTTPException

conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value, uid=None: value
sys.modules.setdefault("database.conversations", conversations_module)
sys.modules.setdefault("database.proposals", ModuleType("database.proposals"))
sys.modules.setdefault("websockets", ModuleType("websockets"))

from database import voice_canary
from ella.routers import voice
from scripts import voice_canary_admin


@pytest.fixture(autouse=True)
def retained_runtime_not_invitation_owned(monkeypatch):
    async def authority_disabled(_uid):
        return False

    monkeypatch.setattr(voice, "self_hosted_runtime_authority_required", authority_disabled)


def _entitlement(**overrides):
    value = {
        "revision": 7,
        "daily_limit_s": 2700,
        "monthly_limit_s": 43200,
        "daily_cost_limit_microusd": None,
        "monthly_cost_limit_microusd": None,
        "soft_limit_ratio": 0.8,
        "hard_limit_ratio": 1.0,
        "max_session_s": 1200,
        "max_concurrent": 1,
        "max_audio_bytes_per_session": 120_000_000,
    }
    value.update(overrides)
    return value


def test_quota_state_warns_and_stops_on_exact_boundaries():
    entitlement = _entitlement()

    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=2159,
        monthly_used_s=0,
    ) == ("ok", False)
    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=2160,
        monthly_used_s=0,
    ) == ("soft_warning", True)
    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=2700,
        monthly_used_s=0,
    ) == ("quota_daily", False)
    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=0,
        monthly_used_s=43200,
    ) == ("quota_monthly", False)
    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=0,
        monthly_used_s=0,
        session_used_s=1200,
    ) == ("session_max", False)
    assert voice_canary.quota_state(
        entitlement,
        daily_used_s=0,
        monthly_used_s=0,
        audio_bytes=120_000_000,
    ) == ("audio_limit", False)


def test_unentitled_session_issuance_returns_typed_denial(monkeypatch):
    async def deny(**kwargs):
        return voice_canary.VoicePolicyDecision(
            allowed=False,
            code="no_entitlement",
            entitlement=None,
            quota={},
        )

    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice.voice_canary_db, "evaluate_issuance", deny)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="uid-a"),
                authenticated_uid="uid-a",
            )
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "no_entitlement"


def test_entitled_session_has_bounded_jwt_and_quota_contract(monkeypatch):
    entitlement = _entitlement()
    quota = {
        "daily_used_s": 60,
        "daily_limit_s": 2700,
        "monthly_used_s": 600,
        "monthly_limit_s": 43200,
        "max_session_s": 1200,
        "max_concurrent": 1,
        "soft_limit_ratio": 0.8,
        "resets_at": "2026-07-27T00:00:00+00:00",
    }

    async def allow(**kwargs):
        return voice_canary.VoicePolicyDecision(
            allowed=True,
            code="ok",
            entitlement=entitlement,
            quota=quota,
        )

    class Pool:
        async def fetchrow(self, *args):
            return {"name": "Alex"}

    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(voice, "SESSION_EXPIRY_MINUTES", 25)
    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice.voice_canary_db, "evaluate_issuance", allow)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))

    response = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(uid="uid-a"),
            authenticated_uid="uid-a",
        )
    )
    claims = jwt.decode(
        response.session_token,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        audience=voice.VOICE_SESSION_AUDIENCE,
        issuer="omi-backend",
    )

    lifetime_s = claims["exp"] - claims["iat"]
    assert 20 * 60 < lifetime_s <= 25 * 60
    assert claims["entitlement_revision"] == 7
    assert claims["correlation_id"]
    assert response.quota == quota
    assert response.expires_in == 25 * 60
    assert "token=" not in response.voice_endpoint
    assert response.voice_endpoint.endswith("?mode=v4")


def test_entitlement_contract_defaults_are_canary_numbers():
    assert voice_canary.DEFAULT_DAILY_LIMIT_S == 45 * 60
    assert voice_canary.DEFAULT_MONTHLY_LIMIT_S == 12 * 60 * 60
    assert voice_canary.DEFAULT_MAX_SESSION_S == 20 * 60
    assert voice_canary.DEFAULT_MAX_CONCURRENT == 1
    assert datetime.now(timezone.utc).tzinfo is not None


def test_get_pool_uses_real_asyncpg_entrypoint(monkeypatch):
    pool = object()
    calls = []

    async def create_pool(**kwargs):
        calls.append(kwargs)
        return pool

    monkeypatch.setattr(voice_canary, "_pool", None)
    monkeypatch.setattr(voice_canary.asyncpg, "create_pool", create_pool)
    monkeypatch.setenv("ELLA_POSTGRES_DSN", "postgresql://voice-canary.test/authority")

    assert asyncio.run(voice_canary.get_pool()) is pool
    assert calls == [
        {
            "dsn": "postgresql://voice-canary.test/authority",
            "min_size": 1,
            "max_size": 10,
        }
    ]


def test_operator_defaults_are_grok_only_and_have_no_fallback():
    assert voice_canary_admin.DEFAULT_PROVIDERS == ["grok-voice"]
    assert voice_canary_admin.DEFAULT_MODES == ["v4"]
    assert voice_canary_admin.DEFAULT_FALLBACK_POLICY == {
        "enabled": False,
        "order": [],
    }
