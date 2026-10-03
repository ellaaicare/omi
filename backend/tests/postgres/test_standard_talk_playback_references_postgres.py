import asyncio
import os
import uuid
from dataclasses import replace
from pathlib import Path

import asyncpg
import pytest

from database.standard_talk_playback_references import PlaybackAssociation, PostgresStandardTalkPlaybackReferenceStore

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION = Path(__file__).resolve().parents[2] / "migrations/020_create_standard_talk_playback_references.sql"
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="approved disposable ELLA_TEST_POSTGRES_DSN required")


def association():
    return PlaybackAssociation("owner-a", "event-a", "source-a", "a" * 64, "b" * 64, "c" * 64)


async def with_database(scenario):
    schema = "talk_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN, min_size=1, max_size=5, server_settings={"search_path": schema, "application_name": schema}
    )

    async def factory():
        return pool

    store = PostgresStandardTalkPlaybackReferenceStore(factory)
    try:
        async with pool.acquire() as conn:
            await conn.execute(MIGRATION.read_text())
            await conn.execute(MIGRATION.read_text())
        await scenario(store, pool, schema)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_two_pools_issue_and_claim_exactly_once_and_response_loss_never_renews():
    async def scenario(store, pool, schema):
        second = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2, server_settings={"search_path": schema})

        async def factory():
            return second

        other = PostgresStandardTalkPlaybackReferenceStore(factory)
        try:
            issued = await asyncio.gather(store.issue(association()), other.issue(association()))
            references = [item for item in issued if item is not None]
            assert len(references) == 1
            claims = await asyncio.gather(
                store.claim(references[0].reference, association()), other.claim(references[0].reference, association())
            )
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            assert await other.publish(winners[0], association())
            assert not await store.publish(winners[0], association())
            assert await store.claim(references[0].reference, association()) is None
            assert await store.issue(association()) is None
            assert await store.issue(replace(association(), canonical_text_sha256="d" * 64)) is None
        finally:
            await second.close()

    asyncio.run(with_database(scenario))


def test_database_clock_expiry_blocks_claim_and_late_publication():
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        row = await pool.fetchrow("SELECT issued_at, expires_at FROM ella_standard_talk_playback_references")
        assert (row["expires_at"] - row["issued_at"]).total_seconds() == 300
        await pool.execute(
            "UPDATE ella_standard_talk_playback_references SET issued_at = statement_timestamp() - interval '6 minutes', "
            "expires_at = statement_timestamp() - interval '1 minute'"
        )
        assert await store.claim(issued.reference, association()) is None
        assert await store.issue(association()) is None
        fresh = replace(association(), event_id="event-b")
        second = await store.issue(fresh)
        claim = await store.claim(second.reference, fresh)
        await pool.execute(
            "UPDATE ella_standard_talk_playback_references SET issued_at = statement_timestamp() - interval '6 minutes', "
            "expires_at = statement_timestamp() - interval '1 minute' WHERE event_id = 'event-b'"
        )
        assert not await store.publish(claim, fresh)
        assert await store.fail(claim)
        assert await store.issue(fresh) is None

    asyncio.run(with_database(scenario))


@pytest.mark.parametrize(
    "field",
    [
        "uid",
        "event_id",
        "source_identity",
        "canonical_text_sha256",
        "runtime_authority_sha256",
        "consent_receipt_sha256",
    ],
)
def test_exact_coordinate_and_digest_mismatch_cannot_claim_or_publish(field):
    async def scenario(store, pool, schema):
        original = association()
        current = replace(original, **{field: "d" * 64 if field.endswith("sha256") else "other"})
        issued = await store.issue(original)
        assert await store.claim(issued.reference, current) is None
        claim = await store.claim(issued.reference, original)
        assert not await store.publish(claim, current)
        assert await store.publish(claim, original)

    asyncio.run(with_database(scenario))


def test_revoked_original_claim_cannot_publish_after_same_tuple_aba():
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        claim = await store.claim(issued.reference, association())
        assert await store.revoke(association()) == 1
        # Returning to the same observed tuple cannot revive a retired row.
        assert not await store.publish(claim, association())
        assert not await store.fail(claim)
        assert await store.claim(issued.reference, association()) is None
        assert await store.issue(association()) is None
        assert await store.revoke(association()) == 0

    asyncio.run(with_database(scenario))


def test_failed_claim_and_wrong_claim_never_produce_another_candidate():
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        claim = await store.claim(issued.reference, association())
        assert not await store.publish(replace(claim, claim_id="d" * 64), association())
        assert not await store.publish(replace(claim, playback_id="d" * 64), association())
        assert await store.fail(claim)
        assert not await store.publish(claim, association())
        assert await store.claim(issued.reference, association()) is None
        assert await store.issue(association()) is None

    asyncio.run(with_database(scenario))


def test_blocked_row_update_rechecks_database_expiry_after_lock_release():
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT 1 FROM ella_standard_talk_playback_references FOR UPDATE")
                task = asyncio.create_task(store.claim(issued.reference, association()))
                waiting = False
                for _ in range(100):
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE application_name = $1 AND wait_event_type = 'Lock')",
                        schema,
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting, "the actual competing PostgreSQL UPDATE must wait on the held row lock"
                assert not task.done()
                await conn.execute(
                    "UPDATE ella_standard_talk_playback_references SET issued_at = statement_timestamp() - interval '6 minutes', "
                    "expires_at = statement_timestamp() - interval '1 minute'"
                )
        assert await task is None

    asyncio.run(with_database(scenario))


@pytest.mark.parametrize("operation", ["claim", "publish"])
@pytest.mark.parametrize("supplied_connection", [False, True], ids=["store-transaction", "caller-transaction"])
def test_unchanged_locked_row_naturally_expires_before_transition(operation, supplied_connection):
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        claim = await store.claim(issued.reference, association()) if operation == "publish" else None
        await pool.execute(
            "UPDATE ella_standard_talk_playback_references "
            "SET issued_at = statement_timestamp() - interval '299 seconds', "
            "expires_at = statement_timestamp() + interval '1 second'"
        )
        original = dict(await pool.fetchrow("SELECT * FROM ella_standard_talk_playback_references"))

        async def transition(connection=None):
            if operation == "claim":
                return await store.claim(issued.reference, association(), connection=connection)
            return await store.publish(claim, association(), connection=connection)

        async def competing_transition():
            if not supplied_connection:
                return await transition()
            async with pool.acquire() as caller:
                async with caller.transaction():
                    return await transition(caller)

        async with pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute("SELECT 1 FROM ella_standard_talk_playback_references FOR UPDATE")
                task = asyncio.create_task(competing_transition())
                waiting = False
                for _ in range(100):
                    await blocker.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await blocker.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE application_name = $1 AND wait_event_type = 'Lock')",
                        schema,
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting, "the actual PostgreSQL transition must block on the unchanged row"
                assert not task.done()
                expired = False
                for _ in range(300):
                    expired = await blocker.fetchval(
                        "SELECT expires_at < clock_timestamp() FROM ella_standard_talk_playback_references"
                    )
                    if expired:
                        break
                    await asyncio.sleep(0.01)
                assert expired, "the deadline must elapse naturally before releasing the row lock"
                assert dict(await blocker.fetchrow("SELECT * FROM ella_standard_talk_playback_references")) == original
        result = await asyncio.wait_for(task, timeout=5)
        assert result is None if operation == "claim" else result is False
        assert dict(await pool.fetchrow("SELECT * FROM ella_standard_talk_playback_references")) == original

    asyncio.run(with_database(scenario))


def test_caller_transaction_rollback_preserves_atomic_publication_and_no_plaintext_columns():
    async def scenario(store, pool, schema):
        issued = await store.issue(association())
        claim = await store.claim(issued.reference, association())
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="synthetic rollback"):
                async with conn.transaction():
                    assert await store.publish(claim, association(), connection=conn)
                    raise RuntimeError("synthetic rollback")
        assert await store.publish(claim, association())
        row = dict(await pool.fetchrow("SELECT * FROM ella_standard_talk_playback_references"))
        assert issued.reference not in row.values()
        assert set(row) == {
            "reference_sha256",
            "uid",
            "event_id",
            "source_identity",
            "canonical_text_sha256",
            "runtime_authority_sha256",
            "consent_receipt_sha256",
            "normalization_version",
            "state",
            "issued_at",
            "expires_at",
            "claim_id",
            "playback_id",
            "claimed_at",
            "terminal_at",
        }
        public_privileges = await pool.fetchval(
            "SELECT count(*) FROM pg_class c "
            "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) acl "
            "WHERE c.oid = 'ella_standard_talk_playback_references'::regclass AND acl.grantee = 0"
        )
        assert public_privileges == 0

    asyncio.run(with_database(scenario))
