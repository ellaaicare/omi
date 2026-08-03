import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import asyncpg
import pytest

from database import (
    account_deletion,
    authority_advisory_lock,
    invitation_operator,
    invitations,
    managed_cloud_consent,
    voice_canary,
)
from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)
from database.runtime_targets import RuntimeTargetLineage, SELF_HOSTED_RUNTIME_MODEL
from ella.services import ai_consent
from ella.services import account_deletion as account_deletion_service
from ella.services import consent_authority, invitation_authority
from ella.services.provisioning import HermesProvisionClient, ProvisioningCoordinator, VerifiedIdentity
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import (
    resolve_isolated_runtime,
    runtime_authority_enabled,
    runtime_authority_identity,
)
from scripts import pilot_invite_admin

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CONFIG = invitations.InvitationConfig(
    hmac_pepper=b"postgres-invite-tests-only",
    redemption_enabled=True,
    ordinary_enabled=True,
    app_review_enabled=True,
    progressive_backoff_enabled=False,
)
OPERATOR_CONFIG = invitations.InvitationConfig(
    hmac_pepper=CONFIG.hmac_pepper,
    redemption_enabled=True,
    ordinary_enabled=False,
    app_review_enabled=False,
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


@pytest.fixture(autouse=True)
def _successful_content_writer_drain(monkeypatch):
    async def tombstone(_uid):
        return True

    monkeypatch.setattr(
        account_deletion_service.content_write_fence,
        "tombstone_content_writes",
        tombstone,
    )


BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE,
    email TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT 'Synthetic User',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    identities JSONB NOT NULL DEFAULT '{}'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    conditions TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    medications TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE canonical_events (
    id BIGSERIAL PRIMARY KEY,
    uid TEXT NOT NULL,
    event_id TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata JSONB NOT NULL,
    raw_event JSONB NOT NULL
);

CREATE TABLE canonical_event_sessions (
    id BIGSERIAL PRIMARY KEY,
    uid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    metadata JSONB NOT NULL,
    raw_completion JSONB NOT NULL
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

CREATE TABLE agent_clusters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agents JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);
"""


def _admission(uid: str) -> invitations.InvitationPilotAdmission:
    return invitations.InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id=f"synthetic-receipt-{uid}",
        profile_binding_id=ai_consent.derive_profile_binding_id(
            account_uid=uid,
            profile_uid=uid,
        ),
        policy_version=ai_consent.CURRENT_POLICY_VERSION,
        processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
        scope_version=ai_consent.CURRENT_SCOPE_VERSION,
        scope_hash=ai_consent.CURRENT_SCOPE_HASH,
    )


def _self_hosted_admission(uid: str, email: str) -> invitations.InvitationPilotAdmission:
    return invitations.InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id="",
        profile_binding_id="",
        policy_version=ai_consent.CURRENT_POLICY_VERSION,
        processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
        scope_version=ai_consent.CURRENT_SCOPE_VERSION,
        scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        verified_email=email,
        required_profile_class="real",
        consent_pending=True,
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
                "007_create_memory_reinterpretation_outbox.sql",
                "008_create_voice_canary_controls.sql",
                "009_create_hermes_cloud_runtime_pool.sql",
                "010_add_cloud_profile_class.sql",
                "011_create_invitation_redemption.sql",
                "012_create_account_profile_runtime_targets.sql",
                "013_create_managed_cloud_consent_authority.sql",
                "014_add_synthetic_invitation_operator_audit.sql",
                "015_add_invitation_allowed_email_hash.sql",
                "017_add_provider_attempt_deletion_fence.sql",
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
    pilot_admission_revalidator: invitations.PilotAdmissionRevalidator | None = None,
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

    async def accept_test_admission(
        expected: invitations.InvitationPilotAdmission,
    ) -> invitations.InvitationPilotAdmission:
        return expected

    return await invitations.redeem_invitation(
        uid=uid,
        code=code,
        source_address=source,
        app_build=app_build,
        config=config,
        pilot_admission=admission,
        pilot_admission_revalidator=(
            pilot_admission_revalidator
            or (invitation_authority.revalidate_invitation_pilot if use_real_gate else accept_test_admission)
        ),
    )


async def _redeem_self_hosted(
    uid: str,
    email: str,
    code: str,
    *,
    source: str = "192.0.2.44",
):
    admission = _self_hosted_admission(uid, email)

    async def revalidate(
        expected: invitations.InvitationPilotAdmission,
    ) -> invitations.InvitationPilotAdmission:
        assert expected == admission
        return expected

    return await invitations.redeem_invitation(
        uid=uid,
        code=code,
        source_address=source,
        app_build="self-hosted-test",
        config=CONFIG,
        pilot_admission=admission,
        user_email=email,
        pilot_admission_revalidator=revalidate,
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


def _set_pilot_rollout(monkeypatch, uids: Iterable[str]) -> None:
    value = ",".join(uids)
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    for name in invitation_authority.PILOT_UID_ALLOWLISTS:
        monkeypatch.setenv(name, value)
    for name in invitation_authority.PILOT_GLOBAL_FLAGS_REQUIRED_FALSE:
        monkeypatch.setenv(name, "false")


def _self_hosted_lineage() -> RuntimeTargetLineage:
    return RuntimeTargetLineage(
        policy_version=ai_consent.CURRENT_POLICY_VERSION,
        processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
        scope_version=ai_consent.CURRENT_SCOPE_VERSION,
        scope_hash=ai_consent.CURRENT_SCOPE_HASH,
    )


def _self_hosted_grant(uid: str) -> managed_cloud_consent.ManagedCloudGrant:
    return managed_cloud_consent.ManagedCloudGrant(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id=f"receipt-{uid}",
        profile_binding_id=ai_consent.derive_profile_binding_id(
            account_uid=uid,
            profile_uid=uid,
        ),
        policy_version=ai_consent.CURRENT_POLICY_VERSION,
        processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
        scope_version=ai_consent.CURRENT_SCOPE_VERSION,
        scope_hash=ai_consent.CURRENT_SCOPE_HASH,
    )


def _local_runtime_binding(uid: str, *, port: int = 18701) -> dict:
    profile_name = f"ella-{uid}"[:63]
    return {
        "provider": "hermes",
        "agent_id": f"hermes-{uid}"[:63],
        "profile_name": profile_name,
        "workspace_root": f"/Users/ellaai/.hermes/profiles/{profile_name}/workspace",
        "internal_gateway_url": f"http://100.76.138.56:{port}",
        "gateway_port": port,
        "service_label": f"com.ella.hermes.{profile_name}"[:255],
        "credential_ref": "env:HERMES_API_SERVER_KEY",
        "honcho_workspace": f"honcho-{uid}"[:63],
        "observed_peer": f"observed-{uid}"[:63],
        "observer_peer": f"observer-{uid}"[:63],
        "template_version": "hermes-user-v1",
        "model_policy_version": "self-hosted-pilot-v1",
        "voice_policy_version": "ella-voice-v1",
        "health_state": "healthy",
        "health_receipt": {"content_free": True, "smoke_passed": True},
    }


def test_self_hosted_redemption_binds_verified_email_identity_and_target_atomically():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "firebase-self-hosted-one"
        email = "pilot.one@example.test"
        issued = await pilot_invite_admin._issue_invitation(
            code="WXYZ-2345",
            code_file_existed=False,
            code_file_ref_hmac="f" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, email),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )

        first = await _redeem_self_hosted(uid, email, "WXYZ-2345")
        second = await _redeem_self_hosted(uid, email, "wxyz 2345")
        assert first["status"] == second["status"] == "invited"
        assert first["revision"] == second["revision"] == 1

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    app_user.omi_uid, app_user.email, app_user.profile_class,
                    entitlement.invitation_consent_pending,
                    entitlement.consent_authority_epoch,
                    entitlement.revision AS entitlement_revision,
                    redemption.user_id AS redemption_user_id,
                    redemption.consent_pending AS redemption_consent_pending,
                    redemption.consent_receipt_ref_hmac,
                    target.provider, target.mode, target.status AS target_status,
                    target.runtime_binding_id,
                    target.entitlement_revision AS target_entitlement_revision,
                    reservation.consumed_slots
                FROM ella_invitations invitation
                JOIN ella_invitation_capacity_reservations reservation
                  ON reservation.id = invitation.capacity_reservation_id
                JOIN ella_invitation_redemptions redemption
                  ON redemption.invitation_id = invitation.id
                JOIN users app_user ON app_user.id = redemption.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = app_user.omi_uid
                 AND entitlement.invitation_id = invitation.id
                JOIN ella_runtime_targets target
                  ON target.invitation_target_id = redemption.invitation_target_id
                WHERE invitation.id = $1::uuid
                """,
                issued["receipt_id"],
            )
        assert len(rows) == 2
        row = rows[0]
        assert row["omi_uid"] == uid
        assert row["email"] == email
        assert row["profile_class"] == "real"
        assert row["invitation_consent_pending"] is True
        assert row["consent_authority_epoch"] is None
        assert row["redemption_user_id"] is not None
        assert row["redemption_consent_pending"] is True
        assert row["consent_receipt_ref_hmac"] is None
        assert {(item["provider"], item["mode"], item["target_status"]) for item in rows} == {
            ("hermes", "hermes-chat", "reserved"),
            ("hermes", "hermes-voice", "reserved"),
        }
        assert all(item["runtime_binding_id"] is None for item in rows)
        assert all(item["target_entitlement_revision"] == item["entitlement_revision"] == 1 for item in rows)
        assert row["consumed_slots"] == 1

        grant = managed_cloud_consent.ManagedCloudGrant(
            account_uid=uid,
            profile_uid=uid,
            consent_receipt_id="receipt-self-hosted-one",
            profile_binding_id=ai_consent.derive_profile_binding_id(
                account_uid=uid,
                profile_uid=uid,
            ),
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        )
        await managed_cloud_consent.synchronize_grant(grant=grant)
        repository = EllaProvisioningRepository(pool)
        admission = await repository.get_self_hosted_invitation_admission(uid)
        assert admission is not None
        assert admission["invitation_consent_pending"] is False
        assert admission["consent_authority_epoch"] == admission["current_authority_epoch"]
        assert admission["runtime_target_entitlement_revision"] == admission["revision"] == 2

        staged = await repository.stage_runtime_binding(
            uid=uid,
            binding={
                "provider": "hermes",
                "agent_id": "hermes-pilot-one",
                "profile_name": "ella-pilot-one",
                "template_version": "hermes-user-v1",
                "model_policy_version": "self-hosted-pilot-v1",
                "voice_policy_version": "ella-voice-v1",
                "health_state": "healthy",
                "health_receipt": {"content_free": True},
            },
        )
        activated = await repository.activate_runtime_binding(
            uid=uid,
            provider="hermes",
            require_invitation_target=True,
            authority_lineage=RuntimeTargetLineage(
                policy_version=ai_consent.CURRENT_POLICY_VERSION,
                processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
                scope_version=ai_consent.CURRENT_SCOPE_VERSION,
                scope_hash=ai_consent.CURRENT_SCOPE_HASH,
            ),
            model=SELF_HOSTED_RUNTIME_MODEL,
        )
        assert activated["id"] == staged["id"]
        async with pool.acquire() as conn:
            ready_targets = await conn.fetch(
                """
                SELECT mode, status, runtime_binding_id
                FROM ella_runtime_targets
                WHERE invitation_target_id IS NOT NULL
                  AND account_user_id = $1
                """,
                admission["user_id"],
            )
        assert {target["mode"] for target in ready_targets} == {"hermes-chat", "hermes-voice"}
        assert all(target["status"] == "ready" for target in ready_targets)
        assert all(target["runtime_binding_id"] == staged["id"] for target in ready_targets)

        after_consent_retry = await _redeem_self_hosted(uid, email, "WXYZ-2345")
        assert after_consent_retry["revision"] == 3
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM ella_invitation_redemptions WHERE invitation_id = $1::uuid",
                    issued["receipt_id"],
                )
                == 1
            )
            await conn.execute(
                """
                UPDATE ella_runtime_targets
                SET scope_hash = $2
                WHERE invitation_target_id IS NOT NULL
                  AND account_user_id = $1
                """,
                admission["user_id"],
                "sha256:" + "0" * 64,
            )
        assert await repository.get_self_hosted_invitation_admission(uid) is None

    asyncio.run(_run_with_database(scenario))


def test_self_hosted_email_mismatch_and_concurrent_open_redeem_fail_closed():
    async def scenario(pool: asyncpg.Pool) -> None:
        scoped = await pilot_invite_admin._issue_invitation(
            code="JKMN-2345",
            code_file_existed=False,
            code_file_ref_hmac="a" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, "approved@example.test"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        with pytest.raises(invitations.InviteRedemptionFailure) as mismatch:
            await _redeem_self_hosted(
                "firebase-wrong-email",
                "wrong@example.test",
                "JKMN-2345",
            )
        assert mismatch.value.code == "invalid"
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE omi_uid = 'firebase-wrong-email'") == 0
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM ella_invitation_targets WHERE invitation_id = $1::uuid",
                    scoped["receipt_id"],
                )
                == 0
            )

        opened = await pilot_invite_admin._issue_invitation(
            code="NPQR-2345",
            code_file_existed=False,
            code_file_ref_hmac="b" * 64,
            kind="ordinary",
            allowed_email_hash=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        results = await asyncio.gather(
            _redeem_self_hosted("firebase-open-a", "a@example.test", "NPQR-2345"),
            _redeem_self_hosted("firebase-open-b", "b@example.test", "NPQR-2345"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert (
            sum(
                isinstance(result, invitations.InviteRedemptionFailure) and result.code == "invalid"
                for result in results
            )
            == 1
        )
        async with pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM ella_invitation_redemptions
                     WHERE invitation_id = $1::uuid) AS redemptions,
                    (SELECT COUNT(*) FROM ella_invitation_targets
                     WHERE invitation_id = $1::uuid) AS targets,
                    (SELECT COUNT(*) FROM users
                     WHERE omi_uid IN ('firebase-open-a', 'firebase-open-b')) AS users
                """,
                opened["receipt_id"],
            )
        assert tuple(counts.values()) == (1, 1, 1)

    asyncio.run(_run_with_database(scenario))


def test_pilot_operator_ordinary_and_reviewer_rows_obey_migration_constraints():
    async def scenario(pool: asyncpg.Pool) -> None:
        ordinary = await pilot_invite_admin._issue_invitation(
            code="STUV-2345",
            code_file_existed=False,
            code_file_ref_hmac="c" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, "pilot@example.test"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
            environment="postgres-test",
            config=CONFIG,
        )
        reviewer = await pilot_invite_admin._issue_invitation(
            code="BCDF-2345",
            code_file_existed=False,
            code_file_ref_hmac="d" * 64,
            kind="app_review",
            allowed_email_hash=None,
            expires_at=None,
            environment="postgres-test",
            config=CONFIG,
        )
        retried = await pilot_invite_admin._issue_invitation(
            code="BCDF-2345",
            code_file_existed=True,
            code_file_ref_hmac="d" * 64,
            kind="app_review",
            allowed_email_hash=None,
            expires_at=None,
            environment="postgres-test",
            config=CONFIG,
        )
        assert retried["receipt_id"] == reviewer["receipt_id"]
        assert retried["idempotent"] is True
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT i.kind, i.display_hint, i.usage_mode, i.max_redemptions,
                       i.reserved_setup_slots, i.cohort,
                       i.exclude_from_product_analytics, i.expires_at,
                       i.allowed_email_hash, r.reserved_slots
                FROM ella_invitations i
                JOIN ella_invitation_capacity_reservations r
                  ON r.id = i.capacity_reservation_id
                WHERE i.id = ANY($1::uuid[])
                ORDER BY i.kind
                """,
                [uuid.UUID(ordinary["receipt_id"]), uuid.UUID(reviewer["receipt_id"])],
            )
        review_row, ordinary_row = rows
        assert (
            review_row["usage_mode"],
            review_row["max_redemptions"],
            review_row["reserved_setup_slots"],
            review_row["reserved_slots"],
            review_row["cohort"],
            review_row["exclude_from_product_analytics"],
            review_row["expires_at"],
        ) == ("capped_multi_redeem", 20, 2, 2, "app_review", True, None)
        assert (
            ordinary_row["usage_mode"],
            ordinary_row["max_redemptions"],
            ordinary_row["reserved_setup_slots"],
            ordinary_row["reserved_slots"],
            ordinary_row["cohort"],
            ordinary_row["exclude_from_product_analytics"],
        ) == (
            "single_use",
            1,
            1,
            1,
            invitations.SELF_HOSTED_OPERATOR_COHORT,
            False,
        )
        assert all(len(row["display_hint"]) == 2 for row in rows)
        assert ordinary_row["allowed_email_hash"] is not None
        assert review_row["allowed_email_hash"] is None

    asyncio.run(_run_with_database(scenario))


def test_pilot_operator_rotation_is_atomic_idempotent_and_preserves_email_scope():
    async def scenario(pool: asyncpg.Pool) -> None:
        old = await pilot_invite_admin._issue_invitation(
            code="QRST-2345",
            code_file_existed=False,
            code_file_ref_hmac="8" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, "rotate@example.test"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        rotated = await pilot_invite_admin._rotate_invitation(
            receipt_id=old["receipt_id"],
            expected_version=1,
            code="VWXY-2345",
            code_file_existed=False,
            code_file_ref_hmac="9" * 64,
            environment="postgres-test",
            config=CONFIG,
        )
        retried = await pilot_invite_admin._rotate_invitation(
            receipt_id=old["receipt_id"],
            expected_version=1,
            code="VWXY-2345",
            code_file_existed=True,
            code_file_ref_hmac="9" * 64,
            environment="postgres-test",
            config=CONFIG,
        )
        assert retried["receipt_id"] == rotated["receipt_id"]
        assert retried["idempotent"] is True

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT invitation.id, invitation.state, invitation.delivery_state,
                       invitation.allowed_email_hash, reservation.state AS reservation_state,
                       invitation.version
                FROM ella_invitations invitation
                JOIN ella_invitation_capacity_reservations reservation
                  ON reservation.id = invitation.capacity_reservation_id
                WHERE invitation.id = ANY($1::uuid[])
                ORDER BY invitation.id = $2::uuid DESC
                """,
                [uuid.UUID(old["receipt_id"]), uuid.UUID(rotated["receipt_id"])],
                old["receipt_id"],
            )
        previous, current = rows
        assert (previous["state"], previous["delivery_state"], previous["reservation_state"]) == (
            "revoked",
            "suppressed",
            "released",
        )
        assert previous["version"] == 2
        assert (current["state"], current["delivery_state"], current["reservation_state"]) == (
            "sent",
            "sent",
            "reserved",
        )
        assert current["allowed_email_hash"] == previous["allowed_email_hash"]

        with pytest.raises(invitations.InviteRedemptionFailure) as old_code:
            await _redeem_self_hosted(
                "rotate-user",
                "rotate@example.test",
                "QRST-2345",
            )
        assert old_code.value.code == "invalid"
        redeemed = await _redeem_self_hosted(
            "rotate-user",
            "rotate@example.test",
            "VWXY-2345",
        )
        assert redeemed["status"] == "invited"

    asyncio.run(_run_with_database(scenario))


def test_pilot_reviewer_quota_expiry_and_revoke_are_fail_closed():
    async def scenario(pool: asyncpg.Pool) -> None:
        reviewer = await pilot_invite_admin._issue_invitation(
            code="GHJK-2345",
            code_file_existed=False,
            code_file_ref_hmac="e" * 64,
            kind="app_review",
            allowed_email_hash=None,
            expires_at=None,
            environment="postgres-test",
            config=CONFIG,
        )
        for index in range(20):
            await _redeem_self_hosted(
                f"reviewer-{index}",
                f"reviewer-{index}@example.test",
                "GHJK-2345",
            )
        with pytest.raises(invitations.InviteRedemptionFailure) as exhausted:
            await _redeem_self_hosted(
                "reviewer-over-cap",
                "reviewer-over-cap@example.test",
                "GHJK-2345",
            )
        assert exhausted.value.code == "invalid"
        async with pool.acquire() as conn:
            reviewer_state = await conn.fetchrow(
                """
                SELECT i.redemption_count, i.state, r.state AS capacity_state,
                       r.consumed_slots,
                       (SELECT COUNT(*) FROM ella_invitation_targets
                        WHERE invitation_id = i.id) AS target_count
                FROM ella_invitations i
                JOIN ella_invitation_capacity_reservations r
                  ON r.id = i.capacity_reservation_id
                WHERE i.id = $1::uuid
                """,
                reviewer["receipt_id"],
            )
        assert tuple(reviewer_state.values()) == (20, "sent", "reserved", 0, 20)

        expiring = await pilot_invite_admin._issue_invitation(
            code="KLMN-2345".replace("L", "M"),
            code_file_existed=False,
            code_file_ref_hmac="6" * 64,
            kind="ordinary",
            allowed_email_hash=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            environment="postgres-test",
            config=CONFIG,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ella_invitations SET expires_at = NOW() - INTERVAL '1 second' WHERE id = $1::uuid",
                expiring["receipt_id"],
            )
        with pytest.raises(invitations.InviteRedemptionFailure) as expired:
            await _redeem_self_hosted(
                "expired-user",
                "expired@example.test",
                "KMMN-2345",
            )
        assert expired.value.code == "expired"

        revokable = await pilot_invite_admin._issue_invitation(
            code="MNPQ-2345",
            code_file_existed=False,
            code_file_ref_hmac="7" * 64,
            kind="ordinary",
            allowed_email_hash=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            environment="postgres-test",
            config=CONFIG,
        )
        revoked = await pilot_invite_admin._revoke_invitation(
            receipt_id=revokable["receipt_id"],
            expected_version=1,
            environment="postgres-test",
            config=CONFIG,
        )
        assert revoked["state"] == "revoked"
        with pytest.raises(invitations.InviteRedemptionFailure) as refused:
            await _redeem_self_hosted(
                "revoked-user",
                "revoked@example.test",
                "MNPQ-2345",
            )
        assert refused.value.code == "invalid"
        async with pool.acquire() as conn:
            states = await conn.fetchrow(
                """
                SELECT i.state, r.state AS reservation_state,
                       (SELECT COUNT(*) FROM users
                        WHERE omi_uid IN ('expired-user', 'revoked-user')) AS user_count,
                       (SELECT COUNT(*) FROM ella_invitation_audit_receipts
                        WHERE invitation_id = i.id
                          AND event_type = 'pilot_operator_revoked') AS revoke_audit_count
                FROM ella_invitations i
                JOIN ella_invitation_capacity_reservations r
                  ON r.id = i.capacity_reservation_id
                WHERE i.id = $1::uuid
                """,
                revokable["receipt_id"],
            )
        assert tuple(states.values()) == ("revoked", "released", 0, 1)

    asyncio.run(_run_with_database(scenario))


def test_pilot_capacity_concurrent_sixth_denial_has_zero_side_effects_and_review_is_separate(
    tmp_path,
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        root = tmp_path / "capacity-handoff"
        root.mkdir(mode=0o700)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        monkeypatch.setattr(pilot_invite_admin, "ROOT_UID", os.getuid())
        monkeypatch.setattr(pilot_invite_admin, "_configuration", lambda: CONFIG)
        receipts = []
        monkeypatch.setattr(
            pilot_invite_admin,
            "_print_receipt",
            lambda action, receipt, **kwargs: receipts.append((action, receipt, kwargs)),
        )

        async def issue(index: int):
            email_input = root / f"scope-{index}.input"
            email_input.write_text(f"pilot-{index}@example.test\n", encoding="utf-8")
            email_input.chmod(0o600)
            return await pilot_invite_admin._issue(
                argparse.Namespace(
                    kind="ordinary",
                    email_input_file=str(email_input),
                    expires_at=expires_at,
                    expected_environment="postgres-test",
                    approved_code_output_root=str(root),
                    code_output_file=str(root / f"pilot-{index}.code"),
                )
            )

        results = await asyncio.gather(*(issue(index) for index in range(6)), return_exceptions=True)
        failures = [result for result in results if isinstance(result, pilot_invite_admin.PilotInvitationError)]
        assert sum(result is None for result in results) == 5
        assert len(failures) == 1
        assert failures[0].code == "pilot_capacity_exhausted"
        failed_index = results.index(failures[0])
        assert not (root / f"pilot-{failed_index}.code").exists()
        assert len(list(root.glob("pilot-*.code"))) == 5
        assert len(list(root.glob(".synthetic-invite-recovery-*.json"))) == 5
        assert len(receipts) == 5

        async with pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM ella_invitations) AS invitations,
                    (SELECT COUNT(*) FROM ella_invitation_capacity_reservations) AS reservations,
                    (SELECT COUNT(*) FROM ella_invitation_audit_receipts
                     WHERE event_type = 'pilot_operator_issued') AS issue_audits,
                    (SELECT COALESCE(SUM(reserved_slots), 0)
                     FROM ella_invitation_capacity_reservations
                     WHERE pool_key = 'self_hosted_pilot'
                       AND state IN ('reserved', 'consumed')) AS pilot_slots
                """
            )
        assert tuple(counts.values()) == (5, 5, 5, 5)

        await pilot_invite_admin._issue(
            argparse.Namespace(
                kind="app_review",
                email_input_file=None,
                expires_at=None,
                expected_environment="postgres-test",
                approved_code_output_root=str(root),
                code_output_file=str(root / "review.code"),
            )
        )
        async with pool.acquire() as conn:
            pools = await conn.fetch(
                """
                SELECT pool_key, SUM(reserved_slots)::int AS slots
                FROM ella_invitation_capacity_reservations
                WHERE state IN ('reserved', 'consumed')
                GROUP BY pool_key
                ORDER BY pool_key
                """
            )
        assert [(row["pool_key"], row["slots"]) for row in pools] == [
            ("app_review", 2),
            ("self_hosted_pilot", 5),
        ]

    asyncio.run(_run_with_database(scenario))


def test_pilot_issue_absolute_expiry_recovers_ambiguous_outcome_without_duplicate_or_orphan(
    tmp_path,
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        root = tmp_path / "pilot-handoff"
        root.mkdir(mode=0o700)
        output = root / "stable-expiry.code"
        email_input = root / "scope.input"
        email_input.write_text("recovery@example.test\n", encoding="utf-8")
        email_input.chmod(0o600)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        args = argparse.Namespace(
            kind="ordinary",
            email_input_file=str(email_input),
            expires_at=expires_at,
            expected_environment="postgres-test",
            approved_code_output_root=str(root),
            code_output_file=str(output),
        )
        monkeypatch.setattr(pilot_invite_admin, "ROOT_UID", os.getuid())
        monkeypatch.setattr(pilot_invite_admin, "_configuration", lambda: CONFIG)

        issue_invitation = pilot_invite_admin._issue_invitation

        async def commit_then_lose_ack(**kwargs):
            await issue_invitation(**kwargs)
            raise ConnectionError("simulated_lost_commit_ack")

        monkeypatch.setattr(pilot_invite_admin, "_issue_invitation", commit_then_lose_ack)
        with pytest.raises(ConnectionError, match="simulated_lost_commit_ack"):
            await pilot_invite_admin._issue(args)
        assert output.exists()
        assert len(list(root.glob(".synthetic-invite-recovery-*.json"))) == 1

        receipts = []
        monkeypatch.setattr(pilot_invite_admin, "_issue_invitation", issue_invitation)
        monkeypatch.setattr(
            pilot_invite_admin,
            "_print_receipt",
            lambda action, receipt, **kwargs: receipts.append((action, receipt, kwargs)),
        )
        await pilot_invite_admin._issue(args)
        assert receipts[0][0] == "issue"
        assert receipts[0][1]["idempotent"] is True
        assert output.exists()

        async with pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM ella_invitations) AS invitations,
                    (SELECT COUNT(*) FROM ella_invitation_capacity_reservations) AS reservations,
                    (SELECT COUNT(*) FROM ella_invitation_audit_receipts
                     WHERE event_type = 'pilot_operator_issued') AS issue_audits,
                    (SELECT COUNT(*) FROM ella_invitation_audit_receipts
                     WHERE event_type = 'pilot_operator_idempotent_retry') AS retry_audits,
                    (SELECT COUNT(*)
                     FROM ella_invitation_capacity_reservations reservation
                     LEFT JOIN ella_invitations invitation
                       ON invitation.capacity_reservation_id = reservation.id
                     WHERE invitation.id IS NULL) AS orphan_reservations
                """
            )
        assert tuple(counts.values()) == (1, 1, 1, 1, 0)
        rendered = repr(receipts)
        assert output.read_text(encoding="ascii").strip() not in rendered
        assert "recovery@example.test" not in rendered

    asyncio.run(_run_with_database(scenario))


def test_self_hosted_runtime_resolution_is_invitation_authoritative_and_exact(monkeypatch):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "runtime-authority-user"
        email = "runtime-authority@example.test"
        await pilot_invite_admin._issue_invitation(
            code="HJKM-3456",
            code_file_existed=False,
            code_file_ref_hmac="b" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, email),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        await _redeem_self_hosted(uid, email, "HJKM-3456")
        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        repository = EllaProvisioningRepository(pool)
        staged = await repository.stage_runtime_binding(uid=uid, binding=_local_runtime_binding(uid))
        await repository.activate_runtime_binding(
            uid=uid,
            provider="hermes",
            require_invitation_target=True,
            authority_lineage=_self_hosted_lineage(),
            model=SELF_HOSTED_RUNTIME_MODEL,
        )
        assert await runtime_authority_enabled(uid, repository=repository) is True
        assert await runtime_authority_enabled("any-public-user", repository=repository) is False
        assert (
            await resolve_isolated_runtime(
                "any-public-user",
                repository=repository,
                target_mode="hermes-cloud-chat",
            )
            is None
        )

        monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "false")
        assert await runtime_authority_enabled(uid, repository=repository) is True
        with pytest.raises(ProvisioningError) as disabled:
            await resolve_isolated_runtime(
                uid,
                repository=repository,
                target_mode="hermes-cloud-chat",
            )
        assert disabled.value.code == "self_hosted_invitation_runtime_disabled"
        assert await runtime_authority_enabled("any-public-user", repository=repository) is False
        monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")

        runtime = await resolve_isolated_runtime(
            uid,
            repository=repository,
            target_mode="hermes-cloud-chat",
        )
        assert runtime is not None
        assert runtime.binding_id == str(staged["id"])
        assert runtime.provider == "hermes"
        assert runtime.runtime_target_mode == "hermes-chat"
        assert runtime.expected_model == SELF_HOSTED_RUNTIME_MODEL
        assert runtime.consent_authority_epoch

        voice_runtime = await resolve_isolated_runtime(
            uid,
            repository=repository,
            target_mode="hermes-voice",
        )
        assert voice_runtime is not None
        assert voice_runtime.binding_id == str(staged["id"])
        assert voice_runtime.provider == "hermes"
        assert voice_runtime.runtime_target_mode == "hermes-voice"
        assert voice_runtime.expected_model == SELF_HOSTED_RUNTIME_MODEL
        assert voice_runtime.account_user_id == voice_runtime.profile_user_id

        async def assert_voice_denied(update_sql: str, restore_sql: str) -> None:
            async with pool.acquire() as conn:
                await conn.execute(update_sql, uid)
            with pytest.raises(ProvisioningError) as drifted:
                await resolve_isolated_runtime(
                    uid,
                    repository=repository,
                    target_mode="hermes-voice",
                )
            assert drifted.value.code == "self_hosted_invitation_runtime_not_provisioned"
            async with pool.acquire() as conn:
                await conn.execute(restore_sql, uid)

        await assert_voice_denied(
            """
            UPDATE ella_runtime_targets target SET status = 'disabled'
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
            """
            UPDATE ella_runtime_targets target SET status = 'ready'
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
        )
        await assert_voice_denied(
            "UPDATE voice_entitlements SET status = 'suspended' WHERE uid = $1",
            "UPDATE voice_entitlements SET status = 'active' WHERE uid = $1",
        )
        await assert_voice_denied(
            "UPDATE voice_entitlements SET consent_authority_epoch = gen_random_uuid() WHERE uid = $1",
            """
            UPDATE voice_entitlements entitlement
            SET consent_authority_epoch = authority.authority_epoch
            FROM users app_user, ella_managed_cloud_consent_authority authority
            WHERE entitlement.uid = $1 AND app_user.omi_uid = entitlement.uid
              AND authority.user_id = app_user.id
            """,
        )
        await assert_voice_denied(
            """
            UPDATE ella_runtime_targets target SET scope_hash = 'sha256:' || repeat('f', 64)
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
            f"""
            UPDATE ella_runtime_targets target SET scope_hash = '{ai_consent.CURRENT_SCOPE_HASH}'
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
        )
        await assert_voice_denied(
            "UPDATE voice_entitlements SET provider_allowlist = ARRAY['hermes', 'retained']::text[] WHERE uid = $1",
            "UPDATE voice_entitlements SET provider_allowlist = ARRAY['hermes']::text[] WHERE uid = $1",
        )
        await assert_voice_denied(
            "UPDATE voice_entitlements SET model_allowlist = ARRAY['drifted-model']::text[] WHERE uid = $1",
            f"UPDATE voice_entitlements SET model_allowlist = ARRAY['{SELF_HOSTED_RUNTIME_MODEL}']::text[] WHERE uid = $1",
        )
        await assert_voice_denied(
            "UPDATE voice_entitlements SET mode_allowlist = ARRAY['hermes-chat']::text[] WHERE uid = $1",
            "UPDATE voice_entitlements SET mode_allowlist = ARRAY['hermes-chat', 'hermes-voice']::text[] WHERE uid = $1",
        )
        await assert_voice_denied(
            """
            UPDATE voice_entitlements
            SET fallback_policy = '{"enabled":true,"order":["retained"]}'::jsonb
            WHERE uid = $1
            """,
            """
            UPDATE voice_entitlements
            SET fallback_policy = '{"enabled":false,"order":[]}'::jsonb
            WHERE uid = $1
            """,
        )
        async with pool.acquire() as conn:
            await _ensure_users(conn, ["voice-drift-owner"], profile_class="real")
            owner_id = await conn.fetchval("SELECT id FROM users WHERE omi_uid = $1", uid)
            drift_owner_id = await conn.fetchval("SELECT id FROM users WHERE omi_uid = 'voice-drift-owner'")
            voice_target_id = await conn.fetchval(
                """
                SELECT target.id
                FROM ella_runtime_targets target
                JOIN users app_user ON app_user.id = target.account_user_id
                WHERE app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
                """,
                uid,
            )
        await assert_voice_denied(
            f"""
            UPDATE ella_runtime_targets target SET profile_user_id = '{drift_owner_id}'::uuid
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
            f"""
            UPDATE ella_runtime_targets target SET profile_user_id = '{owner_id}'::uuid
            FROM users app_user
            WHERE target.account_user_id = app_user.id
              AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
            """,
        )
        await assert_voice_denied(
            f"""
            UPDATE ella_runtime_targets
            SET account_user_id = '{drift_owner_id}'::uuid
            WHERE id = '{voice_target_id}'::uuid AND $1::text IS NOT NULL
            """,
            f"""
            UPDATE ella_runtime_targets
            SET account_user_id = '{owner_id}'::uuid
            WHERE id = '{voice_target_id}'::uuid AND $1::text IS NOT NULL
            """,
        )
        await assert_voice_denied(
            """
            UPDATE ella_runtime_bindings binding SET active = FALSE
            FROM users app_user
            WHERE binding.user_id = app_user.id AND app_user.omi_uid = $1
              AND binding.provider = 'hermes'
            """,
            """
            UPDATE ella_runtime_bindings binding SET active = TRUE
            FROM users app_user
            WHERE binding.user_id = app_user.id AND app_user.omi_uid = $1
              AND binding.provider = 'hermes'
            """,
        )

        issued_identity = runtime_authority_identity(voice_runtime)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_runtime_targets target SET updated_at = target.updated_at + INTERVAL '1 second'
                FROM users app_user
                WHERE target.account_user_id = app_user.id
                  AND app_user.omi_uid = $1 AND target.mode = 'hermes-voice'
                """,
                uid,
            )
        rerouted_voice = await resolve_isolated_runtime(uid, repository=repository, target_mode="hermes-voice")
        assert rerouted_voice is not None
        assert runtime_authority_identity(rerouted_voice).digest != issued_identity.digest

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE voice_entitlements
                SET provider_allowlist = ARRAY['hermes', 'retained']::text[]
                WHERE uid = $1
                """,
                uid,
            )
        with pytest.raises(ProvisioningError) as drifted:
            await resolve_isolated_runtime(
                uid,
                repository=repository,
                target_mode="hermes-cloud-chat",
            )
        assert drifted.value.code == "self_hosted_invitation_runtime_not_provisioned"

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "unit-test-gateway-secret")
    asyncio.run(_run_with_database(scenario))


def test_self_hosted_revoke_invalidates_authority_and_blocks_reactivation(monkeypatch):
    async def scenario(pool: asyncpg.Pool) -> None:
        users = (
            ("review-revoke-a", "review-revoke-a@example.test"),
            ("review-revoke-b", "review-revoke-b@example.test"),
        )
        issued = await pilot_invite_admin._issue_invitation(
            code="JKMN-3456",
            code_file_existed=False,
            code_file_ref_hmac="c" * 64,
            kind="app_review",
            allowed_email_hash=None,
            expires_at=None,
            environment="postgres-test",
            config=CONFIG,
        )
        for uid, email in users:
            await _redeem_self_hosted(uid, email, "JKMN-3456")
            await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))

        repository = EllaProvisioningRepository(pool)
        staged = await repository.stage_runtime_binding(
            uid=users[0][0],
            binding=_local_runtime_binding(users[0][0]),
        )
        activated = await repository.activate_runtime_binding(
            uid=users[0][0],
            provider="hermes",
            require_invitation_target=True,
            authority_lineage=_self_hosted_lineage(),
            model=SELF_HOSTED_RUNTIME_MODEL,
        )
        async with pool.acquire() as conn:
            entitlement_revision = await conn.fetchval(
                "SELECT revision FROM voice_entitlements WHERE uid = $1",
                users[0][0],
            )
            await conn.execute(
                """
                INSERT INTO voice_active_sessions (
                    session_id, uid, correlation_id, entitlement_revision,
                    provider, model, mode
                ) VALUES ('review-revoke-session', $1, 'correlation', $2,
                          'hermes', $3, 'hermes-chat')
                """,
                users[0][0],
                entitlement_revision,
                SELF_HOSTED_RUNTIME_MODEL,
            )
            await conn.execute(
                """
                INSERT INTO ella_runtime_session_scopes (
                    binding_id, user_id, role, channel, session_key
                ) VALUES ($1, $2, 'user', 'chat', 'review-revoke-runtime-session')
                """,
                activated["id"],
                activated["user_id"],
            )
            for uid, _email in users:
                await conn.execute(
                    """
                    INSERT INTO ella_provisioning_jobs (
                        user_id, target_schema_version, state, stage, retryable
                    ) SELECT id, 'hermes-user-v1', 'retryable', 'profile_ready', TRUE
                      FROM users WHERE omi_uid = $1
                    ON CONFLICT (user_id, target_schema_version) DO NOTHING
                    """,
                    uid,
                )
            version = await conn.fetchval(
                "SELECT version FROM ella_invitations WHERE id = $1::uuid",
                issued["receipt_id"],
            )

        revoked = await pilot_invite_admin._revoke_invitation(
            receipt_id=issued["receipt_id"],
            expected_version=version,
            environment="postgres-test",
            config=CONFIG,
        )
        assert revoked["invalidated_users"] == 2
        retried = await pilot_invite_admin._revoke_invitation(
            receipt_id=issued["receipt_id"],
            expected_version=version,
            environment="postgres-test",
            config=CONFIG,
        )
        assert retried["idempotent"] is True

        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT
                    (SELECT state FROM ella_invitations WHERE id = $1::uuid) AS invitation_state,
                        (SELECT reservation.state FROM ella_invitation_capacity_reservations reservation
                     JOIN ella_invitations invitation
                       ON invitation.capacity_reservation_id = reservation.id
                     WHERE invitation.id = $1::uuid) AS capacity_state,
                    (SELECT COUNT(*) FROM voice_entitlements
                     WHERE uid = ANY($2::text[]) AND status = 'revoked') AS revoked_entitlements,
                    (SELECT COUNT(*) FROM ella_runtime_targets target
                     JOIN ella_invitation_targets invitation_target
                       ON invitation_target.id = target.invitation_target_id
                     WHERE invitation_target.invitation_id = $1::uuid
                       AND target.status = 'revoked') AS revoked_targets,
                    (SELECT COUNT(*) FROM ella_invitation_targets
                     WHERE invitation_id = $1::uuid AND revoked_at IS NOT NULL) AS revoked_invitation_targets,
                    (SELECT COUNT(*) FROM voice_active_sessions
                     WHERE uid = ANY($2::text[])) AS voice_sessions,
                    (SELECT COUNT(*) FROM ella_runtime_session_scopes
                     WHERE binding_id = $3) AS runtime_sessions,
                    (SELECT COUNT(*) FROM ella_runtime_bindings
                     WHERE id = $3 AND active = FALSE AND status = 'disabled') AS disabled_bindings,
                    (SELECT COUNT(*) FROM ella_provisioning_jobs job
                     JOIN users app_user ON app_user.id = job.user_id
                     WHERE app_user.omi_uid = ANY($2::text[]) AND job.state = 'blocked') AS blocked_jobs
                """,
                issued["receipt_id"],
                [uid for uid, _email in users],
                staged["id"],
            )
        assert tuple(state.values()) == ("revoked", "released", 2, 4, 2, 0, 0, 1, 2)

        with pytest.raises(ProvisioningError) as revoked_voice:
            await resolve_isolated_runtime(
                users[0][0],
                repository=repository,
                target_mode="hermes-voice",
            )
        assert revoked_voice.value.code == "self_hosted_invitation_runtime_not_provisioned"

        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(users[0][0]))
        with pytest.raises((RuntimePoolClaimError, RuntimeError)):
            await repository.activate_runtime_binding(
                uid=users[0][0],
                provider="hermes",
                require_invitation_target=True,
                authority_lineage=_self_hosted_lineage(),
                model=SELF_HOSTED_RUNTIME_MODEL,
            )
        with pytest.raises(ProvisioningError):
            await resolve_isolated_runtime(
                users[0][0],
                repository=repository,
                target_mode="hermes-cloud-chat",
            )

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "unit-test-gateway-secret")
    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize(
    ("authority_change", "code"),
    (("declined", "MNPQ-3456"), ("grant_drift", "NPQR-3456")),
)
def test_self_hosted_consent_authority_change_invalidates_runtime(
    monkeypatch,
    authority_change,
    code,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = f"self-hosted-{authority_change}"
        email = f"{uid}@example.test"
        await pilot_invite_admin._issue_invitation(
            code=code,
            code_file_existed=False,
            code_file_ref_hmac="e" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, email),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        await _redeem_self_hosted(uid, email, code)
        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        repository = EllaProvisioningRepository(pool)
        activated = await repository.stage_runtime_binding(
            uid=uid,
            binding=_local_runtime_binding(uid),
        )
        await repository.activate_runtime_binding(
            uid=uid,
            provider="hermes",
            require_invitation_target=True,
            authority_lineage=_self_hosted_lineage(),
            model=SELF_HOSTED_RUNTIME_MODEL,
        )

        if authority_change == "declined":
            await managed_cloud_consent.synchronize_denial(uid=uid, decision="declined")
        else:
            changed = _self_hosted_grant(uid)
            await managed_cloud_consent.synchronize_grant(
                grant=managed_cloud_consent.ManagedCloudGrant(
                    account_uid=changed.account_uid,
                    profile_uid=changed.profile_uid,
                    consent_receipt_id=f"{changed.consent_receipt_id}-changed",
                    profile_binding_id=changed.profile_binding_id,
                    policy_version=changed.policy_version,
                    processor_set_hash=changed.processor_set_hash,
                    scope_version=changed.scope_version,
                    scope_hash=changed.scope_hash,
                )
            )

        assert await repository.get_self_hosted_invitation_admission(uid) is None
        with pytest.raises(ProvisioningError) as denied_voice:
            await resolve_isolated_runtime(
                uid,
                repository=repository,
                target_mode="hermes-voice",
            )
        assert denied_voice.value.code == "self_hosted_invitation_runtime_not_provisioned"
        with pytest.raises((RuntimePoolClaimError, RuntimeError)):
            await repository.activate_runtime_binding(
                uid=uid,
                provider="hermes",
                require_invitation_target=True,
                authority_lineage=_self_hosted_lineage(),
                model=SELF_HOSTED_RUNTIME_MODEL,
            )
        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT
                    (SELECT status FROM voice_entitlements WHERE uid = $1) AS entitlement_status,
                    (SELECT MIN(target.status) FROM ella_runtime_targets target
                     JOIN users app_user ON app_user.id = target.account_user_id
                     WHERE app_user.omi_uid = $1 AND target.provider = 'hermes') AS target_status,
                    (SELECT COUNT(*) FROM ella_invitation_targets invitation_target
                     JOIN ella_invitation_redemptions redemption
                       ON redemption.invitation_target_id = invitation_target.id
                     JOIN users app_user ON app_user.id = redemption.user_id
                     WHERE app_user.omi_uid = $1
                       AND invitation_target.revoked_at IS NOT NULL) AS revoked_targets,
                    (SELECT COUNT(*) FROM ella_runtime_bindings
                     WHERE id = $2 AND active = FALSE AND status = 'disabled') AS disabled_bindings
                """,
                uid,
                activated["id"],
            )
        assert tuple(state.values()) == ("revoked", "revoked", 1, 1)

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "unit-test-gateway-secret")
    asyncio.run(_run_with_database(scenario))


def test_self_hosted_post_provider_revoke_race_cannot_publish_or_retry(monkeypatch):
    class ControlledProvisionClient:
        def __init__(self, receipt):
            self.receipt = receipt
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def provision(self, identity, target_schema_version):
            del identity, target_schema_version
            self.started.set()
            await self.release.wait()
            return self.receipt

    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "post-provider-race"
        email = "post-provider-race@example.test"
        issued = await pilot_invite_admin._issue_invitation(
            code="KMNP-3456",
            code_file_existed=False,
            code_file_ref_hmac="d" * 64,
            kind="ordinary",
            allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, email),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            environment="postgres-test",
            config=CONFIG,
        )
        await _redeem_self_hosted(uid, email, "KMNP-3456")
        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        repository = EllaProvisioningRepository(pool)
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                """
                INSERT INTO ella_provisioning_jobs (
                    user_id, target_schema_version, state, stage, retryable
                ) SELECT id, 'hermes-user-v1', 'provisioning', 'profile_ready', TRUE
                  FROM users WHERE omi_uid = $1
                RETURNING *
                """,
                uid,
            )
            version = await conn.fetchval(
                "SELECT version FROM ella_invitations WHERE id = $1::uuid",
                issued["receipt_id"],
            )
        client = ControlledProvisionClient(
            {
                "mode": "hermes_only",
                "provisionMode": "hermes_only",
                "runtimeBinding": {
                    **_local_runtime_binding(uid),
                    "profileName": _local_runtime_binding(uid)["profile_name"],
                    "agentId": _local_runtime_binding(uid)["agent_id"],
                    "workspaceRoot": _local_runtime_binding(uid)["workspace_root"],
                    "internalGatewayUrl": _local_runtime_binding(uid)["internal_gateway_url"],
                    "gatewayPort": _local_runtime_binding(uid)["gateway_port"],
                    "serviceLabel": _local_runtime_binding(uid)["service_label"],
                    "credentialRef": _local_runtime_binding(uid)["credential_ref"],
                    "healthState": "healthy",
                    "smokePassed": True,
                    "healthReceipt": {"content_free": True, "smoke_passed": True},
                    "templateVersion": "hermes-user-v1",
                    "modelPolicyVersion": "self-hosted-pilot-v1",
                    "voicePolicyVersion": "ella-voice-v1",
                    "honcho": {
                        "workspace": _local_runtime_binding(uid)["honcho_workspace"],
                        "observedPeer": _local_runtime_binding(uid)["observed_peer"],
                        "observerPeer": _local_runtime_binding(uid)["observer_peer"],
                    },
                },
            }
        )
        coordinator = ProvisioningCoordinator(repository, client)
        task = asyncio.create_task(
            coordinator.process_claimed_job(
                job=dict(job),
                identity=VerifiedIdentity(uid, email, "Race User", "UTC"),
            )
        )
        provider_started = asyncio.create_task(client.started.wait())
        completed, _ = await asyncio.wait(
            {task, provider_started},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in completed:
            await task
        assert provider_started in completed, "provider boundary did not become reachable"
        await pilot_invite_admin._revoke_invitation(
            receipt_id=issued["receipt_id"],
            expected_version=version,
            environment="postgres-test",
            config=CONFIG,
        )
        client.release.set()
        await task

        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT
                    (SELECT status FROM voice_entitlements WHERE uid = $1) AS entitlement_status,
                    (SELECT MIN(target.status) FROM ella_runtime_targets target
                     JOIN users app_user ON app_user.id = target.account_user_id
                     WHERE app_user.omi_uid = $1 AND target.provider = 'hermes') AS target_status,
                    (SELECT COUNT(*) FROM ella_runtime_bindings binding
                     JOIN users app_user ON app_user.id = binding.user_id
                     WHERE app_user.omi_uid = $1 AND binding.provider = 'hermes'
                       AND binding.active = TRUE) AS active_bindings,
                    (SELECT state FROM ella_provisioning_jobs WHERE id = $2) AS job_state,
                    (SELECT retryable FROM ella_provisioning_jobs WHERE id = $2) AS job_retryable
                """,
                uid,
                job["id"],
            )
        assert tuple(state.values()) == ("revoked", "revoked", 0, "blocked", False)

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "unit-test-gateway-secret")
    asyncio.run(_run_with_database(scenario))


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


def test_broker_lock_blocks_invitation_before_capacity_or_entitlement_mutation():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-broker-blocks-invite"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="EFGH-2345",
            target_uids=[uid],
        )
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                uid,
            )
        owner = authority_advisory_lock.AuthorityOwner.from_values(
            user_id,
            user_id,
        )
        key = authority_advisory_lock.authority_lock_key(
            str(owner.account_id),
            str(owner.profile_id),
        )
        class_id, object_id = authority_advisory_lock._advisory_lock_parts(key)

        async def snapshot():
            async with pool.acquire() as observer:
                row = await observer.fetchrow(
                    """
                    SELECT i.state, i.redemption_count, r.consumed_slots,
                           (
                               SELECT COUNT(*)::integer
                               FROM voice_entitlements e
                               WHERE e.uid = $3
                           ) AS entitlement_count
                    FROM ella_invitations i
                    JOIN ella_invitation_capacity_reservations r
                      ON r.id = i.capacity_reservation_id
                    WHERE i.id = $1::uuid
                      AND r.id = $2::uuid
                    """,
                    invitation_id,
                    reservation_id,
                    uid,
                )
                return tuple(row.values())

        assert await snapshot() == ("sent", 0, 0, 0)
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner,
            )
            redemption = asyncio.create_task(_redeem(uid, "EFGH-2345"))
            for _attempt in range(100):
                async with pool.acquire() as probe:
                    waiting = await probe.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE locktype = 'advisory'
                              AND classid::bigint = $1
                              AND objid::bigint = $2
                              AND objsubid = 1
                              AND mode = 'ExclusiveLock'
                              AND NOT granted
                        )
                        """,
                        class_id,
                        object_id,
                    )
                if waiting:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("invitation writer never waited on the shared v1 lock")
            assert not redemption.done()
            assert await snapshot() == ("sent", 0, 0, 0)
            await transaction.commit()

        result = await asyncio.wait_for(redemption, timeout=5)
        assert result["status"] == "invited"
        assert await snapshot() == ("redeemed", 1, 1, 1)

    asyncio.run(_run_with_database(scenario))


def test_invitation_reproves_owner_and_rolls_back_on_concurrent_drift():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-invite-owner-drift"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="KMNP-2345",
            target_uids=[uid],
        )
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                uid,
            )
        owner = authority_advisory_lock.AuthorityOwner.from_values(
            user_id,
            user_id,
        )
        key = authority_advisory_lock.authority_lock_key(
            str(owner.account_id),
            str(owner.profile_id),
        )
        class_id, object_id = authority_advisory_lock._advisory_lock_parts(key)

        async def snapshot():
            async with pool.acquire() as observer:
                row = await observer.fetchrow(
                    """
                    SELECT i.state, i.redemption_count, r.consumed_slots,
                           (
                               SELECT COUNT(*)::integer
                               FROM voice_entitlements e
                               WHERE e.uid = $3
                           ) AS entitlement_count
                    FROM ella_invitations i
                    JOIN ella_invitation_capacity_reservations r
                      ON r.id = i.capacity_reservation_id
                    WHERE i.id = $1::uuid
                      AND r.id = $2::uuid
                    """,
                    invitation_id,
                    reservation_id,
                    uid,
                )
                return tuple(row.values())

        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner,
            )
            redemption = asyncio.create_task(_redeem(uid, "KMNP-2345"))
            for _attempt in range(100):
                async with pool.acquire() as probe:
                    waiting = await probe.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE locktype = 'advisory'
                              AND classid::bigint = $1
                              AND objid::bigint = $2
                              AND objsubid = 1
                              AND mode = 'ExclusiveLock'
                              AND NOT granted
                        )
                        """,
                        class_id,
                        object_id,
                    )
                if waiting:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("invitation writer never waited on the shared v1 lock")

            async with pool.acquire() as drift:
                async with drift.transaction():
                    await drift.execute(
                        "UPDATE users SET omi_uid = $2 WHERE id = $1",
                        user_id,
                        f"{uid}-moved",
                    )
                    replacement_id = await drift.fetchval(
                        """
                        INSERT INTO users (omi_uid, profile_class)
                        VALUES ($1, 'synthetic')
                        RETURNING id
                        """,
                        uid,
                    )
            await transaction.commit()

        with pytest.raises(
            authority_advisory_lock.AuthorityLockError,
            match="authority_lock_owner_drift",
        ):
            await asyncio.wait_for(redemption, timeout=5)
        assert await snapshot() == ("sent", 0, 0, 0)
        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id IN ($1, $2)
                    """,
                    user_id,
                    replacement_id,
                )
                == 0
            )

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
        target_consumed_at = await conn.fetchval(
            """
            SELECT consumed_at
            FROM ella_invitation_targets
            WHERE invitation_id = $1::uuid
            """,
            invitation_id,
        )
        redemption_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM ella_invitation_redemptions
            WHERE invitation_id = $1::uuid
            """,
            invitation_id,
        )
    assert dict(invitation) == {"state": "sent", "redemption_count": 0}
    assert consumed_slots == 0
    assert entitlement_count == 0
    assert target_consumed_at is None
    assert redemption_count == 0


def test_consent_revocation_at_insert_boundary_rolls_back_all_redemption_mutations(
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-consent-race"
        repository = ai_consent.InMemoryConsentRepository()
        monkeypatch.setattr(ai_consent, "_repository", repository)
        _set_pilot_rollout(monkeypatch, [uid])
        _grant_v7(repository, uid)
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="CDEF-2345",
            target_uids=[uid],
        )
        pilot_admission = invitation_authority.authorize_invitation_pilot(uid)
        revalidation_started = asyncio.Event()
        resume_revalidation = asyncio.Event()

        async def pause_before_current_consent_check(
            expected: invitations.InvitationPilotAdmission,
        ) -> invitations.InvitationPilotAdmission:
            assert expected == pilot_admission
            revalidation_started.set()
            await resume_revalidation.wait()
            return await invitation_authority.revalidate_invitation_pilot(expected)

        redemption = asyncio.create_task(
            invitations.redeem_invitation(
                uid=uid,
                code="CDEF-2345",
                source_address="192.0.2.30",
                app_build="synthetic-race-test",
                config=CONFIG,
                pilot_admission=pilot_admission,
                pilot_admission_revalidator=pause_before_current_consent_check,
            )
        )
        await asyncio.wait_for(revalidation_started.wait(), timeout=5)
        ai_consent.AiConsentService(repository).submit(
            uid,
            ai_consent.ConsentSubmission(
                decision="revoked",
                policy_version=ai_consent.CURRENT_POLICY_VERSION,
                processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
                request_id=f"request-revoke-{uid}",
                app_version="synthetic",
                build_number="1",
                locale="en",
                scope_version=ai_consent.CURRENT_SCOPE_VERSION,
                scope_hash=ai_consent.CURRENT_SCOPE_HASH,
            ),
        )
        resume_revalidation.set()

        with pytest.raises(invitations.InvitePilotGateDenied):
            await asyncio.wait_for(redemption, timeout=5)
        await _assert_unconsumed(
            pool,
            invitation_id=invitation_id,
            reservation_id=reservation_id,
            uid=uid,
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM ella_invitation_audit_receipts") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM ella_invitation_rate_limit_events") == 0

    asyncio.run(_run_with_database(scenario))


def test_revocation_started_after_revalidation_is_serialized_and_quarantines_grant(
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-consent-serialized"
        repository = ai_consent.InMemoryConsentRepository()
        monkeypatch.setattr(ai_consent, "_repository", repository)
        _set_pilot_rollout(monkeypatch, [uid])
        _grant_v7(repository, uid)
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="DEFG-2345",
            target_uids=[uid],
        )
        pilot_admission = invitation_authority.authorize_invitation_pilot(uid)
        authority_locked = asyncio.Event()
        resume_before_insert = asyncio.Event()
        original_lock = managed_cloud_consent.lock_or_bootstrap_grant_on_connection

        async def pause_after_revalidation(conn, *, grant, owner, owner_lock):
            epoch = await original_lock(
                conn,
                grant=grant,
                owner=owner,
                owner_lock=owner_lock,
            )
            authority_locked.set()
            await resume_before_insert.wait()
            return epoch

        monkeypatch.setattr(
            invitations.managed_cloud_consent,
            "lock_or_bootstrap_grant_on_connection",
            pause_after_revalidation,
        )
        redemption = asyncio.create_task(
            invitations.redeem_invitation(
                uid=uid,
                code="DEFG-2345",
                source_address="192.0.2.31",
                app_build="synthetic-serialized-test",
                config=CONFIG,
                pilot_admission=pilot_admission,
                pilot_admission_revalidator=invitation_authority.revalidate_invitation_pilot,
            )
        )
        await asyncio.wait_for(authority_locked.wait(), timeout=5)
        revocation_started = asyncio.Event()

        async def revoke():
            revocation_started.set()
            return await managed_cloud_consent.synchronize_denial(
                uid=uid,
                decision="revoked",
            )

        revocation = asyncio.create_task(revoke())
        await asyncio.wait_for(revocation_started.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not revocation.done()

        resume_before_insert.set()
        redeemed = await asyncio.wait_for(redemption, timeout=5)
        revoked = await asyncio.wait_for(revocation, timeout=5)

        assert redeemed["status"] == "invited"
        assert revoked["decision"] == "revoked"
        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT i.state, i.redemption_count, r.consumed_slots,
                       e.status AS entitlement_status,
                       e.consent_authority_epoch AS entitlement_epoch,
                       a.authority_epoch AS current_epoch,
                       a.decision AS authority_decision,
                       d.redeemed_at, a.updated_at AS authority_updated_at
                FROM ella_invitations i
                JOIN ella_invitation_capacity_reservations r
                  ON r.id = i.capacity_reservation_id
                JOIN ella_invitation_redemptions d
                  ON d.invitation_id = i.id
                JOIN voice_entitlements e
                  ON e.invitation_id = i.id
                JOIN users u ON u.omi_uid = e.uid
                JOIN ella_managed_cloud_consent_authority a
                  ON a.user_id = u.id
                WHERE i.id = $1::uuid
                """,
                invitation_id,
            )
            assert state["state"] == "redeemed"
            assert state["redemption_count"] == 1
            assert state["consumed_slots"] == 1
            assert state["entitlement_status"] == "revoked"
            assert state["authority_decision"] == "revoked"
            assert state["entitlement_epoch"] != state["current_epoch"]
            assert state["authority_updated_at"] >= state["redeemed_at"]
            assert (
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_runtime_targets t
                    JOIN users u ON u.id = t.account_user_id
                    WHERE u.omi_uid = $1
                      AND t.provider = 'hermes_cloud'
                      AND t.status = 'ready'
                    """,
                    uid,
                )
                == 0
            )

        # The invitation/capacity/redemption rows are historical proof of the
        # ordering winner. The only durable entitlement is revoked under the
        # newer epoch, so none of that state authorizes use after revocation.
        assert reservation_id

    asyncio.run(_run_with_database(scenario))


def test_postgres_revocation_epoch_wins_before_redemption_with_zero_mutation():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-consent-revocation-wins"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="EFGH-2345",
            target_uids=[uid],
        )
        await managed_cloud_consent.synchronize_denial(
            uid=uid,
            decision="revoked",
        )

        with pytest.raises(invitations.InvitePilotGateDenied):
            await _redeem(uid, "EFGH-2345")

        await _assert_unconsumed(
            pool,
            invitation_id=invitation_id,
            reservation_id=reservation_id,
            uid=uid,
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM ella_invitation_audit_receipts") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM ella_invitation_rate_limit_events") == 0
            assert (
                await conn.fetchval(
                    """
                    SELECT decision
                    FROM ella_managed_cloud_consent_authority a
                    JOIN users u ON u.id = a.user_id
                    WHERE u.omi_uid = $1
                    """,
                    uid,
                )
                == "revoked"
            )

    asyncio.run(_run_with_database(scenario))


def test_idempotent_retry_after_revocation_returns_no_authority_or_new_mutation():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-idempotent-revoked"
        invitation_id, reservation_id = await _seed_invitation(
            pool,
            code="GHJK-2345",
            target_uids=[uid],
        )
        await _redeem(uid, "GHJK-2345")
        await managed_cloud_consent.synchronize_denial(
            uid=uid,
            decision="revoked",
        )

        with pytest.raises(invitations.InvitePilotGateDenied):
            await _redeem(uid, "GHJK-2345")

        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT i.state, i.redemption_count, r.consumed_slots,
                       e.status AS entitlement_status
                FROM ella_invitations i
                JOIN ella_invitation_capacity_reservations r
                  ON r.id = i.capacity_reservation_id
                JOIN voice_entitlements e
                  ON e.invitation_id = i.id
                WHERE i.id = $1::uuid
                  AND r.id = $2::uuid
                """,
                invitation_id,
                reservation_id,
            )
            assert dict(state) == {
                "state": "redeemed",
                "redemption_count": 1,
                "consumed_slots": 1,
                "entitlement_status": "revoked",
            }
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
            assert (
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM ella_invitation_audit_receipts
                    WHERE invitation_id = $1::uuid
                    """,
                    invitation_id,
                )
                == 1
            )
            assert await conn.fetchval("SELECT COUNT(*) FROM ella_invitation_rate_limit_events") == 0

    asyncio.run(_run_with_database(scenario))


def test_revocation_transaction_error_rolls_back_epoch_and_quarantine():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-consent-revoke-error"
        await _seed_invitation(
            pool,
            code="FGHJ-2345",
            target_uids=[uid],
        )
        await _redeem(uid, "FGHJ-2345")
        async with pool.acquire() as conn:
            before = await conn.fetchrow(
                """
                SELECT a.authority_epoch, a.revision, a.decision, e.status
                FROM ella_managed_cloud_consent_authority a
                JOIN users u ON u.id = a.user_id
                JOIN voice_entitlements e ON e.uid = u.omi_uid
                WHERE u.omi_uid = $1
                """,
                uid,
            )
            await conn.execute(
                """
                CREATE FUNCTION fail_consent_quarantine() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'synthetic quarantine failure';
                END
                $$;
                CREATE TRIGGER fail_consent_quarantine
                BEFORE UPDATE ON voice_entitlements
                FOR EACH ROW
                WHEN (NEW.status = 'revoked')
                EXECUTE FUNCTION fail_consent_quarantine();
                """
            )

        with pytest.raises(managed_cloud_consent.ManagedCloudAuthorityUnavailable):
            await managed_cloud_consent.synchronize_denial(
                uid=uid,
                decision="revoked",
            )

        async with pool.acquire() as conn:
            after = await conn.fetchrow(
                """
                SELECT a.authority_epoch, a.revision, a.decision, e.status
                FROM ella_managed_cloud_consent_authority a
                JOIN users u ON u.id = a.user_id
                JOIN voice_entitlements e ON e.uid = u.omi_uid
                WHERE u.omi_uid = $1
                """,
                uid,
            )
        assert dict(after) == dict(before)
        assert after["decision"] == "granted"
        assert after["status"] == "invited"

    asyncio.run(_run_with_database(scenario))


def test_firestore_error_after_postgres_revocation_remains_fail_closed(
    monkeypatch,
):
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-consent-firestore-error"
        async with pool.acquire() as conn:
            await _ensure_users(conn, [uid])

        class FailingConsentService:
            @staticmethod
            def submit(_uid, _submission):
                raise RuntimeError("synthetic Firestore failure")

        monkeypatch.setenv(
            "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
            uid,
        )
        with pytest.raises(RuntimeError, match="synthetic Firestore failure"):
            await consent_authority.submit_with_managed_cloud_authority(
                uid=uid,
                submission=ai_consent.ConsentSubmission(
                    decision="revoked",
                    policy_version=ai_consent.CURRENT_POLICY_VERSION,
                    processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
                    request_id=f"request-revoke-{uid}",
                    app_version="synthetic",
                    build_number="1",
                    locale="en",
                    scope_version=ai_consent.CURRENT_SCOPE_VERSION,
                    scope_hash=ai_consent.CURRENT_SCOPE_HASH,
                ),
                service=FailingConsentService(),
            )

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                    SELECT decision
                    FROM ella_managed_cloud_consent_authority a
                    JOIN users u ON u.id = a.user_id
                    WHERE u.omi_uid = $1
                    """,
                    uid,
                )
                == "revoked"
            )

    asyncio.run(_run_with_database(scenario))


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


async def _issue_operator_invitation(
    pool: asyncpg.Pool,
    *,
    uid: str,
    code: str,
    expires_at: datetime,
    code_file_existed: bool = False,
    expected_database: str | None = None,
    code_file_path: str | None = None,
    environment: str = "postgres_test",
    operator: str = "pytest",
    recovery_receipt_valid: bool = True,
) -> dict:
    if expected_database is None:
        async with pool.acquire() as conn:
            expected_database = str(await conn.fetchval("SELECT current_database()"))
    identity = invitation_operator.SyntheticInvitationIdentity(
        uid=uid,
        account_uid=uid,
        profile_uid=uid,
    )
    context = invitation_operator.SyntheticInvitationContext(
        environment=environment,
        expected_database=expected_database,
        operator=operator,
    )
    admission = _admission(uid)
    code_file_ref_hmac = invitations.invitation_code_file_ref(
        OPERATOR_CONFIG,
        code_file_path or f"/root/ella-invites/{uid}.code",
    )
    recovery_binding_hmac = invitation_operator.synthetic_invitation_recovery_binding_hmac(
        identity=identity,
        context=context,
        admission=admission,
        code=code,
        code_file_ref_hmac=code_file_ref_hmac,
        expires_at=expires_at,
        config=OPERATOR_CONFIG,
    )
    if not recovery_receipt_valid:
        recovery_binding_hmac = "0" * 64
    return await invitation_operator.issue_synthetic_invitation(
        identity=identity,
        context=context,
        admission=admission,
        code=code,
        code_file_existed=code_file_existed,
        code_file_ref_hmac=code_file_ref_hmac,
        recovery_binding_hmac=recovery_binding_hmac,
        expires_at=expires_at,
        config=OPERATOR_CONFIG,
    )


def test_operator_issue_redeem_is_hmac_only_single_use_and_idempotent():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-operator-redeem"
        precommit_uid = "synthetic-operator-precommit-recovery"
        postcommit_uid = "synthetic-operator-postcommit-recovery"
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        async with pool.acquire() as conn:
            await _ensure_users(
                conn,
                [uid, precommit_uid, postcommit_uid],
            )

        issued = await _issue_operator_invitation(
            pool,
            uid=uid,
            code="ABCD-2345",
            expires_at=expiry,
        )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_stale_code_receipt",
        ):
            await _issue_operator_invitation(
                pool,
                uid=uid,
                code="ABCD-2345",
                expires_at=expiry,
                code_file_existed=True,
                code_file_path="/root/ella-invites/copied.code",
            )
        retried = await _issue_operator_invitation(
            pool,
            uid=uid,
            code="ABCD-2345",
            expires_at=expiry,
            code_file_existed=True,
        )
        assert issued["receipt_id"] == retried["receipt_id"]
        assert retried["idempotent"] is True
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_receipt_context_mismatch",
        ):
            await _issue_operator_invitation(
                pool,
                uid=uid,
                code="ABCD-2345",
                expires_at=expiry,
                code_file_existed=True,
                environment="other_environment",
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_receipt_context_mismatch",
        ):
            await invitation_operator.show_synthetic_invitation(
                receipt_id=issued["receipt_id"],
                identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
                context=invitation_operator.SyntheticInvitationContext(
                    "postgres_test",
                    await _current_database(pool),
                    "other_operator",
                ),
                config=OPERATOR_CONFIG,
            )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE FUNCTION fail_operator_issue_audit() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'injected operator precommit failure';
                END;
                $$;
                CREATE TRIGGER fail_operator_issue_audit
                BEFORE INSERT ON ella_invitation_audit_receipts
                FOR EACH ROW
                WHEN (NEW.event_type = 'operator_issued')
                EXECUTE FUNCTION fail_operator_issue_audit();
                """
            )
        with pytest.raises(
            asyncpg.PostgresError,
            match="injected operator precommit failure",
        ):
            await _issue_operator_invitation(
                pool,
                uid=precommit_uid,
                code="BCDE-2345",
                expires_at=expiry,
            )
        async with pool.acquire() as conn:
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM ella_invitations WHERE code_hmac = $1)",
                invitations.code_hmac(OPERATOR_CONFIG, "BCDE2345"),
            )
            await conn.execute(
                """
                DROP TRIGGER fail_operator_issue_audit
                    ON ella_invitation_audit_receipts;
                DROP FUNCTION fail_operator_issue_audit();
                """
            )
        precommit_recovered = await _issue_operator_invitation(
            pool,
            uid=precommit_uid,
            code="BCDE-2345",
            expires_at=expiry,
            code_file_existed=True,
        )
        assert precommit_recovered["idempotent"] is False

        postcommit_receipt = {}

        async def issue_then_lose_commit_result() -> None:
            committed = await _issue_operator_invitation(
                pool,
                uid=postcommit_uid,
                code="DEFG-2345",
                expires_at=expiry,
            )
            postcommit_receipt.update(committed)
            raise ConnectionError("injected postcommit outcome ambiguity")

        with pytest.raises(
            ConnectionError,
            match="injected postcommit outcome ambiguity",
        ):
            await issue_then_lose_commit_result()
        postcommit_recovered = await _issue_operator_invitation(
            pool,
            uid=postcommit_uid,
            code="DEFG-2345",
            expires_at=expiry,
            code_file_existed=True,
        )
        assert postcommit_recovered["receipt_id"] == postcommit_receipt["receipt_id"]
        assert postcommit_recovered["idempotent"] is True

        first = await _redeem(
            uid,
            "ABCD-2345",
            config=OPERATOR_CONFIG,
        )
        second = await _redeem(
            uid,
            "abcd 2345",
            config=OPERATOR_CONFIG,
        )
        assert first["status"] == second["status"] == "invited"
        assert first["revision"] == second["revision"] == 1

        async with pool.acquire() as conn:
            invitation = dict(
                await conn.fetchrow(
                    """
                    SELECT code_hmac, display_hint, state, redemption_count,
                           cohort, exclude_from_product_analytics
                    FROM ella_invitations
                    WHERE id = $1::uuid
                    """,
                    issued["receipt_id"],
                )
            )
            target = dict(
                await conn.fetchrow(
                    """
                    SELECT account_ref_hmac, profile_ref_hmac,
                           required_profile_class, consumed_at
                    FROM ella_invitation_targets
                    WHERE invitation_id = $1::uuid
                    """,
                    issued["receipt_id"],
                )
            )
            counts = dict(
                await conn.fetchrow(
                    """
                    SELECT
                        (
                            SELECT COUNT(*) FROM ella_invitations
                            WHERE cohort = $2 AND id = $1::uuid
                        ) AS invitations,
                        (
                            SELECT COUNT(*) FROM ella_invitation_redemptions
                            WHERE invitation_id = $1::uuid
                        ) AS redemptions,
                        (
                            SELECT COUNT(*) FROM ella_invitation_audit_receipts
                            WHERE invitation_id = $1::uuid
                              AND event_type = 'operator_issued'
                        ) AS issued_audits,
                        (
                            SELECT COUNT(*) FROM ella_invitation_audit_receipts
                            WHERE invitation_id = $1::uuid
                              AND event_type = 'operator_idempotent_retry'
                        ) AS retry_audits
                    """,
                    issued["receipt_id"],
                    invitations.SYNTHETIC_OPERATOR_COHORT,
                )
            )
        serialized = json.dumps(
            {"invitation": invitation, "target": target},
            default=str,
        )
        assert "ABCD-2345" not in serialized
        assert "ABCD2345" not in serialized
        assert uid not in serialized
        assert invitation["display_hint"] is None
        assert invitation["state"] == "redeemed"
        assert invitation["redemption_count"] == 1
        assert invitation["cohort"] == invitations.SYNTHETIC_OPERATOR_COHORT
        assert invitation["exclude_from_product_analytics"] is True
        assert target["required_profile_class"] == "synthetic"
        assert target["consumed_at"] is not None
        assert counts == {
            "invitations": 1,
            "redemptions": 1,
            "issued_audits": 1,
            "retry_audits": 1,
        }

        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_revoke_refused",
        ):
            await invitation_operator.revoke_synthetic_invitation(
                receipt_id=issued["receipt_id"],
                expected_version=2,
                identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
                context=invitation_operator.SyntheticInvitationContext(
                    "postgres_test",
                    await _current_database(pool),
                    "pytest",
                ),
                config=OPERATOR_CONFIG,
            )

    asyncio.run(_run_with_database(scenario))


def test_operator_policy_drift_cannot_bypass_disabled_ordinary_gate():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-operator-policy-drift"
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        async with pool.acquire() as conn:
            await _ensure_users(conn, [uid])
        issued = await _issue_operator_invitation(
            pool,
            uid=uid,
            code="CDEF-2345",
            expires_at=expiry,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_invitations
                SET entitlement_policy = jsonb_set(
                    entitlement_policy,
                    '{daily_limit_s}',
                    '999999'::jsonb
                )
                WHERE id = $1::uuid
                """,
                issued["receipt_id"],
            )

        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(
                uid,
                "CDEF-2345",
                config=OPERATOR_CONFIG,
            )
        assert error.value.code == "invalid"
        async with pool.acquire() as conn:
            state = dict(
                await conn.fetchrow(
                    """
                    SELECT i.redemption_count, t.consumed_at,
                           r.state AS reservation_state
                    FROM ella_invitations i
                    JOIN ella_invitation_targets t
                      ON t.invitation_id = i.id
                    JOIN ella_invitation_capacity_reservations r
                      ON r.id = i.capacity_reservation_id
                    WHERE i.id = $1::uuid
                    """,
                    issued["receipt_id"],
                )
            )
        assert state == {
            "redemption_count": 0,
            "consumed_at": None,
            "reservation_state": "reserved",
        }

    asyncio.run(_run_with_database(scenario))


async def _current_database(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        return str(await conn.fetchval("SELECT current_database()"))


async def _wait_for_operator_authority_waiter(
    pool: asyncpg.Pool,
    owner: authority_advisory_lock.AuthorityOwner,
) -> None:
    key = authority_advisory_lock.authority_lock_key(
        str(owner.account_id),
        str(owner.profile_id),
    )
    class_id, object_id = authority_advisory_lock._advisory_lock_parts(key)
    for _attempt in range(100):
        async with pool.acquire() as observer:
            waiting = await observer.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND classid::bigint = $1
                      AND objid::bigint = $2
                      AND objsubid = 1
                      AND mode = 'ExclusiveLock'
                      AND NOT granted
                )
                """,
                class_id,
                object_id,
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    pytest.fail("operator cleanup never waited on the shared v1 authority lock")


def test_operator_revoke_cleanup_is_exact_and_real_user_safe():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "synthetic-operator-cleanup"
        other_uid = "synthetic-operator-other"
        contention_uid = "synthetic-operator-cleanup-contention"
        owner_drift_uid = "synthetic-operator-cleanup-owner-drift"
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        async with pool.acquire() as conn:
            await _ensure_users(
                conn,
                [
                    uid,
                    other_uid,
                    contention_uid,
                    owner_drift_uid,
                ],
            )
        issued = await _issue_operator_invitation(
            pool,
            uid=uid,
            code="EFGH-2345",
            expires_at=expiry,
        )
        context = invitation_operator.SyntheticInvitationContext(
            "postgres_test",
            await _current_database(pool),
            "pytest",
        )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_receipt_binding_mismatch",
        ):
            await invitation_operator.show_synthetic_invitation(
                receipt_id=issued["receipt_id"],
                identity=invitation_operator.SyntheticInvitationIdentity(
                    other_uid,
                    other_uid,
                    other_uid,
                ),
                context=context,
                config=OPERATOR_CONFIG,
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_stale_receipt",
        ):
            await invitation_operator.revoke_synthetic_invitation(
                receipt_id=issued["receipt_id"],
                expected_version=99,
                identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
                context=context,
                config=OPERATOR_CONFIG,
            )

        revoked = await invitation_operator.revoke_synthetic_invitation(
            receipt_id=issued["receipt_id"],
            expected_version=1,
            identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
            context=context,
            config=OPERATOR_CONFIG,
        )
        assert revoked["state"] == "revoked"
        assert revoked["version"] == 2
        assert revoked["current_profile_class"] == "synthetic"

        cleaned = await invitation_operator.cleanup_synthetic_invitation(
            receipt_id=issued["receipt_id"],
            expected_version=2,
            identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
            context=context,
            config=OPERATOR_CONFIG,
        )
        assert cleaned["state"] == "revoked"
        assert cleaned["version"] == 3
        assert cleaned["current_profile_class"] == "real"
        shown = await invitation_operator.show_synthetic_invitation(
            receipt_id=issued["receipt_id"],
            identity=invitation_operator.SyntheticInvitationIdentity(uid, uid, uid),
            context=context,
            config=OPERATOR_CONFIG,
        )
        assert shown["current_profile_class"] == "real"

        async with pool.acquire() as conn:
            state = dict(
                await conn.fetchrow(
                    """
                    SELECT i.state, i.delivery_state, r.state AS reservation_state,
                           u.profile_class,
                           (
                               SELECT COUNT(*)
                               FROM ella_invitation_audit_receipts a
                               WHERE a.invitation_id = i.id
                                 AND a.event_type IN (
                                     'operator_revoked', 'operator_cleanup'
                                 )
                           ) AS lifecycle_audits
                    FROM ella_invitations i
                    JOIN ella_invitation_capacity_reservations r
                      ON r.id = i.capacity_reservation_id
                    JOIN ella_invitation_targets t
                      ON t.invitation_id = i.id
                    JOIN users u ON u.omi_uid = $2
                    WHERE i.id = $1::uuid
                    """,
                    issued["receipt_id"],
                    uid,
                )
            )
        assert state == {
            "state": "revoked",
            "delivery_state": "suppressed",
            "reservation_state": "released",
            "profile_class": "real",
            "lifecycle_audits": 2,
        }

        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_real_profile_refused|operator_stale_code_receipt",
        ):
            await _issue_operator_invitation(
                pool,
                uid=uid,
                code="JKMN-2345",
                expires_at=expiry,
            )

        contention_issue = await _issue_operator_invitation(
            pool,
            uid=contention_uid,
            code="MNPR-2345",
            expires_at=expiry,
        )
        await invitation_operator.revoke_synthetic_invitation(
            receipt_id=contention_issue["receipt_id"],
            expected_version=1,
            identity=invitation_operator.SyntheticInvitationIdentity(
                contention_uid,
                contention_uid,
                contention_uid,
            ),
            context=context,
            config=OPERATOR_CONFIG,
        )
        async with pool.acquire() as conn:
            contention_user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                contention_uid,
            )
        contention_owner = authority_advisory_lock.AuthorityOwner.from_values(
            contention_user_id,
            contention_user_id,
        )
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=contention_owner,
            )
            cleanup_task = asyncio.create_task(
                invitation_operator.cleanup_synthetic_invitation(
                    receipt_id=contention_issue["receipt_id"],
                    expected_version=2,
                    identity=invitation_operator.SyntheticInvitationIdentity(
                        contention_uid,
                        contention_uid,
                        contention_uid,
                    ),
                    context=context,
                    config=OPERATOR_CONFIG,
                )
            )
            await _wait_for_operator_authority_waiter(
                pool,
                contention_owner,
            )
            assert not cleanup_task.done()
            async with pool.acquire() as observer:
                blocked_state = dict(
                    await observer.fetchrow(
                        """
                        SELECT u.profile_class, i.state, i.version
                        FROM users u
                        JOIN ella_invitation_targets t
                          ON t.account_ref_hmac = $2
                        JOIN ella_invitations i ON i.id = t.invitation_id
                        WHERE u.id = $1 AND i.id = $3::uuid
                        """,
                        contention_user_id,
                        invitations.invitation_target_refs(
                            OPERATOR_CONFIG,
                            account_uid=contention_uid,
                            profile_uid=contention_uid,
                        )[0],
                        contention_issue["receipt_id"],
                    )
                )
            assert blocked_state == {
                "profile_class": "synthetic",
                "state": "revoked",
                "version": 2,
            }
            await transaction.commit()
        contention_cleaned = await asyncio.wait_for(cleanup_task, timeout=5)
        assert contention_cleaned["current_profile_class"] == "real"

        owner_drift_issue = await _issue_operator_invitation(
            pool,
            uid=owner_drift_uid,
            code="QRST-2345",
            expires_at=expiry,
        )
        await invitation_operator.revoke_synthetic_invitation(
            receipt_id=owner_drift_issue["receipt_id"],
            expected_version=1,
            identity=invitation_operator.SyntheticInvitationIdentity(
                owner_drift_uid,
                owner_drift_uid,
                owner_drift_uid,
            ),
            context=context,
            config=OPERATOR_CONFIG,
        )
        async with pool.acquire() as conn:
            owner_drift_user_id = await conn.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                owner_drift_uid,
            )
        owner_drift_owner = authority_advisory_lock.AuthorityOwner.from_values(
            owner_drift_user_id,
            owner_drift_user_id,
        )
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner_drift_owner,
            )
            owner_drift_cleanup = asyncio.create_task(
                invitation_operator.cleanup_synthetic_invitation(
                    receipt_id=owner_drift_issue["receipt_id"],
                    expected_version=2,
                    identity=invitation_operator.SyntheticInvitationIdentity(
                        owner_drift_uid,
                        owner_drift_uid,
                        owner_drift_uid,
                    ),
                    context=context,
                    config=OPERATOR_CONFIG,
                )
            )
            await _wait_for_operator_authority_waiter(
                pool,
                owner_drift_owner,
            )
            async with pool.acquire() as drift:
                async with drift.transaction():
                    await drift.execute(
                        "UPDATE users SET omi_uid = $2 WHERE id = $1",
                        owner_drift_user_id,
                        f"{owner_drift_uid}-moved",
                    )
                    replacement_id = await drift.fetchval(
                        """
                        INSERT INTO users (omi_uid, profile_class)
                        VALUES ($1, 'synthetic')
                        RETURNING id
                        """,
                        owner_drift_uid,
                    )
            await transaction.commit()

        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_identity_drift",
        ):
            await asyncio.wait_for(owner_drift_cleanup, timeout=5)
        async with pool.acquire() as observer:
            owner_drift_state = dict(
                await observer.fetchrow(
                    """
                    SELECT
                        (SELECT profile_class FROM users WHERE id = $1)
                            AS original_profile_class,
                        (SELECT profile_class FROM users WHERE id = $2)
                            AS replacement_profile_class,
                        i.state,
                        i.version,
                        (
                            SELECT COUNT(*)::integer
                            FROM ella_invitation_audit_receipts a
                            WHERE a.invitation_id = i.id
                              AND a.event_type = 'operator_cleanup'
                        ) AS cleanup_audits
                    FROM ella_invitations i
                    WHERE i.id = $3::uuid
                    """,
                    owner_drift_user_id,
                    replacement_id,
                    owner_drift_issue["receipt_id"],
                )
            )
        assert owner_drift_state == {
            "original_profile_class": "synthetic",
            "replacement_profile_class": "synthetic",
            "state": "revoked",
            "version": 2,
            "cleanup_audits": 0,
        }

    asyncio.run(_run_with_database(scenario))


def test_operator_refuses_real_existing_collision_wrong_database_and_expiry():
    async def scenario(pool: asyncpg.Pool) -> None:
        real_uid = "synthetic-operator-real-refusal"
        collision_uid = "synthetic-operator-collision"
        existing_uid = "synthetic-existing-code-owner"
        expiry_uid = "synthetic-operator-expired"
        artifact_uid = "synthetic-operator-existing-artifact"
        stale_uid = "synthetic-operator-stale-file"
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        async with pool.acquire() as conn:
            await _ensure_users(conn, [real_uid], profile_class="real")
            await _ensure_users(
                conn,
                [collision_uid, expiry_uid, artifact_uid, stale_uid],
            )
            await conn.execute(
                "INSERT INTO voice_entitlements (uid) VALUES ($1)",
                artifact_uid,
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_real_profile_refused",
        ):
            await _issue_operator_invitation(
                pool,
                uid=real_uid,
                code="JKMN-2345",
                expires_at=expiry,
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_database_mismatch",
        ):
            await _issue_operator_invitation(
                pool,
                uid=collision_uid,
                code="MNPQ-2345",
                expires_at=expiry,
                expected_database="wrong_database",
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_existing_profile_artifacts",
        ):
            await _issue_operator_invitation(
                pool,
                uid=artifact_uid,
                code="NPQR-2345",
                expires_at=expiry,
            )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_stale_code_receipt",
        ):
            await _issue_operator_invitation(
                pool,
                uid=stale_uid,
                code="PQRS-2345",
                expires_at=expiry,
                code_file_existed=True,
                recovery_receipt_valid=False,
            )

        await _seed_invitation(
            pool,
            code="RSTU-2345",
            target_uids=[existing_uid],
        )
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_code_collision",
        ):
            await _issue_operator_invitation(
                pool,
                uid=collision_uid,
                code="RSTU-2345",
                expires_at=expiry,
            )

        issued = await _issue_operator_invitation(
            pool,
            uid=expiry_uid,
            code="VWXY-2345",
            expires_at=expiry,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_invitations
                SET expires_at = NOW() - INTERVAL '1 minute'
                WHERE id = $1::uuid
                """,
                issued["receipt_id"],
            )
        with pytest.raises(invitations.InviteRedemptionFailure) as error:
            await _redeem(
                expiry_uid,
                "VWXY-2345",
                config=OPERATOR_CONFIG,
            )
        assert error.value.code == "expired"
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                    SELECT redemption_count
                    FROM ella_invitations
                    WHERE id = $1::uuid
                    """,
                    issued["receipt_id"],
                )
                == 0
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


def test_default_gate_requires_v8_exact_allowlists_and_synthetic_profile(
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
        _set_pilot_rollout(
            monkeypatch,
            [allowed, no_consent, real_profile],
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


async def _prepare_deletion_account(
    pool: asyncpg.Pool,
    *,
    uid: str,
    email: str,
    code: str,
    activate_runtime: bool,
) -> tuple[str, str]:
    issued = await pilot_invite_admin._issue_invitation(
        code=code,
        code_file_existed=False,
        code_file_ref_hmac="7" * 64,
        kind="ordinary",
        allowed_email_hash=pilot_invite_admin._email_hash(CONFIG, email),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        environment="account-deletion-postgres-test",
        config=CONFIG,
    )
    await _redeem_self_hosted(uid, email, code)
    if activate_runtime:
        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        repository = EllaProvisioningRepository(pool)
        await repository.stage_runtime_binding(
            uid=uid,
            binding=_local_runtime_binding(uid),
        )
        await repository.activate_runtime_binding(
            uid=uid,
            provider="hermes",
            require_invitation_target=True,
            authority_lineage=_self_hosted_lineage(),
            model=SELF_HOSTED_RUNTIME_MODEL,
        )
        async with pool.acquire() as connection:
            user_id = await connection.fetchval(
                "SELECT id FROM users WHERE omi_uid = $1",
                uid,
            )
            await connection.execute(
                """
                INSERT INTO agent_clusters (user_id, agents, status)
                VALUES ($1, '{"agentId":"content-free"}'::jsonb, 'ACTIVE')
                """,
                user_id,
            )
    return str(issued["receipt_id"]), uid


def test_account_deletion_atomically_quarantines_authority_releases_capacity_and_retries():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id, uid = await _prepare_deletion_account(
            pool,
            uid="account-delete-complete",
            email="account-delete-complete@example.test",
            code="CDEF-6789",
            activate_runtime=True,
        )

        first = await account_deletion.quarantine_account_for_deletion(uid)
        second = await account_deletion.quarantine_account_for_deletion(uid)

        assert first.capacity_released is True
        assert first.authority_quarantined is True
        assert first.external_cleanup_required == (
            "hermes_profile",
            "honcho_tenancy",
            "runtime_registry",
        )
        assert second.capacity_released is True
        assert second.counts["invitations"] == 0
        assert second.counts["capacity_reservations"] == 0
        with pytest.raises(account_deletion.AccountDeletionUnavailable) as pending:
            await account_deletion.finalize_account_deletion(uid)
        assert pending.value.code == "account_deletion_external_cleanup_incomplete"

        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT invitation.state AS invitation_state,
                       reservation.state AS capacity_state,
                       entitlement.status AS entitlement_status,
                       app_user.status AS user_status,
                       app_user.email,
                       binding.status AS binding_status,
                       binding.active,
                       cluster.status AS cluster_status,
                       cluster.agents
                FROM ella_invitations invitation
                JOIN ella_invitation_capacity_reservations reservation
                  ON reservation.id = invitation.capacity_reservation_id
                JOIN ella_invitation_redemptions redemption
                  ON redemption.invitation_id = invitation.id
                JOIN users app_user ON app_user.id = redemption.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = app_user.omi_uid
                JOIN ella_runtime_bindings binding
                  ON binding.user_id = app_user.id AND binding.provider = 'hermes'
                JOIN agent_clusters cluster ON cluster.user_id = app_user.id
                WHERE invitation.id = $1::uuid
                """,
                invitation_id,
            )
            target_states = await connection.fetch(
                """
                SELECT status
                FROM ella_runtime_targets
                WHERE invitation_target_id IN (
                    SELECT invitation_target_id
                    FROM ella_invitation_redemptions
                    WHERE invitation_id = $1::uuid
                )
                """,
                invitation_id,
            )
        row_data = dict(row)
        deleted_email = row_data.pop("email")
        assert row_data == {
            "invitation_state": "revoked",
            "capacity_state": "released",
            "entitlement_status": "revoked",
            "user_status": "DELETION_PENDING",
            "binding_status": "disabled",
            "active": False,
            "cluster_status": "INACTIVE",
            "agents": "{}",
        }
        assert deleted_email.startswith("deleted+")
        assert deleted_email.endswith("@invalid.ella")
        assert deleted_email != "account-delete-complete@example.test"
        assert {target["status"] for target in target_states} == {"revoked"}

        # A trusted operator can remove only the already-quarantined external
        # binding after independently proving the profile/tenancy are absent.
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    DELETE FROM ella_runtime_targets
                    WHERE account_user_id = (
                        SELECT id FROM users WHERE omi_uid = $1
                    )
                    """,
                    uid,
                )
                await connection.execute(
                    """
                    DELETE FROM ella_runtime_bindings
                    WHERE user_id = (
                        SELECT id FROM users WHERE omi_uid = $1
                    ) AND provider = 'hermes'
                    """,
                    uid,
                )
        after_external_cleanup = await account_deletion.quarantine_account_for_deletion(uid)
        assert after_external_cleanup.external_cleanup_required == ()
        assert await account_deletion.finalize_account_deletion(uid) is True
        assert await account_deletion.finalize_account_deletion(uid) is False
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT status FROM users WHERE omi_uid = $1",
                    uid,
                )
                == "DELETED"
            )

    asyncio.run(_run_with_database(scenario))


def test_account_deletion_routing_trace_purge_is_exact_repeatable_and_retains_other_users():
    async def scenario(pool: asyncpg.Pool) -> None:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE routing_traces (
                    trace_id TEXT PRIMARY KEY,
                    uid TEXT,
                    notes JSONB NOT NULL DEFAULT '[]'::jsonb,
                    client_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
                    resolved_session_key TEXT,
                    error TEXT
                )
                """
            )
            await connection.executemany(
                """
                INSERT INTO routing_traces (
                    trace_id, uid, notes, client_headers, resolved_session_key, error
                ) VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
                """,
                [
                    ("owned-1", "routing-delete-user", '["sensitive-1"]', '{"Secret":"one"}', "session-1", "error-1"),
                    ("owned-2", "routing-delete-user", '["sensitive-2"]', '{"Secret":"two"}', "session-2", "error-2"),
                    ("retained", "routing-retained-user", '["retained"]', '{}', None, None),
                ],
            )

        assert await account_deletion.purge_routing_traces("routing-delete-user") == 2
        assert await account_deletion.purge_routing_traces("routing-delete-user") == 0
        async with pool.acquire() as connection:
            rows = await connection.fetch("SELECT trace_id, uid, notes FROM routing_traces ORDER BY trace_id")
        assert [dict(row) for row in rows] == [
            {
                "trace_id": "retained",
                "uid": "routing-retained-user",
                "notes": '["retained"]',
            }
        ]

    asyncio.run(_run_with_database(scenario))


def test_account_deletion_memory_reinterpretation_purge_removes_attempts_and_retains_other_users():
    async def scenario(pool: asyncpg.Pool) -> None:
        async with pool.acquire() as connection:
            await connection.executemany(
                """
                INSERT INTO memory_reinterpretation_jobs (
                    id, uid, logical_session_id, conversation_id,
                    starting_summary_version_id, source_identity,
                    canonical_refs, transcript_hash, status, not_before
                )
                VALUES ($1, $2, $3, $4, $5, $6, '[]'::jsonb, $7, 'running', NOW())
                """,
                [
                    (
                        "owned-memory-job",
                        "memory-delete-user",
                        "session-owned",
                        "conversation-owned",
                        "v1",
                        "source-owned",
                        "hash-owned",
                    ),
                    (
                        "retained-memory-job",
                        "memory-retained-user",
                        "session-retained",
                        "conversation-retained",
                        "v1",
                        "source-retained",
                        "hash-retained",
                    ),
                ],
            )
            await connection.executemany(
                """
                INSERT INTO memory_reinterpretation_attempts (
                    job_id, transcript_revision, attempt_number,
                    lease_token, worker_id, status
                )
                VALUES ($1, 1, 1, $2, $3, 'running')
                """,
                [
                    ("owned-memory-job", "owned-lease", "owned-worker"),
                    ("retained-memory-job", "retained-lease", "retained-worker"),
                ],
            )

        assert await account_deletion.purge_memory_reinterpretation_work("memory-delete-user") == 2
        assert await account_deletion.purge_memory_reinterpretation_work("memory-delete-user") == 0
        async with pool.acquire() as connection:
            jobs = await connection.fetch("SELECT id, uid FROM memory_reinterpretation_jobs ORDER BY id")
            attempts = await connection.fetch(
                "SELECT job_id, lease_token FROM memory_reinterpretation_attempts ORDER BY job_id"
            )
        assert [dict(row) for row in jobs] == [{"id": "retained-memory-job", "uid": "memory-retained-user"}]
        assert [dict(row) for row in attempts] == [{"job_id": "retained-memory-job", "lease_token": "retained-lease"}]

    asyncio.run(_run_with_database(scenario))


def test_canonical_ledger_positive_control_exact_purge_rollback_and_retry():
    async def scenario(pool: asyncpg.Pool) -> None:
        target_uid = "canonical-delete-user"
        retained_uid = "canonical-retained-user"
        async with pool.acquire() as connection:
            await connection.executemany(
                """
                INSERT INTO canonical_events (uid, event_id, text, metadata, raw_event)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                """,
                [
                    (target_uid, "owned-1", "private transcript one", '{"source":"owned"}', '{"uid":"owned"}'),
                    (target_uid, "owned-2", "private transcript two", '{"source":"owned"}', '{"uid":"owned"}'),
                    (retained_uid, "retained", "retained transcript", '{"source":"other"}', '{"uid":"other"}'),
                ],
            )
            await connection.executemany(
                """
                INSERT INTO canonical_event_sessions (uid, session_id, metadata, raw_completion)
                VALUES ($1, $2, $3::jsonb, $4::jsonb)
                """,
                [
                    (target_uid, "owned-session", '{"source":"owned"}', '{"uid":"owned"}'),
                    (retained_uid, "retained-session", '{"source":"other"}', '{"uid":"other"}'),
                ],
            )

        # Positive control: the previously available deletion purges do not
        # touch either canonical table.
        assert await account_deletion.purge_routing_traces(target_uid) == 0
        assert await account_deletion.purge_memory_reinterpretation_work(target_uid) == 0
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT COUNT(*) FROM canonical_events WHERE uid = $1", target_uid) == 2
            assert (
                await connection.fetchval("SELECT COUNT(*) FROM canonical_event_sessions WHERE uid = $1", target_uid)
                == 1
            )
            await connection.execute(
                """
                CREATE FUNCTION suppress_canonical_session_delete()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RETURN NULL;
                END
                $$;
                CREATE TRIGGER suppress_canonical_session_delete
                BEFORE DELETE ON canonical_event_sessions
                FOR EACH ROW EXECUTE FUNCTION suppress_canonical_session_delete()
                """
            )

        with pytest.raises(account_deletion.AccountDeletionUnavailable) as ambiguous:
            await account_deletion.purge_canonical_event_ledger(target_uid)
        assert ambiguous.value.code == "account_deletion_canonical_event_ledger_unavailable"
        async with pool.acquire() as connection:
            # The affected-count ambiguity rolls back the event delete too.
            assert await connection.fetchval("SELECT COUNT(*) FROM canonical_events WHERE uid = $1", target_uid) == 2
            assert (
                await connection.fetchval("SELECT COUNT(*) FROM canonical_event_sessions WHERE uid = $1", target_uid)
                == 1
            )
            await connection.execute("DROP TRIGGER suppress_canonical_session_delete ON canonical_event_sessions")

        assert await account_deletion.purge_canonical_event_ledger(target_uid) == 3
        assert await account_deletion.purge_canonical_event_ledger(target_uid) == 0
        async with pool.acquire() as connection:
            retained = await connection.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_events WHERE uid = $1) AS events,
                    (SELECT COUNT(*) FROM canonical_event_sessions WHERE uid = $1) AS sessions
                """,
                retained_uid,
            )
            assert tuple(retained.values()) == (1, 1)
            await connection.execute("DROP TABLE canonical_event_sessions")
            await connection.execute(
                """
                INSERT INTO canonical_events (uid, event_id, text, metadata, raw_event)
                VALUES ($1, 'missing-table', 'private transcript', '{}'::jsonb, '{}'::jsonb)
                """,
                target_uid,
            )

        with pytest.raises(account_deletion.AccountDeletionUnavailable) as missing:
            await account_deletion.purge_canonical_event_ledger(target_uid)
        assert missing.value.code == "account_deletion_canonical_event_ledger_unavailable"
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT COUNT(*) FROM canonical_events WHERE uid = $1", target_uid) == 1

    asyncio.run(_run_with_database(scenario))


def test_account_deletion_mid_transaction_failure_rolls_back_every_authority_and_capacity_write():
    async def scenario(pool: asyncpg.Pool) -> None:
        invitation_id, uid = await _prepare_deletion_account(
            pool,
            uid="account-delete-rollback",
            email="account-delete-rollback@example.test",
            code="DEFG-789A",
            activate_runtime=True,
        )
        async with pool.acquire() as connection:
            await connection.execute(
                """
                CREATE FUNCTION fail_account_deletion_user_update()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.status = 'DELETION_PENDING' THEN
                        RAISE EXCEPTION 'synthetic account deletion failure';
                    END IF;
                    RETURN NEW;
                END
                $$
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER fail_account_deletion_user_update
                BEFORE UPDATE ON users
                FOR EACH ROW EXECUTE FUNCTION fail_account_deletion_user_update()
                """
            )

        with pytest.raises(account_deletion.AccountDeletionUnavailable) as failed:
            await account_deletion.quarantine_account_for_deletion(uid)
        assert failed.value.code == "account_deletion_authority_unavailable"

        async with pool.acquire() as connection:
            state = await connection.fetchrow(
                """
                SELECT invitation.state AS invitation_state,
                       reservation.state AS capacity_state,
                       entitlement.status AS entitlement_status,
                       app_user.status AS user_status,
                       binding.status AS binding_status,
                       binding.active
                FROM ella_invitations invitation
                JOIN ella_invitation_capacity_reservations reservation
                  ON reservation.id = invitation.capacity_reservation_id
                JOIN ella_invitation_redemptions redemption
                  ON redemption.invitation_id = invitation.id
                JOIN users app_user ON app_user.id = redemption.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = app_user.omi_uid
                JOIN ella_runtime_bindings binding
                  ON binding.user_id = app_user.id AND binding.provider = 'hermes'
                WHERE invitation.id = $1::uuid
                """,
                invitation_id,
            )
        assert dict(state) == {
            "invitation_state": "redeemed",
            "capacity_state": "consumed",
            "entitlement_status": "active",
            "user_status": "ACTIVE",
            "binding_status": "active",
            "active": True,
        }

    asyncio.run(_run_with_database(scenario))


def test_account_deletion_service_handles_partial_provision_and_repeat_without_operator():
    async def scenario(pool: asyncpg.Pool) -> None:
        _invitation_id, uid = await _prepare_deletion_account(
            pool,
            uid="account-delete-partial",
            email="account-delete-partial@example.test",
            code="EFGH-89AB",
            activate_runtime=False,
        )
        side_effects = []
        for _attempt in range(2):
            result = await account_deletion_service.execute_account_deletion(
                uid,
                delete_firestore=lambda exact_uid: side_effects.append(("firestore", exact_uid)),
                delete_firebase=lambda exact_uid: side_effects.append(("firebase", exact_uid)),
            )
            assert result.status_code == 200
            assert result.body["status"] == "ok"
            assert result.body["deletion_receipt"]["status"] == "completed"

        async with pool.acquire() as connection:
            state = await connection.fetchrow(
                """
                SELECT app_user.status AS user_status,
                       invitation.state AS invitation_state,
                       reservation.state AS capacity_state,
                       entitlement.status AS entitlement_status
                FROM users app_user
                JOIN ella_invitation_redemptions redemption
                  ON redemption.user_id = app_user.id
                JOIN ella_invitations invitation
                  ON invitation.id = redemption.invitation_id
                JOIN ella_invitation_capacity_reservations reservation
                  ON reservation.id = invitation.capacity_reservation_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = app_user.omi_uid
                WHERE app_user.omi_uid = $1
                """,
                uid,
            )
        assert dict(state) == {
            "user_status": "DELETED",
            "invitation_state": "revoked",
            "capacity_state": "released",
            "entitlement_status": "revoked",
        }
        assert side_effects == [
            ("firestore", uid),
            ("firebase", uid),
            ("firestore", uid),
            ("firebase", uid),
        ]

    asyncio.run(_run_with_database(scenario))


def test_lost_provider_ack_persists_unproven_attempt_blocks_firebase_and_retry_converges(monkeypatch):
    class LostAcknowledgementClient(HermesProvisionClient):
        provider_created = False

        @staticmethod
        def resolve_authority(_expected_snapshot=None):
            return None

        @classmethod
        def snapshot_authority(cls, _expected_snapshot=None):
            return None

        async def provision(self, _identity, _target_schema_version, **kwargs):
            assert kwargs["idempotency_key"]
            self.provider_created = True
            raise ProvisioningError("provision_service_timeout", retryable=True)

    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "account-delete-lost-provider-ack"
        email = "account-delete-lost-provider-ack@example.test"
        await _prepare_deletion_account(
            pool,
            uid=uid,
            email=email,
            code="FGHJ-9ABC",
            activate_runtime=False,
        )
        await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        async with pool.acquire() as connection:
            job = dict(
                await connection.fetchrow(
                    """
                    INSERT INTO ella_provisioning_jobs (user_id, target_schema_version, state, stage)
                    SELECT id, 'hermes-user-v1', 'provisioning', 'profile_ready'
                    FROM users WHERE omi_uid = $1
                    RETURNING *
                    """,
                    uid,
                )
            )

        monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
        monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
        client = LostAcknowledgementClient()
        coordinator = ProvisioningCoordinator(EllaProvisioningRepository(pool), client=client)
        await coordinator.process_claimed_job(
            job=job,
            identity=VerifiedIdentity(uid=uid, email=email, name="Synthetic", timezone="UTC"),
        )
        assert client.provider_created is True

        async with pool.acquire() as connection:
            marker = dict(
                await connection.fetchrow(
                    """
                    SELECT proof_state, content_free, idempotency_key, correlation_ref
                    FROM ella_provider_attempts
                    WHERE provisioning_job_id = $1
                    """,
                    job["id"],
                )
            )
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM ella_runtime_bindings WHERE user_id = (SELECT id FROM users WHERE omi_uid = $1)",
                    uid,
                )
                == 0
            )
        assert marker["proof_state"] == "unproven"
        assert marker["content_free"] is True
        assert str(marker["idempotency_key"])
        assert str(marker["correlation_ref"]).startswith("ella-ext-")

        destructive_calls = []
        for _attempt in range(2):
            response = await account_deletion_service.execute_account_deletion(
                uid,
                delete_firestore=lambda exact_uid: destructive_calls.append(("firestore", exact_uid)),
                delete_firebase=lambda exact_uid: destructive_calls.append(("firebase", exact_uid)),
            )
            assert response.status_code == 202
            assert response.body["status"] == "deletion_pending"
            assert response.body["deletion_receipt"]["external_cleanup_references"] == [marker["correlation_ref"]]
            assert "firebase" not in {kind for kind, _uid in destructive_calls}

        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE ella_provider_attempts
                SET proof_state = 'absence_proven',
                    proved_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE provisioning_job_id = $1
                """,
                job["id"],
            )
        completed = await account_deletion_service.execute_account_deletion(
            uid,
            delete_firestore=lambda exact_uid: destructive_calls.append(("firestore", exact_uid)),
            delete_firebase=lambda exact_uid: destructive_calls.append(("firebase", exact_uid)),
        )
        assert completed.status_code == 200
        assert destructive_calls[-1] == ("firebase", uid)

    asyncio.run(_run_with_database(scenario))


def test_deletion_tombstone_serializes_inflight_provider_writer_and_quarantines_photon_immediately():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "account-delete-writer-fence"
        email = "account-delete-writer-fence@example.test"
        await _prepare_deletion_account(
            pool,
            uid=uid,
            email=email,
            code="GHJK-ABCD",
            activate_runtime=True,
        )
        repository = EllaProvisioningRepository(pool)
        async with pool.acquire() as connection:
            user_id = await connection.fetchval("SELECT id FROM users WHERE omi_uid = $1", uid)
            await connection.execute(
                """
                UPDATE ella_runtime_bindings
                SET active = false, status = 'disabled', updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND active = true
                """,
                user_id,
            )
            photon_runtime_binding_id = await connection.fetchval(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, account_user_id, profile_user_id, role, provider,
                    profile_name, agent_id, runtime_instance_id, api_base_url_ref,
                    api_key_ref, honcho_workspace, observed_peer, observer_peer,
                    template_version, prompt_pack_version, prompt_artifact_receipt,
                    model_policy_version, voice_policy_version, expected_model,
                    health_state, status, active, runtime_target_mode,
                    target_endpoint_ref, target_credential_ref
                ) VALUES (
                    $1, $1, $1, 'user', 'hermes_cloud', $2, 'hermes-cloud', $3,
                    'env:ELLA_TEST_ENDPOINT', 'env:ELLA_TEST_KEY', $4, $5, $6,
                    'template-v1', 'prompt-v1', $7::jsonb, 'model-v1', 'voice-v1',
                    'gpt-5.6-terra', 'healthy', 'active', true,
                    'hermes-cloud-photon', 'env:ELLA_TEST_ENDPOINT', 'env:ELLA_TEST_KEY'
                ) RETURNING id
                """,
                user_id,
                f"photon-{uid}",
                f"runtime-{uid}",
                f"photon-workspace-{uid}",
                f"photon-observed-{uid}",
                f"photon-observer-{uid}",
                json.dumps(_prompt_receipt()),
            )
            photon_binding_id = await connection.fetchval(
                """
                INSERT INTO ella_photon_channel_bindings (
                    runtime_binding_id, user_id, status, line_identity_key,
                    contact_identity_key, policy_commit_sha, command_tier_version,
                    daily_message_limit, daily_initiation_limit
                ) VALUES ($1, $2, 'enabled', $3, $4, $5, 'tier-v1', 10, 2)
                RETURNING id
                """,
                photon_runtime_binding_id,
                user_id,
                "1" * 64,
                "2" * 64,
                "a" * 40,
            )
            job_id = await connection.fetchval(
                """
                INSERT INTO ella_provisioning_jobs (user_id, target_schema_version)
                VALUES ($1, 'queued-after-delete-v1') RETURNING id
                """,
                user_id,
            )
            claim_token = uuid.uuid4()
            await connection.execute(
                """
                UPDATE ella_runtime_bindings
                SET claim_job_id = $2, claim_token = $3
                WHERE id = $1
                """,
                photon_runtime_binding_id,
                job_id,
                claim_token,
            )
            await connection.execute(
                """
                CREATE FUNCTION delay_account_deletion_tombstone() RETURNS trigger AS $$
                BEGIN
                    IF NEW.status = 'DELETION_PENDING' THEN PERFORM pg_sleep(0.5); END IF;
                    RETURN NEW;
                END
                $$ LANGUAGE plpgsql
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER delay_account_deletion_tombstone
                BEFORE UPDATE ON users
                FOR EACH ROW EXECUTE FUNCTION delay_account_deletion_tombstone()
                """
            )

        positive = await repository.resolve_photon_channel_binding(
            line_identity_key="1" * 64,
            contact_identity_key="2" * 64,
        )
        assert positive and positive["omi_uid"] == uid

        deletion = asyncio.create_task(account_deletion.quarantine_account_for_deletion(uid))
        await asyncio.sleep(0.05)
        queued_stage = asyncio.create_task(
            repository.stage_runtime_binding(uid=uid, binding=_local_runtime_binding(uid))
        )
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
        await _wait_for_operator_authority_waiter(pool, owner)
        await deletion
        with pytest.raises(authority_advisory_lock.AuthorityLockError) as blocked_stage:
            await queued_stage
        assert blocked_stage.value.code == "authority_write_user_not_active"

        for _attempt in range(2):
            assert (
                await repository.resolve_photon_channel_binding(
                    line_identity_key="1" * 64,
                    contact_identity_key="2" * 64,
                )
                is None
            )

        with pytest.raises(managed_cloud_consent.ManagedCloudAuthorityUnavailable):
            await managed_cloud_consent.synchronize_grant(grant=_self_hosted_grant(uid))
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.activate_user(uid)
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.update_job(
                job_id=str(job_id),
                state="ready",
                stage="active",
                retryable=False,
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.acquire_job(
                uid=uid,
                target_schema_version="post-delete-job-v1",
                client_request_id=None,
                request_payload_hash="0" * 64,
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.claim_job(str(job_id))
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.begin_provider_attempt(uid=uid, job_id=str(job_id))
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.record_cloud_side_effect(
                uid=uid,
                job_id=str(job_id),
                claim_token=str(claim_token),
                effect={"kind": "synthetic"},
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.record_cloud_rollback(
                job_id=str(job_id),
                state="blocked",
                rollback_receipt={"content_free": True},
                error_code="synthetic",
                retryable=False,
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.record_photon_sidecar_preflight(
                photon_binding_id=str(photon_binding_id),
                connection_key="synthetic-connection",
                oauth_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                receipt={"content_free": True},
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await repository.ensure_user_identity(
                uid=uid,
                email=str(await pool.fetchval("SELECT email FROM users WHERE omi_uid = $1", uid)),
                name="Resurrected",
                timezone_name="UTC",
            )
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await voice_canary.upsert_entitlement(
                uid=uid,
                plan="resurrected",
                daily_limit_s=1,
                monthly_limit_s=1,
                max_session_s=1,
                max_concurrent=1,
                soft_limit_ratio=0.8,
                provider_allowlist=["hermes"],
                mode_allowlist=["hermes-chat"],
                fallback_policy={"enabled": False, "order": []},
                operator_note="synthetic",
            )

        async with pool.acquire() as connection:
            with pytest.raises(asyncpg.PostgresError, match="authority_write_user_not_active"):
                await connection.execute(
                    """
                    INSERT INTO ella_runtime_session_scopes (
                        binding_id, user_id, role, channel, session_key
                    ) VALUES ($1, $2, 'user', 'chat', $3)
                    """,
                    photon_runtime_binding_id,
                    user_id,
                    f"post-delete-{uid}",
                )
            with pytest.raises(asyncpg.PostgresError, match="authority_write_user_not_active"):
                await connection.execute(
                    """
                    INSERT INTO ella_runtime_ingestion_receipts (
                        binding_id, canonical_event_id, source_identity, provenance
                    ) VALUES ($1, 'post-delete-event', 'post-delete-source', 'synthetic')
                    """,
                    photon_runtime_binding_id,
                )
            with pytest.raises(asyncpg.PostgresError, match="authority_write_user_not_active"):
                await connection.execute(
                    """
                    INSERT INTO ella_photon_message_receipts (
                        photon_binding_id, inbound_provider_message_key,
                        inbound_payload_sha256, consent_grant_epoch
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    photon_binding_id,
                    "3" * 64,
                    "4" * 64,
                    "post-delete-grant-epoch",
                )

        class FirestoreDocument:
            exists = False
            writes = []

            def get(self):
                return self

            def set(self, payload, *, merge):
                self.writes.append((payload, merge))

        class Firestore:
            document_ref = FirestoreDocument()

            def collection(self, _name):
                return self

            def document(self, _uid):
                return self.document_ref

        fenced_firestore_repository = EllaProvisioningRepository(pool, firestore_db=Firestore())
        with pytest.raises(authority_advisory_lock.AuthorityLockError):
            await fenced_firestore_repository.ensure_omi_user_document(
                uid=uid,
                email=email,
                name="Resurrected",
                timezone_name="UTC",
            )
        assert FirestoreDocument.writes == []

        async with pool.acquire() as connection:
            state = dict(
                await connection.fetchrow(
                    """
                        SELECT u.status, u.identities, entitlement.status AS entitlement_status,
                               consent.decision AS consent_decision,
                               photon.status AS photon_status, binding.active,
                               job.state AS job_state,
                               (SELECT array_agg(target_status ORDER BY target_status)
                                FROM (
                                    SELECT DISTINCT target.status AS target_status
                                    FROM ella_runtime_targets target
                                    WHERE target.account_user_id = u.id
                                       OR target.profile_user_id = u.id
                                ) statuses) AS target_statuses,
                               (SELECT COUNT(*)
                                FROM ella_runtime_bindings all_bindings
                                WHERE all_bindings.user_id = u.id) AS binding_count
                        FROM users u
                        JOIN voice_entitlements entitlement ON entitlement.uid = u.omi_uid
                        JOIN ella_managed_cloud_consent_authority consent ON consent.user_id = u.id
                    JOIN ella_photon_channel_bindings photon ON photon.user_id = u.id
                    JOIN ella_runtime_bindings binding ON binding.id = photon.runtime_binding_id
                    JOIN ella_provisioning_jobs job ON job.id = $2
                    WHERE u.id = $1
                    """,
                    user_id,
                    job_id,
                )
            )
        assert state == {
            "status": "DELETION_PENDING",
            "identities": "{}",
            "entitlement_status": "revoked",
            "consent_decision": "revoked",
            "photon_status": "quarantined",
            "active": False,
            "job_state": "blocked",
            "target_statuses": ["revoked"],
            "binding_count": 2,
        }

    asyncio.run(_run_with_database(scenario))
