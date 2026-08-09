import asyncio
import ast
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

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
    today_card_source_pack,
)
from ella.services.today_card_postgres import PostgresTodayCardRepository

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _summary_row(version="summary-v2", *, privacy_scope="user_private", scan_policy="none"):
    return {
        "event_id": "omi:conversation-a:summary",
        "text": "Grounded body with a useful remembered detail.",
        "started_at": datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        "privacy_scope": privacy_scope,
        "scan_policy": scan_policy,
        "source_ref": {
            "conversation_id": "conversation-a",
            "active_summary_version_id": version,
        },
        "metadata": {
            "adapter": "omi-enriched-conversation",
            "structured": {"title": "Grounded", "overview": "Grounded body with a useful remembered detail."},
        },
    }


def _card(version="summary-v2"):
    evidence = today_card_postgres._evidence_from_row(
        _summary_row(version),
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert evidence is not None
    source = evidence.source
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
            body="Grounded body with a useful remembered detail.",
            spoken_text="Grounded. Grounded body with a useful remembered detail.",
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
        if "FROM canonical_events" in query:
            return _summary_row(self.current_version)
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
            "source_watermark": today_card_source_pack([])[1],
            "reason_code": "no_safe_source",
        }
    )
    assert (
        asyncio.run(
            PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=matching_pool)).sources_are_current(degraded)
        )
        is True
    )


def test_sources_are_current_rejects_same_version_safety_metadata_change():
    class SafetyTightenedPool(Pool):
        async def fetchrow(self, query, *args):
            assert args == ("uid-a", "conversation-a")
            return _summary_row("summary-v2", privacy_scope="system_private")

    current = asyncio.run(
        PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=SafetyTightenedPool())).sources_are_current(_card())
    )

    assert current is False


def test_canonical_source_snapshot_normalizes_driver_json_strings():
    row = _summary_row()
    driver_row = {
        **row,
        "source_ref": canonical_events._stable_json(row["source_ref"]),
        "metadata": canonical_events._stable_json(row["metadata"]),
    }

    assert canonical_events._today_card_source_snapshot(driver_row) == canonical_events._today_card_source_snapshot(row)


def test_source_less_card_becomes_stale_when_delayed_canonical_evidence_arrives():
    class DelayedEvidencePool(Pool):
        async def fetch(self, query, *_args):
            assert "ella_today_card_source_tombstones" in query
            return [
                {
                    "event_id": "omi:conversation-late:summary",
                    "text": "A delayed but eligible source.",
                    "started_at": datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
                    "privacy_scope": "user_private",
                    "scan_policy": "none",
                    "source_ref": {
                        "conversation_id": "conversation-late",
                        "active_summary_version_id": "summary-v1",
                    },
                    "metadata": {
                        "adapter": "omi-enriched-conversation",
                        "structured": {"title": "Late source", "overview": "A delayed but eligible source."},
                    },
                }
            ]

    card = _card().model_copy(
        update={
            "state": TodayCardState.degraded,
            "kind": None,
            "content": None,
            "source_refs": [],
            "source_watermark": today_card_source_pack([])[1],
            "reason_code": "no_safe_source",
        }
    )
    current = asyncio.run(
        PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=DelayedEvidencePool())).sources_are_current(card)
    )

    assert current is False


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
    executions = []
    invalidation_calls = 0

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            nonlocal invalidation_calls
            executions.append((query, args))
            if "UPDATE ella_today_cards" in query:
                invalidation_calls += 1
                return "UPDATE 1" if invalidation_calls == 1 else "UPDATE 0"
            return "DELETE 1"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class DeletionPool:
        def acquire(self):
            return Acquire()

    pool = DeletionPool()
    monkeypatch.setattr(today_card_postgres, "get_ella_postgres_pool", lambda: asyncio.sleep(0, result=pool))

    count = asyncio.run(today_card_postgres.invalidate_deleted_conversation_source("uid-a", "conversation-a"))

    assert count == 1
    rendered = "\n".join(query for query, _args in executions)
    assert "INSERT INTO ella_today_card_source_tombstones" in rendered
    assert "DELETE FROM canonical_events" in rendered
    assert "DELETE FROM canonical_event_sessions" in rendered
    assert rendered.count("UPDATE ella_today_cards") == 2
    assert rendered.count("pg_advisory_xact_lock") == 2
    destructive_args = [args for query, args in executions if "INSERT INTO ella_today_card_source_tombstones" in query]
    assert destructive_args == [("uid-a", "conversation-a", "source_deleted")]
    invalidation_args = [args for query, args in executions if "UPDATE ella_today_cards" in query]
    assert all(args[0] == "uid-a" for args in invalidation_args)
    assert all('"source_id": "conversation-a"' in args[1] for args in invalidation_args)
    assert all(args[2] == "source_deleted" for args in invalidation_args)


def test_materialized_save_fails_closed_when_selected_source_is_tombstoned():
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
            assert "ella_today_card_source_tombstones" in query
            return None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class TombstonePool:
        def acquire(self):
            return Acquire()

    repository = PostgresTodayCardRepository(lambda: asyncio.sleep(0, result=TombstonePool()))

    with pytest.raises(RuntimeError, match="today_card_materialization_conflict"):
        asyncio.run(repository.save_materialized(_card()))


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

        async def execute(self, query, *_args):
            assert "pg_advisory_xact_lock" in query

        async def fetchrow(self, query, *args):
            if "SELECT text" in query:
                return _summary_row("summary-v1")
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

        async def execute(self, query, *_args):
            assert "pg_advisory_xact_lock" in query

        async def fetchrow(self, query, *_args):
            if "SELECT text" in query:
                return _summary_row("summary-v1")
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
    assert "ella_today_card_source_tombstones" in migration
    assert "PRIMARY KEY (uid, source_id)" in migration
    assert "'source_retracted'" in migration
    assert "ON DELETE CASCADE" in migration


def test_today_card_router_registration_has_no_function_local_imports():
    source = (Path(__file__).parents[2] / "ella" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    register = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_register_routers"
    )
    local_imports = [
        node
        for node in ast.walk(register)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and "today" in ast.unparse(node).lower()
    ]

    assert local_imports == []


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


def test_enriched_adapter_does_not_promote_missing_context_meta_summary():
    row = _summary_row()
    row["metadata"]["structured"] = {
        "title": "A very short moment",
        "overview": (
            "This tiny recording caught only the word So before it ended. "
            "There is not enough context to know what was being discussed."
        ),
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


def test_enriched_adapter_honors_explicit_capture_quality_metrics():
    row = _summary_row()
    row["metadata"]["source_quality"] = {
        "transcript_word_count": 4,
        "capture_duration_seconds": 3.5,
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.transcript_word_count == 4
    assert evidence.capture_duration_seconds == 3.5
    assert today_card_postgres.evidence_is_safe(evidence) is False


def test_capture_quality_metrics_ignore_nonfinite_values():
    assert today_card_postgres._first_nonnegative_number(float("nan"), float("inf"), 9) == 9
    assert today_card_postgres._first_nonnegative_number(float("-inf"), -1) is None
