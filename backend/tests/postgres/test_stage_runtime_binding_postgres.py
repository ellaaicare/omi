"""Focused PostgreSQL coverage for EllaProvisioningRepository.stage_runtime_binding.

Executes the real stage_runtime_binding SQL against a schema rebuilt by
replaying the actual migrations (008-015; 012 adds runtime_target_mode,
account/profile user ids) over the minimal base bootstrap tables (users,
ella_provisioning_jobs, ella_runtime_bindings).

This pins the SQL shape itself so class-of-bug regressions fail loudly here:
- the #377 trailing-comma PostgresSyntaxError (COALESCE(a, b,) inside the
  ON CONFLICT DO UPDATE) died the whole fresh self-hosted provision path,
- the COALESCE no-clobber semantics (preserve an existing runtime_target_mode
  when the restaged binding does not pin one), and
- the hermes-chat default for a fresh self-hosted user lane (rev 1 = no patch).
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import asyncpg
import pytest

from database.authority_advisory_lock import AuthorityLockError
from database.ella_provisioning import EllaProvisioningRepository

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for stage_runtime_binding PostgreSQL tests",
)

# Minimal base bootstrap tables; the rest of the schema shape comes from the
# real migrations replayed below (see drift guards in _run_with_database).
BASE_SCHEMA = """
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
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

# Order/name list mirrors the proven replay in the invitation postgres suite.
MIGRATION_NAMES = (
    "008_create_voice_canary_controls.sql",
    "009_create_hermes_cloud_runtime_pool.sql",
    "010_add_cloud_profile_class.sql",
    "011_create_invitation_redemption.sql",
    "012_create_account_profile_runtime_targets.sql",
    "013_create_managed_cloud_consent_authority.sql",
    "014_add_synthetic_invitation_operator_audit.sql",
    "015_add_invitation_allowed_email_hash.sql",
)


def _local_binding(uid: str, *, port: int = 18701, mode: str | None = None) -> dict:
    profile_name = f"ella-{uid}"[:63]
    honcho_workspace = f"honcho-{uid}"[:63]
    observed_peer = f"observed-{uid}"[:63]
    observer_peer = f"observer-{uid}"[:63]
    binding = {
        "binding_id": str(uuid.uuid4()),
        "provider": "hermes",
        "agent_id": f"hermes-{uid}"[:63],
        "profile_name": profile_name,
        "workspace_root": f"/Users/ellaai/.hermes/profiles/{profile_name}/workspace",
        "internal_gateway_url": f"http://100.76.138.56:{port}",
        "gateway_port": port,
        "service_label": f"com.ella.hermes.{profile_name}"[:255],
        "credential_ref": "env:HERMES_API_SERVER_KEY",
        "honcho_workspace": honcho_workspace,
        "observed_peer": observed_peer,
        "observer_peer": observer_peer,
        "template_version": "hermes-user-v1",
        "model_policy_version": "self-hosted-pilot-v1",
        "voice_policy_version": "ella-voice-v1",
        "health_state": "healthy",
        "health_receipt": {"content_free": True, "smoke_passed": True},
    }
    if mode is not None:
        binding["runtime_target_mode"] = mode
    return binding


async def _run_with_database(
    scenario: Callable[[asyncpg.Pool], Awaitable[None]],
) -> None:
    schema = f"stage_binding_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=3,
        server_settings={"search_path": schema},
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(BASE_SCHEMA)
            for name in MIGRATION_NAMES:
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            # Drift guards: the SQL stage_runtime_binding executes must line up
            # with the migration-replayed schema, not a hand-maintained copy.
            assert await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'ella_runtime_bindings'
                      AND column_name = 'runtime_target_mode'
                )
                """)
            assert await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'ella_runtime_bindings'
                      AND column_name = 'account_user_id'
                )
                """)
        await scenario(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _ensure_user(pool: asyncpg.Pool, uid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (omi_uid)
            VALUES ($1)
            ON CONFLICT (omi_uid) DO NOTHING
            """,
            uid,
        )


async def _binding_row(pool: asyncpg.Pool, uid: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT b.revision,
                   b.runtime_target_mode,
                   b.active,
                   b.provider,
                   b.role,
                   b.health_state,
                   b.health_receipt
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE u.omi_uid = $1
            """,
            uid,
        )
    assert row is not None, f"no binding row for {uid}"
    return dict(row)


def test_stage_runtime_binding_fresh_self_hosted_defaults_to_hermes_chat_rev1():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "fresh-self-hosted-user"
        await _ensure_user(pool, uid)
        repository = EllaProvisioningRepository(pool)
        # Fresh user path: no runtime_target_mode on the binding -> must default
        # to hermes-chat with rev 1 (single insert, no conflict patch).
        staged = await repository.stage_runtime_binding(
            uid=uid,
            binding=_local_binding(uid, port=18701),
        )
        assert staged["revision"] == 1
        assert staged["runtime_target_mode"] == "hermes-chat"
        assert staged["active"] is False
        assert staged["provider"] == "hermes"
        assert staged["role"] == "user"
        row = await _binding_row(pool, uid)
        assert row["revision"] == 1
        assert row["runtime_target_mode"] == "hermes-chat"
        assert row["health_state"] == "healthy"

    asyncio.run(_run_with_database(scenario))


def test_stage_runtime_binding_restage_bumps_revision_and_preserves_target_mode():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "restage-self-hosted-user"
        await _ensure_user(pool, uid)
        repository = EllaProvisioningRepository(pool)
        binding = _local_binding(uid, port=18702)
        first = await repository.stage_runtime_binding(uid=uid, binding=binding)
        assert first["revision"] == 1
        assert first["runtime_target_mode"] == "hermes-chat"

        # Coordinator/fix retries reuse the same binding payload; the mode must
        # survive the ON CONFLICT DO UPDATE via COALESCE instead of being reset.
        second = await repository.stage_runtime_binding(uid=uid, binding=binding)
        assert second["revision"] == 2
        assert second["runtime_target_mode"] == "hermes-chat"
        assert second["active"] is False
        assert str(second["id"]) == str(first["id"])

    asyncio.run(_run_with_database(scenario))


def test_stage_runtime_binding_respects_provider_pinned_target_mode():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "pinned-mode-user"
        await _ensure_user(pool, uid)
        repository = EllaProvisioningRepository(pool)
        # A provider-pinned voice lane must NOT be defaulted to hermes-chat.
        staged = await repository.stage_runtime_binding(
            uid=uid,
            binding=_local_binding(uid, port=18703, mode="hermes-voice"),
        )
        assert staged["runtime_target_mode"] == "hermes-voice"
        assert (await _binding_row(pool, uid))["runtime_target_mode"] == "hermes-voice"

    asyncio.run(_run_with_database(scenario))


def test_stage_runtime_binding_unknown_user_fails_closed_without_write():
    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "no-such-user"
        repository = EllaProvisioningRepository(pool)
        # Owner resolution happens before the INSERT, so an unknown uid fails
        # closed at the authority lock boundary with no binding row written.
        try:
            await repository.stage_runtime_binding(
                uid=uid,
                binding=_local_binding(uid, port=18704),
            )
        except AuthorityLockError as exc:
            assert str(exc) == "authority_lock_owner_missing"
        else:
            raise AssertionError("expected AuthorityLockError(authority_lock_owner_missing)")
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM ella_runtime_bindings b
                JOIN users u ON u.id = b.user_id
                WHERE u.omi_uid = $1
                """,
                uid,
            )
        assert count == 0

    asyncio.run(_run_with_database(scenario))


def test_seed_voice_entitlement_if_absent_creates_once_and_no_clobber():
    """Real create-if-absent seed INSERT (Sophia bar / note i): a fresh user gets
    an active grok-voice row with the proven canary quota shape, and a second
    call is a no-op that leaves the row/revision untouched (ON CONFLICT
    DO NOTHING - DB no-clobber pinned against real Postgres)."""

    async def scenario(pool: asyncpg.Pool) -> None:
        uid = "fresh-grok-voice-user"
        await _ensure_user(pool, uid)
        repository = EllaProvisioningRepository(pool)

        created = await repository.seed_voice_entitlement_if_absent(uid=uid)
        assert created is True

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT uid, status, plan, daily_limit_s, monthly_limit_s,
                       max_session_s, max_concurrent,
                       provider_allowlist, mode_allowlist, revision
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
            assert row is not None
        assert row["status"] == "active"
        assert row["plan"] == "canary"
        assert row["daily_limit_s"] == 2700
        assert row["monthly_limit_s"] == 43200
        assert row["max_session_s"] == 1200
        assert row["max_concurrent"] == 1
        assert row["provider_allowlist"] == ["grok-voice"]
        assert row["mode_allowlist"] == ["v4"]
        seeded_revision = row["revision"]

        created_again = await repository.seed_voice_entitlement_if_absent(uid=uid)
        assert created_again is False
        async with pool.acquire() as conn:
            revision = await conn.fetchval(
                "SELECT revision FROM voice_entitlements WHERE uid = $1",
                uid,
            )
        assert revision == seeded_revision  # no conflict bump, no clobber

    asyncio.run(_run_with_database(scenario))
