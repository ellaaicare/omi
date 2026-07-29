import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import asyncpg
import pytest

from database import invitations, voice_canary
from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)
from ella.services import ai_consent
from ella.services import invitation_authority
from ella.services.provisioning import ProvisioningCoordinator, VerifiedIdentity
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import resolve_isolated_runtime

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
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
    "provider_allowlist": [invitations.PILOT_RUNTIME_PROVIDER],
    "model_allowlist": [invitations.PILOT_MODEL],
    "mode_allowlist": list(invitations.PILOT_TARGET_MODES),
    "fallback_policy": {"enabled": False, "order": []},
}


def _prompt_receipt(*, model: str = invitations.PILOT_MODEL) -> dict:
    return {
        "schema_version": "ella-hermes-cloud-approval-v1",
        "prompt_pack_version": "ella-hermes-cloud-v1c-canary",
        "model_policy_version": "model-policy-v1",
        "expected_model": model,
        "model_context_window_tokens": 16384,
        "policy_commit_sha": "a" * 40,
        "lane_s_review_url": "https://github.com/ellaaicare/ella-ai/pull/1124",
        "approval_manifest_sha256": "b" * 64,
        "content_free": True,
        "soul_sha256": "c" * 64,
        "observed_soul_sha256": "c" * 64,
        "agents_sha256": "d" * 64,
        "observed_agents_sha256": "d" * 64,
        "model_policy_sha256": "e" * 64,
        "observed_model_policy_sha256": "e" * 64,
    }


pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for invitation PostgreSQL tests",
)

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT 'Synthetic User',
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE ella_provisioning_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_schema_version TEXT NOT NULL,
    client_request_id TEXT,
    request_payload_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    stage TEXT NOT NULL DEFAULT 'identity_ready',
    retryable BOOLEAN NOT NULL DEFAULT true,
    error_code TEXT,
    error_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    receipts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, target_schema_version)
);

CREATE TABLE ella_runtime_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    provider TEXT NOT NULL,
    profile_name TEXT UNIQUE,
    agent_id TEXT NOT NULL,
    workspace_root TEXT,
    internal_gateway_url TEXT,
    gateway_port INTEGER UNIQUE,
    service_label TEXT UNIQUE,
    credential_ref TEXT,
    honcho_workspace TEXT UNIQUE,
    observed_peer TEXT UNIQUE,
    observer_peer TEXT UNIQUE,
    template_version TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    voice_policy_version TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'pending',
    health_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_runtime_bindings_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ella_runtime_bindings_user_role_provider_key
    ON ella_runtime_bindings(user_id, role, provider);
"""


def _admission(uid: str) -> invitations.InvitationPilotAdmission:
    return invitations.InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id=f"synthetic-receipt-{uid}",
        policy_version=ai_consent.CURRENT_POLICY_VERSION,
        processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
        scope_version=ai_consent.CURRENT_SCOPE_VERSION,
        scope_hash=ai_consent.CURRENT_SCOPE_HASH,
    )


async def _run_with_database(
    scenario: Callable[[asyncpg.Pool], Awaitable[None]],
) -> None:
    schema = f"invite_redemption_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=5,
        server_settings={"search_path": schema},
    )
    previous_pool = voice_canary._pool
    voice_canary._pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute(BASE_PROVISIONING_SCHEMA)
            for name in (
                "008_create_voice_canary_controls.sql",
                "009_create_hermes_cloud_runtime_pool.sql",
                "010_add_cloud_profile_class.sql",
                "011_create_invitation_redemption.sql",
                "012_create_account_profile_runtime_targets.sql",
            ):
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            assert await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'users'
                      AND column_name = 'profile_class'
                      AND is_nullable = 'NO'
                )
                """
            )
            assert await conn.fetchval("SELECT to_regclass('ella_invitation_targets') IS NOT NULL")
        await scenario(pool)
    finally:
        voice_canary._pool = previous_pool
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _ensure_users(
    conn: asyncpg.Connection,
    uids: Iterable[str],
    *,
    profile_class: str = "synthetic",
) -> None:
    for uid in uids:
        await conn.execute(
            """
            INSERT INTO users (omi_uid, profile_class)
            VALUES ($1, $2)
            ON CONFLICT (omi_uid) DO UPDATE
            SET profile_class = EXCLUDED.profile_class
            """,
            uid,
            profile_class,
        )


async def _seed_invitation(
    pool: asyncpg.Pool,
    *,
    code: str,
    target_uids: Iterable[str],
    expires_at: datetime | None = None,
    capacity_state: str = "reserved",
    kind: str = "ordinary",
    profile_class: str = "synthetic",
) -> tuple[str, str]:
    targets = list(target_uids)
    assert targets
    normalized = invitations.normalize_invite_code(code)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_users(conn, targets, profile_class=profile_class)
            is_review = kind == "app_review"
            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                "app_review" if is_review else "synthetic",
                capacity_state,
                2 if is_review else 1,
                None if is_review else expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
            )
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, required_consent_policy_version,
                    required_consent_processor_set_hash,
                    required_consent_scope_version, required_consent_scope_hash,
                    cohort, exclude_from_product_analytics,
                    first_sent_at, expires_at
                ) VALUES (
                    $1, $2, $3, $4, 'sent', 'sent', $5, $6,
                    $7, $8, $9::jsonb, $10, $11, $12, $13,
                    $14, $15, NOW(), $16
                )
                RETURNING id
                """,
                reservation_id,
                kind,
                invitations.code_hmac(CONFIG, normalized),
                normalized[-2:],
                "capped_multi_redeem" if is_review else "single_use",
                20 if is_review else 1,
                2 if is_review else 1,
                "app-review-policy-v1" if is_review else "synthetic-policy-v1",
                json.dumps(POLICY),
                ai_consent.CURRENT_POLICY_VERSION,
                ai_consent.CURRENT_PROCESSOR_SET_HASH,
                ai_consent.CURRENT_SCOPE_VERSION,
                ai_consent.CURRENT_SCOPE_HASH,
                "app_review" if is_review else "synthetic",
                is_review,
                None if is_review else expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
            )
            for uid in targets:
                account_ref, profile_ref = invitations.invitation_target_refs(
                    CONFIG,
                    account_uid=uid,
                    profile_uid=uid,
                )
                await conn.execute(
                    """
                    INSERT INTO ella_invitation_targets (
                        invitation_id, account_ref_hmac, profile_ref_hmac,
                        required_profile_class
                    ) VALUES ($1, $2, $3, 'synthetic')
                    """,
                    invitation_id,
                    account_ref,
                    profile_ref,
                )
    return str(invitation_id), str(reservation_id)


async def _redeem(
    uid: str,
    code: str,
    *,
    source: str = "192.0.2.10",
    app_build: str = "synthetic-test",
    config: invitations.InvitationConfig = CONFIG,
    use_real_gate: bool = False,
):
    try:
        admission = invitation_authority.authorize_invitation_pilot(uid) if use_real_gate else _admission(uid)
    except invitations.InvitePilotGateDenied as exc:
        raise invitations.InviteRedemptionFailure(
            "invalid",
            status_code=400,
            support_code="INV-UNITTEST",
            correlation_id=str(uuid.uuid4()),
        ) from exc
    return await invitations.redeem_invitation(
        uid=uid,
        code=code,
        source_address=source,
        app_build=app_build,
        config=config,
        pilot_admission=admission,
    )


def _grant_v7(repository: ai_consent.InMemoryConsentRepository, uid: str) -> None:
    ai_consent.AiConsentService(repository).submit(
        uid,
        ai_consent.ConsentSubmission(
            decision="granted",
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            request_id=f"request-{uid}",
            app_version="synthetic",
            build_number="1",
            locale="en",
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )


def test_same_uid_retry_is_idempotent_and_privacy_safe():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-retry"
        invitation_id, _ = await _seed_invitation(
            pool,
            code="ABCD-2345",
            target_uids=[uid],
        )
        first = await _redeem(
            uid,
            "ABCD-2345",
            app_build="personal@example.com",
        )
        second = await _redeem(uid, "abcd 2345")

        assert first["status"] == second["status"] == "invited"
        assert first["revision"] == second["revision"] == 1
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM voice_entitlements WHERE uid = $1",
                    uid,
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
            stored = dict(
                await conn.fetchrow(
                    """
                    SELECT code_hmac, display_hint
                    FROM ella_invitations WHERE id = $1::uuid
                    """,
                    invitation_id,
                )
            )
            target = dict(
                await conn.fetchrow(
                    """
                    SELECT account_ref_hmac, profile_ref_hmac
                    FROM ella_invitation_targets
                    WHERE invitation_id = $1::uuid
                    """,
                    invitation_id,
                )
            )
            app_build = await conn.fetchval(
                """
                SELECT app_build FROM ella_invitation_redemptions
                WHERE invitation_id = $1::uuid
                """,
                invitation_id,
            )
            consent_receipt_ref = await conn.fetchval(
                """
                SELECT consent_receipt_ref_hmac
                FROM ella_invitation_redemptions
                WHERE invitation_id = $1::uuid
                """,
                invitation_id,
            )
        serialized = json.dumps(
            {"invitation": stored, "target": target},
            default=str,
        )
        assert "ABCD-2345" not in serialized
        assert "ABCD2345" not in serialized
        assert uid not in serialized
        assert app_build is None
        assert len(consent_receipt_ref) == 64
        assert "synthetic-receipt" not in consent_receipt_ref

    asyncio.run(_run_with_database(scenario))


def test_forwarded_or_cross_profile_code_cannot_mutate_capacity_or_entitlement():
    async def scenario(pool: asyncpg.Pool) -> None:
        owner = "synthetic-target-owner"
        forwarded = "synthetic-forwarded"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="EFGH-6789",
            target_uids=[owner],
        )
        await _ensure_users_for_test(pool, [forwarded])
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(forwarded, "EFGH-6789")
        assert error.value.code == "invalid"
        await _assert_unconsumed(
            pool,
            invitation_id=invitation_id,
            reservation_id=reservation_id,
            uid=forwarded,
        )

    asyncio.run(_run_with_database(scenario))


async def _ensure_users_for_test(
    pool: asyncpg.Pool,
    uids: Iterable[str],
    *,
    profile_class: str = "synthetic",
) -> None:
    async with pool.acquire() as conn:
        await _ensure_users(conn, uids, profile_class=profile_class)


async def _assert_unconsumed(
    pool: asyncpg.Pool,
    *,
    invitation_id: str,
    reservation_id: str,
    uid: str,
) -> None:
    async with pool.acquire() as conn:
        invitation = await conn.fetchrow(
            """
            SELECT state, redemption_count
            FROM ella_invitations WHERE id = $1::uuid
            """,
            invitation_id,
        )
        consumed_slots = await conn.fetchval(
            """
            SELECT consumed_slots
            FROM ella_invitation_capacity_reservations
            WHERE id = $1::uuid
            """,
            reservation_id,
        )
        entitlement_count = await conn.fetchval(
            "SELECT COUNT(*) FROM voice_entitlements WHERE uid = $1",
            uid,
        )
    assert dict(invitation) == {"state": "sent", "redemption_count": 0}
    assert consumed_slots == 0
    assert entitlement_count == 0


def test_two_authorized_targets_still_yield_one_atomic_grant():
    async def scenario(pool: asyncpg.Pool) -> None:
        first_uid = "synthetic-race-a"
        second_uid = "synthetic-race-b"
        invitation_id, _ = await _seed_invitation(
            pool,
            code="JKMN-2345",
            target_uids=[first_uid, second_uid],
        )
        results = await asyncio.gather(
            _redeem(first_uid, "JKMN-2345", source="192.0.2.11"),
            _redeem(second_uid, "JKMN-2345", source="192.0.2.12"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, invitations.InviteRedemptionFailure)]
        assert len(successes) == len(failures) == 1
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
        expired_uid = "synthetic-expired"
        capacity_uid = "synthetic-capacity"
        invalid_uid = "synthetic-invalid"
        expired_id, _ = await _seed_invitation(
            pool,
            code="MNPQ-6789",
            target_uids=[expired_uid],
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        capacity_id, reservation_id = await _seed_invitation(
            pool,
            code="RSTU-2345",
            target_uids=[capacity_uid],
            capacity_state="released",
        )
        await _ensure_users_for_test(pool, [invalid_uid])
        cases = [
            (expired_uid, "MNPQ-6789", "expired"),
            (capacity_uid, "RSTU-2345", "capacity"),
            (invalid_uid, "VWXY-6789", "invalid"),
        ]
        for uid, code, expected in cases:
            with pytest.raises(invitations.InviteRedemptionFailure) as error:
                await _redeem(uid, code)
            assert error.value.code == expected

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, state, redemption_count
                FROM ella_invitations WHERE id = ANY($1::uuid[])
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
        uid = "synthetic-rate"
        await _ensure_users_for_test(pool, [uid])
        for index in range(5):
            with pytest.raises(invitations.InviteRedemptionFailure) as error:
                await _redeem(
                    uid,
                    f"ABCD-23{index}Z",
                    source="198.51.100.7",
                )
            assert error.value.code == "invalid"
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(uid, "ABCD-239Z", source="198.51.100.7")
        assert error.value.code == "rate_limited"
        assert error.value.retry_after_s and error.value.retry_after_s <= 900

        async with pool.acquire() as conn:
            stored = [
                dict(row)
                for row in await conn.fetch(
                    """
                    SELECT uid_ref_hmac, source_ref_hmac
                    FROM ella_invitation_rate_limit_events
                    """
                )
            ]
        assert len(stored) == 5
        serialized = json.dumps(stored)
        assert uid not in serialized
        assert "198.51.100.7" not in serialized

    asyncio.run(_run_with_database(scenario))


def test_ordinary_feature_gate_cannot_create_entitlement_or_consume_code():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-disabled"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="Z234-5678",
            target_uids=[uid],
        )
        disabled = invitations.InvitationConfig(
            hmac_pepper=CONFIG.hmac_pepper,
            redemption_enabled=True,
            ordinary_enabled=False,
            app_review_enabled=False,
        )
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(uid, "Z234-5678", config=disabled)
        assert error.value.code == "invalid"
        await _assert_unconsumed(
            pool,
            invitation_id=invitation_id,
            reservation_id=reservation_id,
            uid=uid,
        )

    asyncio.run(_run_with_database(scenario))


def test_app_review_code_remains_capped_and_requires_prebound_targets():
    async def scenario(pool: asyncpg.Pool) -> None:
        targets = [f"synthetic-reviewer-{index}" for index in range(20)]
        invitation_id, _ = await _seed_invitation(
            pool,
            code="2345-6789",
            target_uids=targets,
            kind="app_review",
        )
        for index, uid in enumerate(targets):
            result = await _redeem(
                uid,
                "2345-6789",
                source=f"198.51.100.{index + 1}",
            )
            assert result["status"] == "invited"
        await _ensure_users_for_test(pool, ["synthetic-reviewer-forwarded"])
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(
                "synthetic-reviewer-forwarded",
                "2345-6789",
                source="198.51.100.99",
            )
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
                SELECT COUNT(*) FROM voice_entitlements
                WHERE invitation_id = $1::uuid
                  AND cohort = 'app_review'
                  AND exclude_from_product_analytics = TRUE
                """,
                invitation_id,
            )
        assert dict(invitation) == {"state": "sent", "redemption_count": 20}
        assert excluded == 20

    asyncio.run(_run_with_database(scenario))


def test_default_gate_requires_v7_exact_allowlists_and_synthetic_profile(
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        allowed = "synthetic-consented"
        no_consent = "synthetic-no-consent"
        no_allowlist = "synthetic-no-allowlist"
        real_profile = "synthetic-real-profile"
        repository = ai_consent.InMemoryConsentRepository()
        monkeypatch.setattr(ai_consent, "_repository", repository)
        for uid in (allowed, no_allowlist, real_profile):
            _grant_v7(repository, uid)
        monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
        monkeypatch.setenv(
            "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
            ",".join((allowed, no_consent, real_profile)),
        )
        monkeypatch.setenv(
            "ELLA_HERMES_CLOUD_SYNTHETIC_UIDS",
            ",".join((allowed, no_consent, real_profile)),
        )

        allowed_id, _ = await _seed_invitation(
            pool,
            code="ABCD-6789",
            target_uids=[allowed],
        )
        result = await _redeem(allowed, "ABCD-6789", use_real_gate=True)
        assert result["status"] == "invited"

        cases = [
            (no_consent, "EFGH-2345", "synthetic"),
            (no_allowlist, "JKMN-6789", "synthetic"),
            (real_profile, "MNPQ-2345", "real"),
        ]
        for uid, code, profile_class in cases:
            invitation_id, reservation_id = await _seed_invitation(
                pool,
                code=code,
                target_uids=[uid],
                profile_class=profile_class,
            )
            with pytest.raises(invitations.InviteRedemptionFailure) as error:
                await _redeem(uid, code, use_real_gate=True)
            assert error.value.code == "invalid"
            await _assert_unconsumed(
                pool,
                invitation_id=invitation_id,
                reservation_id=reservation_id,
                uid=uid,
            )

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                    SELECT redemption_count FROM ella_invitations
                    WHERE id = $1::uuid
                    """,
                    allowed_id,
                )
                == 1
            )

    asyncio.run(_run_with_database(scenario))


async def _register_pool(
    repository: EllaProvisioningRepository,
    *,
    runtime_instance_id: str,
) -> None:
    await repository.register_cloud_pool_binding(
        runtime_instance_id=runtime_instance_id,
        profile_name=f"profile-{runtime_instance_id}",
        agent_id="hermes-cloud",
        api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        honcho_api_key_ref="env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
        template_version="hermes-cloud-user-v1",
        prompt_pack_version="ella-hermes-cloud-v1c-canary",
        prompt_artifact_receipt=_prompt_receipt(),
        model_policy_version="model-policy-v1",
        voice_policy_version="voice-policy-v1",
        expected_model=invitations.PILOT_MODEL,
        allowed_tools=[],
        required_capabilities=["responses_api"],
        health_receipt={"status": "ok", "content_free": True},
    )


def test_invite_to_atomic_cloud_claim_is_idempotent_and_has_zero_fallback(
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-integrated-claim"
        legacy_uid = "legacy-plato-preserved"
        consent_repository = ai_consent.InMemoryConsentRepository()
        monkeypatch.setattr(ai_consent, "_repository", consent_repository)
        _grant_v7(consent_repository, uid)
        monkeypatch.setenv(
            "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
            uid,
        )
        monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", uid)
        invitation_id, _ = await _seed_invitation(
            pool,
            code="RSTU-6789",
            target_uids=[uid],
        )
        await _ensure_users_for_test(pool, [legacy_uid], profile_class="real")
        async with pool.acquire() as conn:
            legacy_user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                legacy_uid,
            )
            await conn.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, role, provider, status, profile_name, agent_id,
                    workspace_root, internal_gateway_url, gateway_port,
                    service_label, credential_ref, honcho_workspace,
                    observed_peer, observer_peer, template_version,
                    model_policy_version, voice_policy_version,
                    health_state, health_receipt, revision, active
                ) VALUES (
                    $1, 'user', 'hermes', 'active', 'legacy-preserved',
                    'legacy-agent', '/profiles/legacy-preserved/workspace',
                    'http://127.0.0.1:9999', 9999, 'legacy-service',
                    'env:LEGACY_TEST_ONLY', 'legacy-workspace',
                    'legacy-observed', 'legacy-observer', 'legacy-template',
                    'legacy-model', 'legacy-voice', 'healthy',
                    '{"status":"ok"}'::jsonb, 1, true
                )
                """,
                legacy_user_id,
            )

        await _redeem(uid, "RSTU-6789")
        repository = EllaProvisioningRepository(pool)
        await _register_pool(repository, runtime_instance_id="integrated-a")

        with pytest.raises(ProvisioningError) as before_claim:
            await resolve_isolated_runtime(
                uid,
                repository,
                target_mode="hermes-cloud-chat",
            )
        assert before_claim.value.code == "hermes_cloud_not_provisioned"

        async def _schema_ready() -> None:
            return None

        async def _identity_ready(**_kwargs) -> dict:
            return {"omi_uid": uid, "status": "ACTIVE"}

        async def _omi_identity_ready(**_kwargs) -> bool:
            return False

        monkeypatch.setattr(repository, "assert_schema_ready", _schema_ready)
        monkeypatch.setattr(repository, "assert_cloud_schema_ready", _schema_ready)
        monkeypatch.setattr(repository, "ensure_user_identity", _identity_ready)
        monkeypatch.setattr(repository, "ensure_omi_user_document", _omi_identity_ready)
        coordinator = ProvisioningCoordinator(repository)
        job, binding, claimed = await coordinator.ensure_job(
            identity=VerifiedIdentity(
                uid=uid,
                email="synthetic-integrated-claim@example.invalid",
                name="Synthetic Integrated Claim",
                timezone="UTC",
            ),
            target_schema_version="hermes-cloud-user-v1",
            client_request_id="synthetic-integrated-claim-request",
            request_payload={"source": "postgres-integration-test"},
        )
        assert binding is None
        assert claimed is True
        assert job["state"] == "provisioning"
        job_id = str(job["id"])

        admission = await voice_canary.evaluate_runtime_activation(
            uid=uid,
            provider=invitations.PILOT_RUNTIME_PROVIDER,
            model=invitations.PILOT_MODEL,
        )
        assert admission.allowed
        first = await repository.claim_cloud_pool_binding(
            uid=uid,
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=int(admission.entitlement["revision"]),
            provider=invitations.PILOT_RUNTIME_PROVIDER,
            model=invitations.PILOT_MODEL,
            required_profile_class="synthetic",
        )
        restarted_repository = EllaProvisioningRepository(pool)
        replay = await restarted_repository.claim_cloud_pool_binding(
            uid=uid,
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=int(admission.entitlement["revision"]),
            provider=invitations.PILOT_RUNTIME_PROVIDER,
            model=invitations.PILOT_MODEL,
            required_profile_class="synthetic",
        )
        assert first["id"] == replay["id"]
        assert first["claim_token"] == replay["claim_token"]
        with pytest.raises(ProvisioningError) as claiming:
            await resolve_isolated_runtime(
                uid,
                restarted_repository,
                target_mode="hermes-cloud-chat",
            )
        assert claiming.value.code == "hermes_cloud_claiming"

        async with pool.acquire() as conn:
            legacy = dict(
                await conn.fetchrow(
                    """
                    SELECT b.provider, b.status, b.active, b.profile_name
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                    """,
                    legacy_uid,
                )
            )
            entitlement = dict(
                await conn.fetchrow(
                    """
                    SELECT provider_allowlist, model_allowlist, mode_allowlist,
                           fallback_policy
                    FROM voice_entitlements WHERE uid = $1
                    """,
                    uid,
                )
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT redemption_count FROM ella_invitations
                    WHERE id = $1::uuid
                    """,
                    invitation_id,
                )
                == 1
            )
        assert legacy == {
            "provider": "hermes",
            "status": "active",
            "active": True,
            "profile_name": "legacy-preserved",
        }
        assert entitlement["provider_allowlist"] == [invitations.PILOT_RUNTIME_PROVIDER]
        assert entitlement["model_allowlist"] == [invitations.PILOT_MODEL]
        assert entitlement["mode_allowlist"] == list(invitations.PILOT_TARGET_MODES)
        fallback_policy = entitlement["fallback_policy"]
        if isinstance(fallback_policy, str):
            fallback_policy = json.loads(fallback_policy)
        assert fallback_policy == {
            "enabled": False,
            "order": [],
        }

    asyncio.run(_run_with_database(scenario))


def test_kill_switch_after_invite_blocks_claim_and_preserves_pool(monkeypatch):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-kill-switch"
        monkeypatch.setenv(
            "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
            uid,
        )
        monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", uid)
        await _seed_invitation(
            pool,
            code="VWXY-2345",
            target_uids=[uid],
        )
        await _redeem(uid, "VWXY-2345")
        repository = EllaProvisioningRepository(pool)
        await _register_pool(repository, runtime_instance_id="kill-switch-a")
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                uid,
            )
            job_id = await conn.fetchval(
                """
                INSERT INTO ella_provisioning_jobs (
                    user_id, target_schema_version
                ) VALUES ($1, 'hermes-cloud-user-v1')
                RETURNING id
                """,
                user_id,
            )
        admission = await voice_canary.evaluate_runtime_activation(
            uid=uid,
            provider=invitations.PILOT_RUNTIME_PROVIDER,
            model=invitations.PILOT_MODEL,
        )
        assert admission.allowed
        async with pool.acquire() as conn:
            async with conn.transaction():
                await voice_canary.set_kill_switch_on_connection(
                    conn,
                    scope_type="provider",
                    scope_value=invitations.PILOT_RUNTIME_PROVIDER,
                    enabled=True,
                    reason="synthetic test",
                    updated_by="test",
                )
        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.claim_cloud_pool_binding(
                uid=uid,
                job_id=str(job_id),
                lease_seconds=120,
                admitted_entitlement_revision=int(admission.entitlement["revision"]),
                provider=invitations.PILOT_RUNTIME_PROVIDER,
                model=invitations.PILOT_MODEL,
                required_profile_class="synthetic",
            )
        assert error.value.code == "runtime_admission_provider_disabled"
        async with pool.acquire() as conn:
            binding = dict(
                await conn.fetchrow(
                    """
                    SELECT status, user_id, claim_job_id, claim_token
                    FROM ella_runtime_bindings
                    WHERE runtime_instance_id = 'kill-switch-a'
                    """
                )
            )
        assert binding == {
            "status": "pool_available",
            "user_id": None,
            "claim_job_id": None,
            "claim_token": None,
        }

    asyncio.run(_run_with_database(scenario))
