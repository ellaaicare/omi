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

    async def no_direct_runtime(_uid):
        return None

    monkeypatch.setattr(voice, "self_hosted_runtime_authority_required", authority_disabled)
    monkeypatch.setattr(voice, "resolve_direct_self_hosted_runtime", no_direct_runtime)


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


class _NoRowConn:
    async def fetchrow(self, *args, **kwargs):
        return None


class _RowConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *args, **kwargs):
        return self._row


async def _noop(*args, **kwargs):
    return None


def _zero_rollup(fixed):
    async def rollup(conn, uid, now):
        return {
            "daily_used_s": 0,
            "monthly_used_s": 0,
            "daily_cost_microusd": 0,
            "monthly_cost_microusd": 0,
            "daily_resets_at": fixed.isoformat(),
            "monthly_resets_at": fixed.isoformat(),
        }

    return rollup


def test_fresh_uid_entitlement_contract_respects_relax_flag(monkeypatch):
    fixed = datetime(2026, 8, 8, 2, 0, 0, tzinfo=timezone.utc)

    async def noop_lock(*args, **kwargs):
        return None

    monkeypatch.setattr(voice_canary, "lock_runtime_authority_on_connection", noop_lock)

    async def build():
        return await voice_canary.get_entitlement_contract_for_connection(
            _NoRowConn(), "uid-fresh", now=fixed, expire_stale_sessions=False
        )

    # Flag unset -> status "none" (client invite gate stays up).
    monkeypatch.delenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", raising=False)
    contract = asyncio.run(build())
    assert contract["status"] == "none"
    assert "plan" not in contract

    # Flag set -> provisionable "invited" status so the client proceeds to ensure.
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "true")
    contract = asyncio.run(build())
    assert contract["status"] == "invited"
    assert contract["plan"] == "canary"
    assert contract["revision"] == 0
    assert "quota" in contract


def test_existing_revoked_row_unaffected_by_relax_flag(monkeypatch):
    # Sophia criterion 3: the relax lives strictly in the `if not row:` branch.
    # An existing row (revoked/suspended) keeps its real status even with the
    # relax flag on - it must NOT become provisionable through this change.
    fixed = datetime(2026, 8, 8, 2, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(voice_canary, "lock_runtime_authority_on_connection", _noop)
    monkeypatch.setattr(voice_canary, "_usage_rollup", _zero_rollup(fixed))

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "true")
    revoked = {
        "status": "revoked",
        "plan": "canary",
        "revision": 3,
        "daily_limit_s": 2700,
        "monthly_limit_s": 43200,
        "max_session_s": 1200,
        "max_concurrent": 1,
        "soft_limit_ratio": voice_canary.DEFAULT_SOFT_LIMIT_RATIO,
    }

    async def build():
        return await voice_canary.get_entitlement_contract_for_connection(
            _RowConn(revoked), "uid-existing", now=fixed, expire_stale_sessions=False
        )

    contract = asyncio.run(build())
    assert contract["status"] == "revoked"
    assert contract["revision"] == 3
    assert contract["plan"] == "canary"


def test_fresh_contract_relax_alone_does_not_open_voice_session(monkeypatch):
    # Sophia criterion 2 / 5: gate pass (contract "invited") is NOT session-open.
    # Session-open reads the real voice_entitlements row; a genuinely fresh no-row
    # user is still denied (no_entitlement) even with the relax flag on. This pins
    # that real fresh-user voice availability is a separate concern from the
    # contract status - proving `active` from this contract could not un-brick it
    # either.
    monkeypatch.setattr(voice_canary, "lock_runtime_authority_on_connection", _noop)
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "true")

    async def run():
        return await voice_canary._runtime_activation_decision(
            _NoRowConn(),
            uid="uid-fresh",
            provider="grok-voice",
            model="grok-4",
            require_active=True,
        )

    decision = asyncio.run(run())
    assert decision.allowed is False
    assert decision.code == "no_entitlement"


def test_operator_defaults_are_grok_only_and_have_no_fallback():
    assert voice_canary_admin.DEFAULT_PROVIDERS == ["grok-voice"]
    assert voice_canary_admin.DEFAULT_MODES == ["v4"]
    assert voice_canary_admin.DEFAULT_FALLBACK_POLICY == {
        "enabled": False,
        "order": [],
    }
