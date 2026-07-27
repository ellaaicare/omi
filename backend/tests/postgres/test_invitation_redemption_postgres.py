import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import asyncpg
import pytest

from database import invitations, voice_canary

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = [
    Path(__file__).resolve().parents[2] / "migrations" / name
    for name in (
        "008_create_voice_canary_controls.sql",
        "011_create_invitation_redemption.sql",
    )
]
CONFIG = invitations.InvitationConfig(
    hmac_pepper=b"postgres-invite-tests-only",
    redemption_enabled=True,
    ordinary_enabled=True,
    app_review_enabled=True,
    progressive_backoff_enabled=False,
)
POLICY = {
    "plan": "canary",
    "daily_limit_s": 2700,
    "monthly_limit_s": 43200,
    "max_session_s": 1200,
    "max_concurrent": 1,
    "max_audio_bytes_per_session": 120_000_000,
    "max_audio_bytes_per_minute": 6_000_000,
    "soft_limit_ratio": 0.8,
    "hard_limit_ratio": 1.0,
    "provider_allowlist": ["grok-voice"],
    "model_allowlist": [],
    "mode_allowlist": ["v4"],
    "fallback_policy": {"enabled": False, "order": []},
}

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for invitation PostgreSQL tests",
)


async def _run_with_database(
    scenario: Callable[[asyncpg.Pool], Awaitable[None]],
) -> None:
    pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)
    previous_pool = voice_canary._pool
    voice_canary._pool = pool
    try:
        async with pool.acquire() as conn:
            for migration in MIGRATIONS:
                await conn.execute(migration.read_text(encoding="utf-8"))
            await conn.execute("""
                TRUNCATE TABLE
                    ella_invitation_security_alerts,
                    ella_invitation_rate_limit_events,
                    ella_invitation_audit_receipts,
                    ella_invitation_redemptions,
                    voice_active_sessions,
                    voice_usage_events,
                    voice_rate_limit_events,
                    voice_kill_switches,
                    voice_entitlements,
                    ella_invitations,
                    ella_invitation_capacity_reservations
                RESTART IDENTITY CASCADE
                """)
        await scenario(pool)
    finally:
        voice_canary._pool = previous_pool
        await pool.close()


async def _seed_invitation(
    pool: asyncpg.Pool,
    *,
    code: str,
    expires_at: datetime | None = None,
    capacity_state: str = "reserved",
    ordinary_enabled: bool = True,
) -> tuple[str, str]:
    normalized = invitations.normalize_invite_code(code)
    async with pool.acquire() as conn:
        async with conn.transaction():
            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ('synthetic', $1, 1, $2)
                RETURNING id
                """,
                capacity_state,
                expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
            )
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, cohort, first_sent_at, expires_at
                ) VALUES (
                    $1, 'ordinary', $2, $3, 'sent', 'sent', 'single_use', 1,
                    1, 'synthetic-policy-v1', $4::jsonb, 'synthetic', NOW(), $5
                )
                RETURNING id
                """,
                reservation_id,
                invitations.code_hmac(CONFIG, normalized),
                normalized[-2:],
                json.dumps(POLICY),
                expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
            )
    assert ordinary_enabled
    return str(invitation_id), str(reservation_id)


async def _seed_app_review_invitation(
    pool: asyncpg.Pool,
    *,
    code: str,
) -> str:
    normalized = invitations.normalize_invite_code(code)
    async with pool.acquire() as conn:
        async with conn.transaction():
            reservation_id = await conn.fetchval("""
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots
                ) VALUES ('app_review', 'reserved', 2)
                RETURNING id
                """)
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, cohort, exclude_from_product_analytics,
                    first_sent_at, expires_at
                ) VALUES (
                    $1, 'app_review', $2, $3, 'sent', 'sent',
                    'capped_multi_redeem', 20, 2, 'app-review-policy-v1',
                    $4::jsonb, 'app_review', TRUE, NOW(), NULL
                )
                RETURNING id
                """,
                reservation_id,
                invitations.code_hmac(CONFIG, normalized),
                normalized[-2:],
                json.dumps(POLICY),
            )
    return str(invitation_id)


async def _redeem(
    uid: str,
    code: str,
    *,
    source: str = "192.0.2.10",
    app_build: str = "synthetic-test",
    config=CONFIG,
):
    return await invitations.redeem_invitation(
        uid=uid,
        code=code,
        source_address=source,
        app_build=app_build,
        config=config,
    )


def test_same_uid_retry_is_idempotent_and_returns_same_entitlement():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id, _ = await _seed_invitation(pool, code="ABCD-2345")
        first = await _redeem(
            "uid-retry",
            "ABCD-2345",
            app_build="personal@example.com",
        )
        second = await _redeem("uid-retry", "abcd 2345")

        assert first["status"] == second["status"] == "invited"
        assert first["revision"] == second["revision"] == 1
        assert (await voice_canary.get_entitlement_contract("uid-other"))["status"] == "none"
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM voice_entitlements WHERE uid = 'uid-retry'") == 1
            assert (
                await conn.fetchval(
                    "SELECT redemption_count FROM ella_invitations WHERE id = $1::uuid",
                    invitation_id,
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    """
                SELECT COUNT(*) FROM ella_invitation_redemptions
                WHERE invitation_id = $1::uuid
                """,
                    invitation_id,
                )
                == 1
            )
            stored = await conn.fetchrow(
                """
                SELECT code_hmac, display_hint
                FROM ella_invitations
                WHERE id = $1::uuid
                """,
                invitation_id,
            )
            receipts = await conn.fetch(
                """
                SELECT uid_ref_hmac, source_ref_hmac, metadata::text
                FROM ella_invitation_audit_receipts
                WHERE invitation_id = $1::uuid
                """,
                invitation_id,
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT app_build
                    FROM ella_invitation_redemptions
                    WHERE invitation_id = $1::uuid
                    """,
                    invitation_id,
                )
                is None
            )
        serialized = json.dumps(
            {"invitation": dict(stored), "receipts": [dict(row) for row in receipts]},
            default=str,
        )
        assert "ABCD-2345" not in serialized
        assert "ABCD2345" not in serialized
        assert "192.0.2.10" not in serialized

    asyncio.run(_run_with_database(scenario))


def test_two_uid_concurrency_yields_exactly_one_grant():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id, _ = await _seed_invitation(pool, code="EFGH-6789")
        results = await asyncio.gather(
            _redeem("uid-race-a", "EFGH-6789", source="192.0.2.11"),
            _redeem("uid-race-b", "EFGH-6789", source="192.0.2.12"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, invitations.InviteRedemptionFailure)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "invalid"
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                SELECT COUNT(*) FROM voice_entitlements
                WHERE invitation_id = $1::uuid
                """,
                    invitation_id,
                )
                == 1
            )

    asyncio.run(_run_with_database(scenario))


def test_expired_capacity_and_invalid_failures_do_not_consume_codes():
    async def scenario(pool: asyncpg.Pool) -> None:
        expired_id, _ = await _seed_invitation(
            pool,
            code="JKMN-2345",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        capacity_id, reservation_id = await _seed_invitation(
            pool,
            code="MNPQ-6789",
            capacity_state="released",
        )
        cases = [
            ("uid-expired", "JKMN-2345", "expired"),
            ("uid-capacity", "MNPQ-6789", "capacity"),
            ("uid-invalid", "RSTU-2345", "invalid"),
        ]
        for uid, code, expected in cases:
            with pytest.raises(invitations.InviteRedemptionFailure) as error:
                await _redeem(uid, code)
            assert error.value.code == expected

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, state, redemption_count
                FROM ella_invitations
                WHERE id = ANY($1::uuid[])
                """,
                [expired_id, capacity_id],
            )
            assert {(row["state"], row["redemption_count"]) for row in rows} == {("sent", 0)}
            assert (
                await conn.fetchval(
                    """
                SELECT consumed_slots
                FROM ella_invitation_capacity_reservations
                WHERE id = $1::uuid
                """,
                    reservation_id,
                )
                == 0
            )

    asyncio.run(_run_with_database(scenario))


def test_rate_limit_uses_hmac_refs_and_emits_authoritative_retry():
    async def scenario(pool: asyncpg.Pool) -> None:
        for index in range(5):
            with pytest.raises(invitations.InviteRedemptionFailure) as error:
                await _redeem("uid-rate", f"ABCD-23{index}Z", source="198.51.100.7")
            assert error.value.code == "invalid"
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem("uid-rate", "ABCD-239Z", source="198.51.100.7")
        assert error.value.code == "rate_limited"
        assert error.value.retry_after_s and error.value.retry_after_s <= 900

        async with pool.acquire() as conn:
            stored = [dict(row) for row in await conn.fetch("""
                    SELECT uid_ref_hmac, source_ref_hmac
                    FROM ella_invitation_rate_limit_events
                    """)]
        assert len(stored) == 5
        assert all(len(row["uid_ref_hmac"]) == 64 for row in stored)
        assert all(len(row["source_ref_hmac"]) == 64 for row in stored)
        serialized = json.dumps(stored)
        assert "uid-rate" not in serialized
        assert "198.51.100.7" not in serialized

    asyncio.run(_run_with_database(scenario))


def test_ordinary_feature_gate_cannot_create_entitlement_or_consume_code():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id, _ = await _seed_invitation(pool, code="VWXY-6789")
        disabled = invitations.InvitationConfig(
            hmac_pepper=CONFIG.hmac_pepper,
            redemption_enabled=True,
            ordinary_enabled=False,
            app_review_enabled=False,
        )
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem("uid-disabled", "VWXY-6789", config=disabled)
        assert error.value.code == "invalid"
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM voice_entitlements WHERE uid = 'uid-disabled'") == 0
            row = await conn.fetchrow(
                """
                SELECT state, redemption_count
                FROM ella_invitations WHERE id = $1::uuid
                """,
                invitation_id,
            )
        assert row["state"] == "sent"
        assert row["redemption_count"] == 0

    asyncio.run(_run_with_database(scenario))


def test_app_review_code_is_capped_at_twenty_and_marks_analytics_exclusion():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id = await _seed_app_review_invitation(pool, code="Z234-5678")
        for index in range(20):
            result = await _redeem(
                f"reviewer-{index}",
                "Z234-5678",
                source=f"198.51.100.{index + 1}",
            )
            assert result["status"] == "invited"
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem("reviewer-over-cap", "Z234-5678", source="198.51.100.99")
        assert error.value.code == "invalid"

        async with pool.acquire() as conn:
            invitation = await conn.fetchrow(
                """
                SELECT state, redemption_count
                FROM ella_invitations WHERE id = $1::uuid
                """,
                invitation_id,
            )
            excluded = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM voice_entitlements
                WHERE invitation_id = $1::uuid
                  AND cohort = 'app_review'
                  AND exclude_from_product_analytics = TRUE
                """,
                invitation_id,
            )
        assert invitation["state"] == "sent"
        assert invitation["redemption_count"] == 20
        assert excluded == 20

    asyncio.run(_run_with_database(scenario))
