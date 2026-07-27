import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import asyncpg
import pytest

from database import voice_canary

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "008_create_voice_canary_controls.sql"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for voice canary PostgreSQL tests",
)


async def _run_with_database(
    scenario: Callable[[asyncpg.Pool], Awaitable[None]],
) -> None:
    pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=3)
    previous_pool = voice_canary._pool
    voice_canary._pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
            await conn.execute("""
                TRUNCATE TABLE
                    voice_active_sessions,
                    voice_usage_events,
                    voice_rate_limit_events,
                    voice_kill_switches,
                    voice_entitlements
                RESTART IDENTITY CASCADE
                """)
        await scenario(pool)
    finally:
        voice_canary._pool = previous_pool
        await pool.close()


async def _grant(
    pool: asyncpg.Pool,
    uid: str,
    *,
    trial_expires_at: datetime | None = None,
) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                INSERT INTO voice_entitlements (
                    uid, status, trial_started_at, trial_expires_at
                ) VALUES ($1, 'active', $2::timestamptz, $3::timestamptz)
                RETURNING revision
                """,
                uid,
                trial_expires_at - timedelta(hours=1) if trial_expires_at else None,
                trial_expires_at,
            )
        )


async def _accept(uid: str, session_id: str, revision: int):
    return await voice_canary.accept_session(
        uid=uid,
        session_id=session_id,
        correlation_id=f"trace-{session_id}",
        entitlement_revision=revision,
        provider="grok-voice",
        model="grok-voice",
        mode="v4",
    )


def test_lower_completion_cannot_overwrite_metered_heartbeat(monkeypatch):
    started_at = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    clock = {"now": started_at}
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: clock["now"])

    async def scenario(pool: asyncpg.Pool) -> None:
        revision = await _grant(pool, "uid-lower-completion")
        assert (await _accept("uid-lower-completion", "session-lower", revision)).allowed

        clock["now"] = started_at + timedelta(seconds=20)
        heartbeat = await voice_canary.update_session(
            uid="uid-lower-completion",
            session_id="session-lower",
            input_audio_s=12.5,
            output_audio_s=4.25,
            input_audio_bytes=600_000,
            output_audio_bytes=204_000,
            tool_calls=3,
            reconnects=2,
            provider_request_ids=["request-a", "request-b"],
            estimated_cost_microusd=9_000,
        )
        assert heartbeat.allowed

        clock["now"] = started_at + timedelta(seconds=40)
        event_id = await voice_canary.complete_session(
            uid="uid-lower-completion",
            session_id="session-lower",
            input_audio_s=1,
            output_audio_s=1,
            connection_s=1,
            input_audio_bytes=1,
            output_audio_bytes=1,
            tool_calls=1,
            reconnects=1,
            provider_request_ids=["request-a"],
            termination_reason="completed",
            normalized_error_code=None,
            estimated_cost_microusd=1,
        )
        assert event_id
        assert (
            await voice_canary.complete_session(
                uid="uid-lower-completion",
                session_id="session-lower",
                input_audio_s=99,
                output_audio_s=99,
                connection_s=99,
                input_audio_bytes=99,
                output_audio_bytes=99,
                tool_calls=99,
                reconnects=99,
                provider_request_ids=["request-c"],
                termination_reason="completed",
                normalized_error_code=None,
                estimated_cost_microusd=99,
            )
            is None
        )

        async with pool.acquire() as conn:
            event = dict(
                await conn.fetchrow(
                    """
                    SELECT *
                    FROM voice_usage_events
                    WHERE id = $1::uuid
                    """,
                    event_id,
                )
            )
            rollup = await voice_canary._usage_rollup(
                conn,
                "uid-lower-completion",
                clock["now"],
            )
        assert float(event["input_audio_s"]) == 12.5
        assert float(event["output_audio_s"]) == 4.25
        assert float(event["connection_s"]) == 40
        assert event["input_audio_bytes"] == 600_000
        assert event["output_audio_bytes"] == 204_000
        assert event["tool_calls"] == 3
        assert event["reconnects"] == 2
        assert event["estimated_cost_microusd"] == 9_000
        assert rollup["daily_used_s"] == 40
        assert rollup["monthly_used_s"] == 40
        assert rollup["daily_cost_microusd"] == 9_000
        assert rollup["monthly_cost_microusd"] == 9_000
        assert voice_canary._json_list(event["provider_request_ids"]) == [
            "request-a",
            "request-b",
        ]

    asyncio.run(_run_with_database(scenario))


def test_lease_expiry_keeps_last_metered_usage_after_proxy_crash(monkeypatch):
    started_at = datetime(2026, 7, 26, 19, 0, tzinfo=timezone.utc)
    clock = {"now": started_at}
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: clock["now"])
    monkeypatch.setattr(voice_canary, "VOICE_SESSION_LEASE_SECONDS", 45)

    async def scenario(pool: asyncpg.Pool) -> None:
        revision = await _grant(pool, "uid-proxy-crash")
        assert (await _accept("uid-proxy-crash", "session-crash", revision)).allowed

        clock["now"] = started_at + timedelta(seconds=10)
        heartbeat = await voice_canary.update_session(
            uid="uid-proxy-crash",
            session_id="session-crash",
            input_audio_s=7.5,
            output_audio_s=2.5,
            input_audio_bytes=360_000,
            output_audio_bytes=120_000,
            tool_calls=2,
            reconnects=1,
            provider_request_ids=["request-crash"],
            estimated_cost_microusd=7_500,
        )
        assert heartbeat.allowed

        clock["now"] = started_at + timedelta(seconds=60)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid="uid-proxy-crash",
                )
                await voice_canary._expire_stale_sessions(
                    conn,
                    clock["now"],
                    uid="uid-proxy-crash",
                )
            event = dict(await conn.fetchrow("""
                    SELECT *
                    FROM voice_usage_events
                    WHERE session_id = 'session-crash'
                      AND event_type = 'session_terminated'
                    """))
            active_count = await conn.fetchval(
                "SELECT COUNT(*) FROM voice_active_sessions WHERE session_id = 'session-crash'"
            )
            rollup = await voice_canary._usage_rollup(
                conn,
                "uid-proxy-crash",
                clock["now"],
            )

        assert active_count == 0
        assert event["termination_reason"] == "lease_expired"
        assert event["normalized_error_code"] == "session_lease_expired"
        assert float(event["input_audio_s"]) == 7.5
        assert float(event["output_audio_s"]) == 2.5
        assert float(event["connection_s"]) == 60
        assert event["input_audio_bytes"] == 360_000
        assert event["output_audio_bytes"] == 120_000
        assert event["estimated_cost_microusd"] == 7_500
        assert rollup["daily_used_s"] == 60
        assert rollup["monthly_used_s"] == 60
        assert rollup["daily_cost_microusd"] == 7_500
        assert rollup["monthly_cost_microusd"] == 7_500

    asyncio.run(_run_with_database(scenario))


def test_trial_expiry_is_rechecked_at_accept_and_heartbeat(monkeypatch):
    started_at = datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc)
    clock = {"now": started_at}
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: clock["now"])

    async def scenario(pool: asyncpg.Pool) -> None:
        expired_revision = await _grant(
            pool,
            "uid-expired-before-accept",
            trial_expires_at=started_at,
        )
        denied = await _accept(
            "uid-expired-before-accept",
            "session-expired-accept",
            expired_revision,
        )
        assert not denied.allowed
        assert denied.code == "expired"

        active_revision = await _grant(
            pool,
            "uid-expires-during-session",
            trial_expires_at=started_at + timedelta(seconds=10),
        )
        assert (
            await _accept(
                "uid-expires-during-session",
                "session-expired-heartbeat",
                active_revision,
            )
        ).allowed

        clock["now"] = started_at + timedelta(seconds=11)
        heartbeat = await voice_canary.update_session(
            uid="uid-expires-during-session",
            session_id="session-expired-heartbeat",
            input_audio_s=1,
            output_audio_s=0.5,
            input_audio_bytes=48_000,
            output_audio_bytes=24_000,
            tool_calls=0,
            reconnects=0,
            provider_request_ids=[],
            estimated_cost_microusd=2_000,
        )
        assert not heartbeat.allowed
        assert heartbeat.code == "expired"

        async with pool.acquire() as conn:
            persisted = dict(await conn.fetchrow("""
                    SELECT input_audio_s, output_audio_s, estimated_cost_microusd
                    FROM voice_active_sessions
                    WHERE session_id = 'session-expired-heartbeat'
                    """))
            entitlement = dict(await conn.fetchrow("""
                    SELECT provider_allowlist, mode_allowlist, fallback_policy
                    FROM voice_entitlements
                    WHERE uid = 'uid-expires-during-session'
                    """))
        assert float(persisted["input_audio_s"]) == 1
        assert float(persisted["output_audio_s"]) == 0.5
        assert persisted["estimated_cost_microusd"] == 2_000
        assert entitlement["provider_allowlist"] == ["grok-voice"]
        assert entitlement["mode_allowlist"] == ["v4"]
        fallback_policy = entitlement["fallback_policy"]
        if isinstance(fallback_policy, str):
            fallback_policy = json.loads(fallback_policy)
        assert fallback_policy == {"enabled": False, "order": []}

    asyncio.run(_run_with_database(scenario))


def test_cost_reservation_is_atomic_at_hard_boundary_and_settles(monkeypatch):
    started_at = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: started_at)

    async def scenario(pool: asyncpg.Pool) -> None:
        revision = await _grant(pool, "uid-cost-reservation")
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE voice_entitlements
                SET daily_cost_limit_microusd = 1000,
                    monthly_cost_limit_microusd = 1000
                WHERE uid = 'uid-cost-reservation'
                """)
        assert (await _accept("uid-cost-reservation", "session-cost", revision)).allowed

        denied = await voice_canary.reserve_session_cost(
            uid="uid-cost-reservation",
            session_id="session-cost",
            reservation_microusd=1000,
        )
        assert denied.allowed is False
        assert denied.code == "cost_daily"

        allowed = await voice_canary.reserve_session_cost(
            uid="uid-cost-reservation",
            session_id="session-cost",
            reservation_microusd=999,
        )
        assert allowed.allowed is True
        await voice_canary.settle_session_cost(
            uid="uid-cost-reservation",
            session_id="session-cost",
            actual_cost_microusd=300,
            tool_calls=1,
        )
        async with pool.acquire() as conn:
            settled = dict(await conn.fetchrow("""
                    SELECT estimated_cost_microusd, tool_calls
                    FROM voice_active_sessions
                    WHERE session_id = 'session-cost'
                    """))
        assert settled == {"estimated_cost_microusd": 300, "tool_calls": 1}

        await voice_canary.release_session_cost(
            uid="uid-cost-reservation",
            session_id="session-cost",
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval("""
                    SELECT estimated_cost_microusd
                    FROM voice_active_sessions
                    WHERE session_id = 'session-cost'
                    """) == 0

    asyncio.run(_run_with_database(scenario))


def test_cost_reservation_rejects_revocation_between_accept_and_reserve(monkeypatch):
    started_at = datetime(2026, 7, 26, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: started_at)

    async def scenario(pool: asyncpg.Pool) -> None:
        revision = await _grant(pool, "uid-revoked-before-reserve")
        assert (
            await _accept(
                "uid-revoked-before-reserve",
                "session-revoked-before-reserve",
                revision,
            )
        ).allowed
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE voice_entitlements
                SET status = 'revoked'
                WHERE uid = 'uid-revoked-before-reserve'
                """)

        denied = await voice_canary.reserve_session_cost(
            uid="uid-revoked-before-reserve",
            session_id="session-revoked-before-reserve",
            reservation_microusd=500,
        )

        assert denied.allowed is False
        assert denied.code == "revoked"
        async with pool.acquire() as conn:
            assert await conn.fetchval("""
                    SELECT estimated_cost_microusd
                    FROM voice_active_sessions
                    WHERE session_id = 'session-revoked-before-reserve'
                    """) == 0

    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize(
    ("uid", "update_sql", "expected_code"),
    [
        (
            "uid-stale-before-reserve",
            "SET revision = revision + 1",
            "entitlement_stale",
        ),
        (
            "uid-provider-before-reserve",
            "SET provider_allowlist = ARRAY[]::TEXT[]",
            "provider_not_allowed",
        ),
        (
            "uid-model-before-reserve",
            "SET model_allowlist = ARRAY['other-model']",
            "model_not_allowed",
        ),
        (
            "uid-mode-before-reserve",
            "SET mode_allowlist = ARRAY['other-mode']",
            "mode_not_allowed",
        ),
    ],
)
def test_cost_reservation_revalidates_changed_entitlement_authority(
    monkeypatch,
    uid,
    update_sql,
    expected_code,
):
    started_at = datetime(2026, 7, 26, 22, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: started_at)

    async def scenario(pool: asyncpg.Pool) -> None:
        revision = await _grant(pool, uid)
        session_id = f"session-{uid}"
        assert (await _accept(uid, session_id, revision)).allowed
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE voice_entitlements {update_sql} WHERE uid = $1",
                uid,
            )

        denied = await voice_canary.reserve_session_cost(
            uid=uid,
            session_id=session_id,
            reservation_microusd=500,
        )

        assert denied.allowed is False
        assert denied.code == expected_code

    asyncio.run(_run_with_database(scenario))


def test_cost_reservation_rechecks_expiry_after_accept(monkeypatch):
    started_at = datetime(2026, 7, 26, 23, 0, tzinfo=timezone.utc)
    clock = {"now": started_at}
    monkeypatch.setattr(voice_canary, "_utcnow", lambda: clock["now"])

    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "uid-expired-before-reserve"
        revision = await _grant(
            pool,
            uid,
            trial_expires_at=started_at + timedelta(seconds=5),
        )
        session_id = "session-expired-before-reserve"
        assert (await _accept(uid, session_id, revision)).allowed
        clock["now"] = started_at + timedelta(seconds=6)

        denied = await voice_canary.reserve_session_cost(
            uid=uid,
            session_id=session_id,
            reservation_microusd=500,
        )

        assert denied.allowed is False
        assert denied.code == "expired"

    asyncio.run(_run_with_database(scenario))
