import asyncio
import json
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from ella.services import today_card_postgres
from ella.routers import canonical_events
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services.today_card import DeterministicTodayCardRenderer, TodayCardMaterializer, today_card_source_pack
from ella.services.today_card_postgres import PostgresTodayCardRepository

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "016_create_today_cards.sql"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for Today-card PostgreSQL tests",
)

CANONICAL_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE canonical_events (
    id BIGSERIAL PRIMARY KEY,
    uid TEXT NOT NULL,
    canonical_identity TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    session_id TEXT,
    channel TEXT NOT NULL,
    provider TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    privacy_scope TEXT NOT NULL DEFAULT 'user_private',
    scan_policy TEXT NOT NULL DEFAULT 'none',
    source_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_event JSONB NOT NULL DEFAULT '{}'::jsonb,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, source_identity)
);
CREATE TABLE canonical_event_sessions (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    uid TEXT NOT NULL,
    source_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (session_id, source_identity)
);
"""


async def _pool_for_schema(schema: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )


async def _create_schema() -> tuple[asyncpg.Connection, asyncpg.Pool, str]:
    schema = f"today_card_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await _pool_for_schema(schema)
    async with pool.acquire() as conn:
        await conn.execute(CANONICAL_SCHEMA)
        migration = MIGRATION.read_text(encoding="utf-8")
        await conn.execute(migration)
        await conn.execute(migration)
    return admin, pool, schema


async def _drop_schema(admin: asyncpg.Connection, pool: asyncpg.Pool, schema: str) -> None:
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


async def _insert_summary(pool: asyncpg.Pool, *, uid: str, conversation_id: str) -> None:
    await pool.execute(
        """
        INSERT INTO canonical_events (
            uid, canonical_identity, event_id, source_identity, channel,
            provider, role, text, started_at, source_ref, metadata
        )
        VALUES ($1, $1, $2, $3, 'omi', 'omi-backend', 'assistant',
                'Eligible summary.', $4, $5::jsonb, $6::jsonb)
        ON CONFLICT (event_id, source_identity) DO UPDATE
        SET text = EXCLUDED.text, source_ref = EXCLUDED.source_ref, metadata = EXCLUDED.metadata
        """,
        uid,
        f"omi:{conversation_id}:summary",
        f"omi:{conversation_id}",
        datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        json.dumps({"conversation_id": conversation_id, "active_summary_version_id": "summary-v1"}),
        json.dumps(
            {
                "adapter": "omi-enriched-conversation",
                "structured": {"title": "Eligible source", "overview": "Eligible summary."},
            }
        ),
    )


async def _insert_explicit_source(
    pool: asyncpg.Pool,
    *,
    uid: str,
    source_id: str,
    source_version_id: str = "source-v1",
) -> None:
    await pool.execute(
        """
        INSERT INTO canonical_events (
            uid, canonical_identity, event_id, source_identity, channel,
            provider, role, text, started_at, source_ref, metadata
        )
        VALUES ($1, $1, $2, $3, 'hermes', 'hermes', 'assistant',
                'A useful observation from recent memory.', $4, '{}'::jsonb, $5::jsonb)
        ON CONFLICT (event_id, source_identity) DO UPDATE
        SET text = EXCLUDED.text, source_ref = EXCLUDED.source_ref, metadata = EXCLUDED.metadata
        """,
        uid,
        f"today:{source_id}:{source_version_id}",
        f"hermes:{source_id}:{source_version_id}",
        datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        json.dumps(
            {
                "today_card": {
                    "kind": "recap",
                    "source_id": source_id,
                    "source_version_id": source_version_id,
                    "title": "A useful observation",
                    "summary": "A useful observation from recent memory.",
                    "confidence": 0.95,
                    "meaningful": True,
                    "positive_or_neutral": True,
                }
            }
        ),
    )


def test_deleted_source_is_tombstoned_removed_and_excluded_after_stale_reinsert(monkeypatch):
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-delete-user"
            conversation_id = "conversation-deleted"
            await _insert_summary(pool, uid=uid, conversation_id=conversation_id)
            await pool.execute(
                """
                INSERT INTO canonical_event_sessions (session_id, source_identity, uid, source_ref)
                VALUES ('session-a', 'omi:conversation-deleted', $1, $2::jsonb)
                """,
                uid,
                json.dumps({"conversation_id": conversation_id}),
            )
            await pool.execute(
                """
                INSERT INTO ella_today_cards (
                    card_id, uid, local_date, timezone, contract_version, version,
                    state, source_refs, source_watermark, render_contract_version
                ) VALUES (gen_random_uuid(), $1, DATE '2026-08-01', 'UTC',
                          'ella.today_card.v1', 1, 'degraded', $2::jsonb,
                          $3, 'ella.today_card.render.v1')
                """,
                uid,
                json.dumps([{"source_id": conversation_id}]),
                today_card_source_pack([])[1],
            )

            async def get_pool():
                return pool

            monkeypatch.setattr(today_card_postgres, "get_ella_postgres_pool", get_pool)
            invalidated = await today_card_postgres.invalidate_deleted_conversation_source(uid, conversation_id)

            assert invalidated == 1
            assert await pool.fetchval("SELECT COUNT(*) FROM canonical_events WHERE uid = $1", uid) == 0
            assert await pool.fetchval("SELECT COUNT(*) FROM canonical_event_sessions WHERE uid = $1", uid) == 0
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM ella_today_card_source_tombstones WHERE uid = $1 AND source_id = $2",
                    uid,
                    conversation_id,
                )
                == 1
            )
            assert await pool.fetchval("SELECT invalidated_at IS NOT NULL FROM ella_today_cards WHERE uid = $1", uid)

            await _insert_summary(pool, uid=uid, conversation_id=conversation_id)
            repository = PostgresTodayCardRepository(get_pool)
            evidence = await repository.load_evidence(
                uid=uid,
                previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
                previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
                history_start=datetime(2025, 8, 1, tzinfo=timezone.utc),
            )
            assert evidence == []
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_source_less_card_watermark_detects_delayed_canonical_evidence():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-delayed-user"
            await pool.execute("INSERT INTO users (omi_uid) VALUES ($1)", uid)
            await pool.execute(
                """
                INSERT INTO ella_today_cards (
                    card_id, uid, local_date, timezone, contract_version, version,
                    state, source_refs, source_watermark, render_contract_version
                ) VALUES (gen_random_uuid(), $1, DATE '2026-08-01', 'UTC',
                          'ella.today_card.v1', 1, 'degraded', '[]'::jsonb,
                          $2, 'ella.today_card.render.v1')
                """,
                uid,
                today_card_source_pack([])[1],
            )

            async def get_pool():
                return pool

            repository = PostgresTodayCardRepository(get_pool)
            card = await repository.get_current(uid, date(2026, 8, 1))
            assert card is not None
            assert await repository.sources_are_current(card) is True

            await _insert_summary(pool, uid=uid, conversation_id="conversation-late")
            assert await repository.sources_are_current(card) is False
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_same_version_safety_tightening_invalidates_ready_source_fingerprint():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-safety-user"
            await pool.execute("INSERT INTO users (omi_uid) VALUES ($1)", uid)
            await _insert_summary(pool, uid=uid, conversation_id="conversation-safety")

            async def get_pool():
                return pool

            repository = PostgresTodayCardRepository(get_pool)
            materializer = TodayCardMaterializer(
                repository,
                clock=lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            )
            result = await materializer.materialize(uid, date(2026, 8, 1))
            assert result.card.state.value == "ready"
            assert result.card.source_refs[0].source_version_id == "summary-v1"
            assert await repository.sources_are_current(result.card) is True

            await pool.execute(
                """
                UPDATE canonical_events
                SET privacy_scope = 'system_private'
                WHERE uid = $1 AND source_ref ->> 'conversation_id' = 'conversation-safety'
                """,
                uid,
            )

            assert await repository.sources_are_current(result.card) is False
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_source_advance_during_materialization_cannot_publish_stale_card(monkeypatch):
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-race-user"
            conversation_id = "conversation-race"
            await pool.execute("INSERT INTO users (omi_uid) VALUES ($1)", uid)
            await _insert_summary(pool, uid=uid, conversation_id=conversation_id)

            async def get_pool():
                return pool

            monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
            store = PostgresCanonicalEventStore(
                reinterpretation_repository=False,
                today_card_repository=PostgresTodayCardRepository(get_pool),
            )

            class AdvancingRenderer(DeterministicTodayCardRenderer):
                async def render(self, **kwargs):
                    await store.write_batch(
                        [
                            CanonicalEventIn(
                                uid=uid,
                                canonical_identity=uid,
                                event_id=f"omi:{conversation_id}:summary",
                                source_ref={
                                    "source_identity": f"omi:{conversation_id}",
                                    "conversation_id": conversation_id,
                                    "active_summary_version_id": "summary-v2",
                                },
                                channel="omi",
                                provider="omi-backend",
                                role="assistant",
                                text="New canonical summary.",
                                started_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
                                metadata={
                                    "adapter": "omi-enriched-conversation",
                                    "structured": {
                                        "title": "New canonical source",
                                        "overview": "New canonical summary.",
                                    },
                                },
                            )
                        ]
                    )
                    return await super().render(**kwargs)

            materializer = TodayCardMaterializer(
                PostgresTodayCardRepository(get_pool),
                renderer=AdvancingRenderer(),
                clock=lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            )

            with pytest.raises(RuntimeError, match="today_card_materialization_conflict"):
                await materializer.materialize(uid, date(2026, 8, 1))

            row = await pool.fetchrow("SELECT state, source_refs FROM ella_today_cards WHERE uid = $1", uid)
            assert row is not None
            assert row["state"] == "preparing"
            assert json.loads(row["source_refs"]) == []
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_explicit_source_retraction_is_durable_across_stale_reinsert():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-explicit-retract-user"
            source_id = "hermes-note-a"
            await pool.execute("INSERT INTO users (omi_uid) VALUES ($1)", uid)
            await _insert_explicit_source(pool, uid=uid, source_id=source_id)

            async def get_pool():
                return pool

            repository = PostgresTodayCardRepository(get_pool)
            materializer = TodayCardMaterializer(
                repository,
                clock=lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            )
            result = await materializer.materialize(uid, date(2026, 8, 1))
            assert result.card.state.value == "ready"
            assert result.card.source_refs[0].source_id == source_id

            assert (
                await repository.tombstone_source(
                    uid=uid,
                    source_id=source_id,
                    reason="source_retracted",
                )
                == 1
            )
            assert await pool.fetchval("SELECT COUNT(*) FROM canonical_events WHERE uid = $1", uid) == 0

            await _insert_explicit_source(pool, uid=uid, source_id=source_id)
            evidence = await repository.load_evidence(
                uid=uid,
                previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
                previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
                history_start=datetime(2025, 8, 1, tzinfo=timezone.utc),
            )
            assert evidence == []
            assert await repository.sources_are_current(result.card) is False
            assert await pool.fetchval(
                """
                SELECT reason = 'source_retracted'
                FROM ella_today_card_source_tombstones
                WHERE uid = $1 AND source_id = $2
                """,
                uid,
                source_id,
            )
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_explicit_source_currentness_uses_only_latest_canonical_version():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "today-explicit-version-user"
            source_id = "hermes-note-versioned"
            await _insert_explicit_source(pool, uid=uid, source_id=source_id, source_version_id="source-v1")
            await _insert_explicit_source(pool, uid=uid, source_id=source_id, source_version_id="source-v2")

            async def get_pool():
                return pool

            repository = PostgresTodayCardRepository(get_pool)
            evidence = await repository.load_evidence(
                uid=uid,
                previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
                previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
                history_start=datetime(2025, 8, 1, tzinfo=timezone.utc),
            )

            assert len(evidence) == 1
            assert evidence[0].source.source_id == source_id
            assert evidence[0].source.source_version_id == "source-v2"
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())
