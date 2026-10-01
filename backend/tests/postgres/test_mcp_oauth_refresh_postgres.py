import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from database.authority_advisory_lock import AuthorityOwner, acquire_authority_lock
from database.mcp_oauth_refresh import MCPRefreshRepository
from ella.services import mcp_oauth_refresh as refresh
from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPIdentityResolution,
    MCPProfileGrant,
    STATE_AUTHENTICATED_MAPPED,
    validate_mcp_session_token,
)

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="Disposable ELLA_TEST_POSTGRES_DSN required; never use a live DSN")
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "021_create_mcp_oauth_refresh_families.sql"


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def _fixture(case):
    schema = "mcp_refresh_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(TEST_DSN, server_settings={"search_path": schema}, min_size=1, max_size=5)
    try:
        await pool.execute("CREATE TABLE users(id UUID PRIMARY KEY)")
        await pool.execute(MIGRATION.read_text())
        account, profile = uuid.uuid4(), uuid.uuid4()
        await pool.execute("INSERT INTO users(id) VALUES($1),($2)", account, profile)
        authority = dict(
            account_user_id=str(account), profile_user_id=str(profile), client_id="client", scopes=["tools:read"]
        )
        repository = MCPRefreshRepository(pool)
        row = await repository.create_family(authority, "a" * 64, _digest("old synthetic secret"))
        await case(pool, repository, row)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_refresh_rotation_is_single_use_and_hash_only():
    async def case(pool, repository, row):
        rotated = await repository.rotate(
            _digest("old synthetic secret"),
            _digest("new synthetic secret"),
            client_id="client",
            authority_digest="a" * 64,
        )
        assert rotated["id"] == row["id"]
        assert 6.99 < (row["expires_at"] - datetime.now(timezone.utc)).total_seconds() / 86400 <= 7
        assert await pool.fetchval("SELECT count(*) FROM ella_mcp_refresh_tokens") == 2
        assert await pool.fetchval(
            "SELECT consumed_at IS NOT NULL FROM ella_mcp_refresh_tokens WHERE digest=$1",
            _digest("old synthetic secret"),
        )
        assert not await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ella_mcp_refresh_tokens WHERE digest=$1)", "old synthetic secret"
        )

    asyncio.run(_fixture(case))


def test_consumed_refresh_replay_commits_family_revocation():
    async def case(pool, repository, row):
        await repository.rotate(
            _digest("old synthetic secret"),
            _digest("new synthetic secret"),
            client_id="client",
            authority_digest="a" * 64,
        )
        with pytest.raises(ValueError, match="invalid_grant"):
            await repository.rotate(
                _digest("old synthetic secret"),
                _digest("second synthetic secret"),
                client_id="client",
                authority_digest="a" * 64,
            )
        assert not (await repository.read(str(row["id"])))["usable"]
        assert await pool.fetchval("SELECT count(*) FROM ella_mcp_refresh_tokens") == 2

    asyncio.run(_fixture(case))


def test_two_connections_concurrent_rotation_one_commit_then_replay_burn():
    async def case(pool, repository, row):
        results = await asyncio.gather(
            *(
                repository.rotate(
                    _digest("old synthetic secret"),
                    _digest(f"new synthetic {index}"),
                    client_id="client",
                    authority_digest="a" * 64,
                )
                for index in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert not (await repository.read(str(row["id"])))["usable"]
        assert await pool.fetchval("SELECT count(*) FROM ella_mcp_refresh_tokens") == 2

    asyncio.run(_fixture(case))


def test_expiry_while_blocked_is_checked_after_shared_owner_lock():
    async def case(pool, repository, row):
        await pool.execute(
            "UPDATE ella_mcp_refresh_families SET expires_at=clock_timestamp()+interval '150 milliseconds' WHERE id=$1",
            row["id"],
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                await acquire_authority_lock(
                    connection, owner=AuthorityOwner.from_values(row["account_user_id"], row["profile_user_id"])
                )
                task = asyncio.create_task(
                    repository.rotate(
                        _digest("old synthetic secret"),
                        _digest("new synthetic secret"),
                        client_id="client",
                        authority_digest="a" * 64,
                    )
                )
                await asyncio.sleep(0.3)
                assert not task.done()
        with pytest.raises(ValueError, match="invalid_grant"):
            await task
        assert await pool.fetchval("SELECT count(*) FROM ella_mcp_refresh_tokens") == 1
        assert await pool.fetchval("SELECT consumed_at FROM ella_mcp_refresh_tokens") is None

    asyncio.run(_fixture(case))


def test_wrong_client_does_not_consume_or_burn_legitimate_family():
    async def case(pool, repository, row):
        with pytest.raises(ValueError, match="invalid_grant"):
            await repository.rotate(
                _digest("old synthetic secret"),
                _digest("new synthetic secret"),
                client_id="other",
                authority_digest="a" * 64,
            )
        assert (await repository.read(str(row["id"])))["usable"]
        assert await pool.fetchval("SELECT consumed_at FROM ella_mcp_refresh_tokens") is None

    asyncio.run(_fixture(case))


def test_revocation_serializes_with_pending_renewal():
    async def case(pool, repository, row):
        await repository.revoke(str(row["id"]))
        with pytest.raises(ValueError, match="invalid_grant"):
            await repository.rotate(
                _digest("old synthetic secret"),
                _digest("new synthetic secret"),
                client_id="client",
                authority_digest="a" * 64,
            )
        assert not (await repository.read(str(row["id"])))["usable"]

    asyncio.run(_fixture(case))


def test_delete_recreate_same_owner_cannot_restore_old_family():
    async def case(pool, repository, row):
        await pool.execute("DELETE FROM users WHERE id=$1", row["account_user_id"])
        await pool.execute("INSERT INTO users(id) VALUES($1)", row["account_user_id"])
        assert await repository.read(str(row["id"])) is None
        with pytest.raises(ValueError, match="invalid_grant"):
            await repository.rotate(
                _digest("old synthetic secret"),
                _digest("new synthetic secret"),
                client_id="client",
                authority_digest="a" * 64,
            )

    asyncio.run(_fixture(case))


def test_migration_missing_owner_schema_rolls_back_both_tables():
    async def case():
        schema = "mcp_refresh_missing_" + uuid.uuid4().hex
        connection = await asyncpg.connect(TEST_DSN)
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        try:
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(MIGRATION.read_text())
            await connection.execute("ROLLBACK")
            assert await connection.fetchval("SELECT to_regclass('ella_mcp_refresh_families')") is None
            assert await connection.fetchval("SELECT to_regclass('ella_mcp_refresh_tokens')") is None
        finally:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await connection.close()

    asyncio.run(case())


def test_real_repository_composed_with_issuer_and_renewable_access_replay_checks(monkeypatch):
    monkeypatch.setenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "true")
    monkeypatch.setenv("ELLA_MCP_SESSION_SECRET", "synthetic-postgres-refresh-signing-secret")
    monkeypatch.setitem(
        refresh.registered_clients,
        "client",
        dict(
            client_id="client",
            redirect_uris=["https://client.test/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
    )

    async def case(pool, repository, row):
        resolution = MCPIdentityResolution(
            state=STATE_AUTHENTICATED_MAPPED,
            trace_id="test",
            identity=ExternalConnectorIdentity("google", "subject"),
            selected_grant=MCPProfileGrant(
                "grant",
                "owner",
                scopes=["tools:read", "memory:read"],
                allowed_tools=["companion_get_conversation_summary"],
            ),
        )
        material = dict(
            row["authority"],
            provider="google",
            subject="subject",
            profile_uid="owner",
            grant_id="grant",
            scopes=["memory:read", "tools:read"],
            tools=["companion_get_conversation_summary"],
        )

        async def capture(**kwargs):
            assert kwargs["uid"] == "owner"
            return material, resolution

        async def create():
            return repository

        monkeypatch.setattr(refresh, "capture_authority", capture)
        monkeypatch.setattr(refresh.MCPRefreshRepository, "create", create)
        issued = await refresh.issue_refresh_family(
            resolution, client_id="client", scope="tools:read memory:read offline_access", ttl_seconds=3600
        )
        first_claims = validate_mcp_session_token(issued["access_token"])
        assert (await repository.read(first_claims["refresh_family_id"]))["usable"]
        renewed = await refresh.renew_refresh_family(
            token=issued["refresh_token"], client_id="client", scope="", ttl_seconds=3600
        )
        second_claims = validate_mcp_session_token(renewed["access_token"])
        await refresh.validate_renewable_session(second_claims)
        with pytest.raises(ValueError):
            await refresh.renew_refresh_family(
                token=issued["refresh_token"], client_id="client", scope="", ttl_seconds=3600
            )
        for claims in (first_claims, second_claims):
            with pytest.raises(ValueError):
                await refresh.validate_renewable_session(claims)
        assert await pool.fetchval("SELECT count(*) FROM ella_mcp_refresh_tokens") == 3

    asyncio.run(_fixture(case))
