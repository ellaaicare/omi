import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

from ella.routers import canonical_events
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services import today_card_postgres
from ella.services.today_card import (
    TodayCardContent,
    TodayCardKind,
    TodayCardRecord,
    TodayCardSourceRef,
    TodayCardState,
    sha256_ref,
)
from ella.services.today_card_postgres import PostgresTodayCardRepository

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _card(version="summary-v2"):
    source = TodayCardSourceRef(
        source_type="conversation_summary",
        source_id="conversation-a",
        source_version_id=version,
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        evidence_hash=sha256_ref(version),
        conversation_id="conversation-a",
    )
    return TodayCardRecord(
        card_id="3aa05168-9d13-49ba-8137-cbcbac86855b",
        uid="uid-a",
        local_date=date(2026, 8, 1),
        timezone="UTC",
        version=2,
        state=TodayCardState.ready,
        kind=TodayCardKind.recap,
        content=TodayCardContent(
            eyebrow="A NOTE FROM YESTERDAY",
            headline="Grounded",
            body="Grounded body.",
            spoken_text="Grounded. Grounded body.",
            sentence_source_ids=["conversation-a"],
        ),
        source_refs=[source],
        evidence_hash=sha256_ref([source.model_dump(mode="json")]),
        generated_at=NOW,
        updated_at=NOW,
    )


class Pool:
    def __init__(self, *, current_version="summary-v2"):
        self.current_version = current_version
        self.executions = []

    async def fetchrow(self, query, *args):
        if "active_summary_version_id" in query:
            return {"active_summary_version_id": self.current_version}
        return None

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "UPDATE 1"

    async def fetch(self, *_args):
        return []


def test_sources_are_current_requires_exact_owner_conversation_and_active_version():
    matching_pool = Pool(current_version="summary-v2")
    stale_pool = Pool(current_version="summary-v3")

    matching = asyncio.run(
        PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=matching_pool)).sources_are_current(_card())
    )
    stale = asyncio.run(
        PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=stale_pool)).sources_are_current(_card())
    )

    assert matching is True
    assert stale is False

    degraded = _card().model_copy(
        update={
            "state": TodayCardState.degraded,
            "kind": None,
            "content": None,
            "source_refs": [],
            "reason_code": "no_safe_source",
        }
    )
    assert (
        asyncio.run(
            PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=matching_pool)).sources_are_current(degraded)
        )
        is True
    )


def test_fresh_preparing_row_is_not_reported_as_acquired():
    preparing = _card().model_copy(
        update={
            "state": TodayCardState.preparing,
            "kind": None,
            "content": None,
            "source_refs": [],
            "evidence_hash": None,
            "generated_at": None,
            "updated_at": NOW,
        }
    )
    row = preparing.model_dump(mode="python")
    row["state"] = preparing.state.value
    row["contract_version"] = preparing.contract_version
    row["render_contract_version"] = preparing.render_contract_version

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *_args):
            assert "pg_advisory_xact_lock" in query

        async def fetchrow(self, query, *_args):
            assert "FOR UPDATE" in query
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class ClaimPool:
        def acquire(self):
            return Acquire()

    repository = PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=ClaimPool()))

    claim = asyncio.run(
        repository.claim_materialization(
            uid="uid-a",
            local_date=date(2026, 8, 1),
            timezone_name="UTC",
            now=NOW,
            force_regenerate=True,
        )
    )

    assert claim.acquired is False
    assert claim.card.state == TodayCardState.preparing
    assert claim.card.version == preparing.version


def test_deleted_source_invalidation_is_exact_uid_and_source(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(today_card_postgres, "get_ella_postgres_pool", lambda: asyncio.sleep(0, result=pool))

    count = asyncio.run(today_card_postgres.invalidate_deleted_conversation_source("uid-a", "conversation-a"))

    assert count == 1
    query, args = pool.executions[0]
    assert "WHERE uid = $1" in query
    assert "source_refs @> $2::jsonb" in query
    assert args[0] == "uid-a"
    assert '"source_id": "conversation-a"' in args[1]
    assert args[2] == "source_deleted"


def test_canonical_summary_correction_invalidates_card_in_same_transaction(monkeypatch):
    calls = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *args):
            if "SELECT source_ref" in query:
                return {"active_summary_version_id": "summary-v1"}
            if "INSERT INTO canonical_events" in query:
                return {"inserted": False}
            raise AssertionError(query)

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class CanonicalPool:
        def acquire(self):
            return Acquire()

    class TodayRepository:
        async def invalidate_source_in_connection(self, conn, **kwargs):
            calls.append((conn, kwargs))
            return 1

    monkeypatch.setattr(canonical_events, "_get_pool", lambda: asyncio.sleep(0, result=CanonicalPool()))
    store = PostgresCanonicalEventStore(reinterpretation_repository=False, today_card_repository=TodayRepository())
    event = CanonicalEventIn(
        uid="uid-a",
        canonical_identity="uid-a",
        event_id="omi:conversation-a:summary",
        source_ref={
            "source_identity": "omi:conversation-a",
            "conversation_id": "conversation-a",
            "active_summary_version_id": "summary-v2",
        },
        channel="omi",
        provider="omi-backend",
        role="assistant",
        text="",
        started_at=NOW,
        metadata={"adapter": "omi-enriched-conversation", "structured": {}},
    )

    result = asyncio.run(store.write_batch([event]))

    assert result["updated"] == 1
    assert calls[0][1] == {
        "uid": "uid-a",
        "source_id": "conversation-a",
        "reason": "source_version_changed",
    }


def test_missing_today_card_table_rolls_back_savepoint_without_aborting_canonical_write(monkeypatch):
    transaction_exits = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, *_args):
            transaction_exits.append(exc_type)
            return False

    class Connection:
        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *_args):
            if "SELECT source_ref" in query:
                return {"active_summary_version_id": "summary-v1"}
            if "INSERT INTO canonical_events" in query:
                return {"inserted": False}
            raise AssertionError(query)

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class CanonicalPool:
        def acquire(self):
            return Acquire()

    class MissingTodayRepository:
        async def invalidate_source_in_connection(self, _conn, **_kwargs):
            raise canonical_events.asyncpg.UndefinedTableError("ella_today_cards is missing")

    monkeypatch.setattr(canonical_events, "_get_pool", lambda: asyncio.sleep(0, result=CanonicalPool()))
    store = PostgresCanonicalEventStore(
        reinterpretation_repository=False,
        today_card_repository=MissingTodayRepository(),
    )
    event = CanonicalEventIn(
        uid="uid-a",
        canonical_identity="uid-a",
        event_id="omi:conversation-a:summary",
        source_ref={
            "source_identity": "omi:conversation-a",
            "conversation_id": "conversation-a",
            "active_summary_version_id": "summary-v2",
        },
        channel="omi",
        provider="omi-backend",
        role="assistant",
        text="",
        started_at=NOW,
        metadata={"adapter": "omi-enriched-conversation", "structured": {}},
    )

    result = asyncio.run(store.write_batch([event]))

    assert result["updated"] == 1
    assert transaction_exits == [canonical_events.asyncpg.UndefinedTableError, None]


def test_migration_defines_versioned_authority_and_source_indexes():
    migrations = Path(__file__).parents[2] / "migrations"
    migration_path = migrations / "016_create_today_cards.sql"
    migration = migration_path.read_text(encoding="utf-8")
    numbered_migrations = sorted((*migrations.glob("[0-9][0-9][0-9]_*.py"), *migrations.glob("[0-9][0-9][0-9]_*.sql")))
    prefixes = [path.name.partition("_")[0] for path in numbered_migrations]

    assert len(prefixes) == len(set(prefixes))
    assert migration_path.name == "016_create_today_cards.sql"
    assert "UNIQUE (uid, local_date, contract_version)" in migration
    assert "ella_today_cards_ready_shape_check" in migration
    assert "ella_today_cards_new_user_shape_check" in migration
    assert "source_refs jsonb_path_ops" in migration
    assert "ella_today_card_feedback" in migration
    assert "ON DELETE CASCADE" in migration


def test_malformed_canonical_evidence_is_skipped_without_failing_materialization():
    class MalformedPool(Pool):
        async def fetch(self, *_args):
            return [
                {
                    "event_id": "bad-event",
                    "text": "Malformed evidence",
                    "started_at": NOW,
                    "privacy_scope": "x" * 80,
                    "scan_policy": "none",
                    "source_ref": {"conversation_id": "conversation-a", "active_summary_version_id": "v1"},
                    "metadata": {
                        "adapter": "omi-enriched-conversation",
                        "structured": {"title": "Title", "overview": "Overview"},
                    },
                }
            ]

    repository = PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=MalformedPool()))
    evidence = asyncio.run(
        repository.load_evidence(
            uid="uid-a",
            previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
            previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
            history_start=datetime(2025, 8, 1, tzinfo=timezone.utc),
        )
    )

    assert evidence == []


def test_internal_assessment_risk_and_string_booleans_fail_closed():
    class RiskPool(Pool):
        async def fetch(self, *_args):
            return [
                {
                    "event_id": "risky-interest",
                    "text": "An interest that must not be selected.",
                    "started_at": NOW,
                    "privacy_scope": "user_private",
                    "scan_policy": "none",
                    "source_ref": {},
                    "metadata": {
                        "today_card": {
                            "kind": "interest",
                            "source_id": "interest-a",
                            "title": "Interest",
                            "summary": "An interest that must not be selected.",
                            "confidence": 0.99,
                            "confirmed": "false",
                        },
                        "internal_assessment": {
                            "risk_level": "high",
                            "reason_codes": ["medical_fall_risk"],
                        },
                    },
                }
            ]

    repository = PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=RiskPool()))
    evidence = asyncio.run(
        repository.load_evidence(
            uid="uid-a",
            previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
            previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
            history_start=datetime(2025, 8, 1, tzinfo=timezone.utc),
        )
    )

    assert evidence[0].confirmed is False
    assert "safety" in evidence[0].tags
