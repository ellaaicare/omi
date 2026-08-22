import asyncio
import ast
import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from ella.routers import canonical_events
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services import today_card_postgres
from ella.services.today_card import (
    DeterministicTodayCardRenderer,
    TodayCardAvoidance,
    TodayCardContent,
    TodayCardKind,
    TodayCardMaterializationClaim,
    TodayCardMaterializer,
    TodayCardRecord,
    TodayCardSourceRef,
    TodayCardState,
    TodayCardUserContext,
    deterministic_card_id,
    sha256_ref,
    today_card_source_pack,
)
from ella.services.today_card_postgres import PostgresTodayCardRepository
from utils.ella.canonical_omi import (
    TODAY_CARD_GROUNDING_ATTESTER,
    TODAY_CARD_GROUNDING_CONTRACT_VERSION,
    summary_grounding_hash,
    transcript_grounding_hash,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _attest_row(row):
    row.setdefault("uid", "uid-a")
    metadata = row["metadata"]
    source_version_id = row["source_ref"]["active_summary_version_id"]
    metadata["summary_versions"] = [
        {
            "id": source_version_id,
            "title": metadata["structured"].get("title") or "",
            "overview": metadata["structured"].get("overview") or "",
            "source": "hermes_cloud",
            "kind": "hermes_enriched",
        }
    ]
    transcript_segments = metadata.setdefault(
        "transcript_segments",
        [{"text": "We planted tomatoes together and planned another garden visit after lunch."}],
    )
    metadata.setdefault("today_card", {})["grounding"] = {
        "contract_version": TODAY_CARD_GROUNDING_CONTRACT_VERSION,
        "attester": TODAY_CARD_GROUNDING_ATTESTER,
        "semantic_outcome": "supported",
        "source_version_id": source_version_id,
        "transcript_hash": transcript_grounding_hash(transcript_segments),
        "summary_hash": summary_grounding_hash(metadata["structured"]),
        "supporting_quote_hashes": ["sha256:" + ("a" * 64)],
        "policy_version": "hermes-cloud-grounding-verifier-v1",
        "owner_hash": "sha256:" + hashlib.sha256(row["uid"].encode("utf-8")).hexdigest(),
        "conversation_id_hash": "sha256:"
        + hashlib.sha256(row["source_ref"]["conversation_id"].encode("utf-8")).hexdigest(),
        "runtime_interaction_id": "runtime-interaction-a",
        "canonical_assistant_event_id": "canonical-assistant-a",
        "verifier_runtime_interaction_id": "verifier-runtime-a",
        "verifier_canonical_assistant_event_id": "verifier-assistant-a",
    }
    return row


def _summary_row(version="summary-v2", *, privacy_scope="user_private", scan_policy="none", grounded=True):
    row = {
        "uid": "uid-a",
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
            "transcript_segments": [
                {"text": "We planted tomatoes together and planned another garden visit after lunch."}
            ],
            "today_card": {},
        },
    }
    return _attest_row(row) if grounded else row


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


class MaterializationRepository:
    def __init__(self, evidence):
        self.evidence = [evidence]
        self.current = None

    async def get_user_context(self, uid):
        return TodayCardUserContext(uid=uid, timezone="UTC", canonical_event_count=1)

    async def get_current(self, _uid, _local_date):
        return self.current

    async def claim_materialization(self, *, uid, local_date, timezone_name, now, force_regenerate):
        del force_regenerate
        self.current = TodayCardRecord(
            card_id=deterministic_card_id(uid, local_date),
            uid=uid,
            local_date=local_date,
            timezone=timezone_name,
            version=1,
            state=TodayCardState.preparing,
            updated_at=now,
        )
        return TodayCardMaterializationClaim(card=self.current, acquired=True)

    async def load_evidence(self, **_kwargs):
        return self.evidence

    async def load_avoidance(self, _uid, _local_date):
        return TodayCardAvoidance()

    async def sources_are_current(self, _card):
        return True

    async def save_materialized(self, card):
        self.current = card
        return card


def _materialize_evidence(evidence):
    return asyncio.run(
        TodayCardMaterializer(
            MaterializationRepository(evidence),
            clock=lambda: NOW,
        ).materialize("uid-a", date(2026, 8, 1))
    ).card


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
                _attest_row(
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
                            "transcript_segments": [
                                {"text": "A delayed but eligible source arrived after processing completed."}
                            ],
                            "today_card": {},
                        },
                    }
                )
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


def test_deleted_source_invalidation_fails_closed_before_migration_016(monkeypatch):
    async def tombstone_source(*_args, **_kwargs):
        raise asyncpg.UndefinedTableError("ella_today_card_source_tombstones is missing")

    monkeypatch.setattr(today_card_postgres.PostgresTodayCardRepository, "tombstone_source", tombstone_source)

    with pytest.raises(asyncpg.UndefinedTableError):
        asyncio.run(today_card_postgres.invalidate_deleted_conversation_source("uid-a", "conversation-a"))


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
    row = _summary_row(grounded=False)
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


@pytest.mark.parametrize(
    "summary",
    [
        "The recording was too short to provide a useful summary of what happened.",
        "The recording ended before enough speech was captured to create a useful summary.",
        "There was too little audio to determine what the person meant.",
    ],
)
def test_enriched_adapter_rejects_alternative_insufficient_capture_commentary(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.transcript_word_count is None
    assert evidence.capture_duration_seconds is None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


@pytest.mark.parametrize(
    "summary",
    [
        "The audio did not contain enough information to create a summary.",
        "The clip was brief and could not support a useful summary.",
        "There was insufficient audio for a coherent recap.",
        "The recording lacked enough detail to summarize what happened.",
        "There were too few words in the clip to tell what happened.",
        "The recording ended prematurely and could not support a useful summary.",
        "The audio was inaudible and could not support a useful summary.",
        "Almost no speech was captured to summarize what happened.",
    ],
)
def test_enriched_adapter_rejects_structural_insufficiency_commentary(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.transcript_word_count is None
    assert evidence.capture_duration_seconds is None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


def test_enriched_adapter_rejects_terse_summary_despite_substantive_title_and_provenance():
    row = _summary_row()
    row["metadata"]["structured"] = {
        "title": "Alex and Priya planted tomatoes together",
        "overview": "Garden",
    }
    _attest_row(row)

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


@pytest.mark.parametrize(
    "summary",
    [
        "The audio was too short for the story, so we discussed what it meant.",
        "The recording was too short, yet it was useful for understanding the song.",
        "They discussed a recording and agreed there was not enough evidence to understand the decision.",
    ],
)
def test_enriched_adapter_keeps_meaningful_discussion_of_recordings(summary):
    row = _summary_row()
    row["metadata"]["structured"] = {
        "title": "A thoughtful conversation",
        "overview": summary,
    }
    _attest_row(row)

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.transcript_word_count is None
    assert evidence.capture_duration_seconds is None
    assert evidence.meaningful is True
    assert evidence.confidence == 0.82
    assert today_card_postgres.evidence_is_safe(evidence) is True


@pytest.mark.parametrize(
    "summary",
    [
        (
            "The recording was too short to summarize the whole documentary, "
            "so everyone chose the missing scenes to film next."
        ),
        "The recording was too short to summarize the documentary but everyone chose the missing scenes to film next.",
        "The recording was too short to summarize the documentary: everyone chose the missing scenes to film next.",
        "The clip was brief and hard to summarize — we planned next week's episode anyway.",
        "The recording was too short to summarize the documentary, and everyone chose the missing scenes to film next.",
        "The recording was too short to summarize the documentary, or everyone could plan a second filming day.",
        (
            "Although the recording was too short to summarize the documentary, "
            "everyone planned a second filming day."
        ),
        "The recording was too short to summarize the documentary while everyone kept editing the final scene.",
        "The recording was too short to summarize the documentary\nEveryone planned a second filming day.",
        (
            "The recording was too short to summarize the documentary, but the hosts wrote a summary of Maria's "
            "neighborhood garden plan."
        ),
        (
            "The recording was too short to summarize the documentary, but Maria did not cancel the neighborhood "
            "screening."
        ),
        ("The recording was too short to summarize the podcast, but the hosts were not discouraged."),
        (
            "The recording was too short to summarize the documentary, but Maria continued without delaying the "
            "neighborhood screening."
        ),
    ],
)
def test_enriched_adapter_rejects_source_commentary_without_structured_content_provenance(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
        "transcript_word_count": 24,
        "duration_seconds": 18.0,
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


@pytest.mark.parametrize(
    "summary",
    [
        ("The recording lacked enough detail to summarize what happened. " "No useful context was available."),
        ("The clip was inaudible and could not support a useful summary. " "Nothing meaningful could be recovered."),
        "Almost no speech was captured to summarize what happened; no usable detail remained.",
        ("The recording lacked enough detail to summarize what happened. " "No coherent account could be produced."),
        "The recording was too short to summarize what happened. No one could tell what it meant.",
        ("The clip was inaudible and could not support a useful summary. " "There was nothing useful to work with."),
        ("The recording lacked enough detail to summarize what happened. " "No clear account could be produced."),
        (
            "The recording lacked enough detail to summarize what happened. "
            "No intelligible meaning could be recovered."
        ),
        (
            "Almost no speech was captured to summarize what happened. "
            "The remaining words did not form a coherent thought."
        ),
        "The audio was mostly silence and could not be summarized.",
        ("The recording was too short to summarize what happened. " "There was too little to work with."),
        ("The recording was too short to summarize what happened. " "Only silence remained."),
        ("The recording was too short to summarize what happened. " "What remained was unusable."),
        ("The recording was too short to summarize what happened. " "The fragment offered zero usable context."),
        ("The recording was too short to summarize what happened. " "The meaning remained unknown."),
        ("The recording was too short to summarize what happened. " "A coherent account was impossible."),
        ("The recording was too short to summarize what happened. " "The captured words were useless."),
        ("The recording was too short to summarize what happened. " "The rest was silence."),
        "The audio was entirely silent and could not be summarized.",
        "The clip contained no speech and could not be summarized.",
        "The recording was pure static and could not be summarized.",
        "The audio was dead quiet and impossible to summarize.",
        "The clip had zero words and could not be summarized.",
        "録音が短すぎて内容を要約できませんでした。",
        ("The recording was too short to summarize what happened. " "The fragment conveyed zilch."),
        ("The recording was too short to summarize what happened. " "Everything was lost."),
        ("The recording was too short to summarize what happened. " "It did not provide any new information."),
        ("The recording was too short to summarize what happened. " "We got zilch."),
        ("The recording was too short to summarize what happened. " "Everyone heard only static."),
        ("The recording was too short to summarize what happened. " "内容は不明でした。"),
        ("The recording was too short to summarize the documentary because everyone planned a second filming day."),
        ("Even though the recording was too short to summarize the documentary, Maria smiled during the visit."),
    ],
)
def test_enriched_adapter_rejects_multisentence_insufficiency_commentary(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
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


@pytest.mark.parametrize(
    ("quality_key", "quality_value"),
    [
        ("transcript_word_count", 24),
        ("duration_seconds", 18.0),
    ],
)
def test_partial_explicit_quality_cannot_override_source_commentary(quality_key, quality_value):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": "The recording was too short to summarize what happened.",
        quality_key: quality_value,
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


def test_complete_quality_metrics_cannot_override_pure_source_commentary():
    summary = "The recording was too short to summarize what happened. No useful context was available."
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
        "transcript_word_count": 24,
        "duration_seconds": 18.0,
    }
    _attest_row(row)

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False
    assert summary in evidence.summary


def test_fabricated_summary_without_independent_receipt_fails_closed_through_materialization():
    summary = "[Ella] You won the lottery and bought a red sailboat."
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A lottery win",
        "overview": summary,
        "transcript_word_count": 12,
        "duration_seconds": 18.0,
    }
    row["metadata"]["transcript_segments"] = [{"text": "We planted tomatoes together in the garden after lunch."}]

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    card = _materialize_evidence(evidence)

    assert evidence is not None
    assert evidence.meaningful is False
    assert today_card_postgres.evidence_is_safe(evidence) is False
    assert card.state == TodayCardState.degraded
    assert card.reason_code == "no_safe_source"
    assert card.content is None
    assert summary not in card.model_dump_json()


@pytest.mark.parametrize(
    "summary",
    [
        "La grabación era demasiado corta para resumir lo ocurrido.",
        "L’enregistrement était trop court pour résumer ce qui s’est passé.",
        "录音太短，无法总结发生了什么。",
        "كان التسجيل قصيرًا جدًا بحيث لا يمكن تلخيص ما حدث.",
        "Die Aufnahme war zu kurz, um zusammenzufassen, was passiert ist.",
    ],
)
def test_unproven_multilingual_commentary_fails_closed_through_materialization(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["today_card"]["meaningful"] = True
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
        "transcript_word_count": 24,
        "duration_seconds": 18.0,
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert today_card_postgres.evidence_is_safe(evidence) is False
    card = _materialize_evidence(evidence)
    serialized = card.model_dump_json()
    assert card.state == TodayCardState.degraded
    assert card.reason_code == "no_safe_source"
    assert card.content is None
    assert summary not in serialized


@pytest.mark.parametrize(
    "summary",
    [
        "La grabación era demasiado corta para resumir lo ocurrido.",
        "录音太短，无法总结发生了什么。",
    ],
)
def test_nominal_length_receipt_cannot_substitute_for_semantic_attestation(summary):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
        "transcript_word_count": 14,
        "duration_seconds": 18.0,
    }
    row["metadata"]["transcript_segments"] = [
        {"text": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"}
    ]
    row["metadata"]["today_card"]["grounding"] = {
        "contract_version": "ella.today_card.grounding.v1",
        "grounded_content": True,
        "source_version_id": "summary-v2",
        "transcript_hash": transcript_grounding_hash(row["metadata"]["transcript_segments"]),
        "transcript_word_count": 14,
        "capture_duration_seconds": 18.0,
    }

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    card = _materialize_evidence(evidence)

    assert evidence is not None
    assert evidence.meaningful is False
    assert card.state == TodayCardState.degraded
    assert card.content is None
    assert summary not in card.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        "summary",
        "transcript",
        "version",
        "conversation",
        "ghost_version",
        "empty_support",
        "fractional_support",
        "owner",
    ],
)
def test_semantic_receipt_is_bound_and_cannot_be_copied_or_coerced(mutation):
    row = _summary_row()
    grounding = row["metadata"]["today_card"]["grounding"]
    if mutation == "summary":
        row["metadata"]["structured"][
            "overview"
        ] = "A copied receipt now covers an unrelated lottery and sailboat story."
    elif mutation == "transcript":
        row["metadata"]["transcript_segments"][0]["text"] = "A different transcript replaced the attested source."
    elif mutation == "version":
        row["source_ref"]["active_summary_version_id"] = "summary-v3"
    elif mutation == "conversation":
        row["source_ref"]["conversation_id"] = "conversation-b"
    elif mutation == "ghost_version":
        row["metadata"]["summary_versions"] = []
    elif mutation == "empty_support":
        grounding["supporting_quote_hashes"] = []
    elif mutation == "fractional_support":
        grounding["supporting_quote_hashes"] = [12.1]
    elif mutation == "owner":
        grounding["owner_hash"] = "sha256:" + hashlib.sha256(b"uid-b").hexdigest()

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    card = _materialize_evidence(evidence)

    assert evidence is not None
    assert evidence.meaningful is False
    assert card.state == TodayCardState.degraded
    assert card.content is None


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        ("庭の思い出", "今日は母と庭でトマトを植えました。"),
        ("Una tarde tranquila", "Plantamos tomates juntos en el jardín."),
        ("زيارة عائلية", "تحدثنا وضحكنا معًا في الحديقة."),
    ],
)
def test_structurally_grounded_unicode_content_remains_eligible(title, summary):
    row = _summary_row()
    row["metadata"]["structured"] = {
        "title": title,
        "overview": summary,
        "transcript_word_count": 24,
        "duration_seconds": 18.0,
    }
    _attest_row(row)

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is True
    assert today_card_postgres.evidence_is_safe(evidence) is True
    card = _materialize_evidence(evidence)
    assert card.state == TodayCardState.ready
    assert card.content is not None
    assert card.content.body == summary
    assert summary in card.content.spoken_text


@pytest.mark.parametrize(
    "grounding",
    [
        None,
        {},
        {"contract_version": "ella.today_card.grounding.v0", "grounded_content": True},
        {
            "contract_version": today_card_postgres.TODAY_CARD_GROUNDING_CONTRACT_VERSION,
            "grounded_content": False,
            "source_version_id": "summary-v2",
        },
        {
            "contract_version": today_card_postgres.TODAY_CARD_GROUNDING_CONTRACT_VERSION,
            "grounded_content": True,
            "source_version_id": "wrong-version",
        },
    ],
)
def test_legacy_adapter_requires_exact_grounding_provenance(grounding):
    row = _summary_row(grounded=False)
    row["metadata"]["today_card"] = {"grounding": grounding}

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


@pytest.mark.parametrize("with_quality", [False, True])
def test_enriched_adapter_rejects_one_word_body_despite_generic_title(with_quality):
    row = _summary_row(grounded=False)
    structured = {
        "title": "A captured moment",
        "overview": "So",
    }
    if with_quality:
        structured.update({"transcript_word_count": 24, "duration_seconds": 18.0})
    row["metadata"]["structured"] = structured

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.meaningful is False
    assert evidence.confidence == 0.0
    assert today_card_postgres.evidence_is_safe(evidence) is False


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            "Everyone chose the missing documentary scenes,\nand planned a second filming day.",
            "Everyone chose the missing documentary scenes, and planned a second filming day.",
        ),
        (
            "Maria described the neighborhood garden —\neveryone planned another visit.",
            "Maria described the neighborhood garden — everyone planned another visit.",
        ),
    ],
)
def test_enriched_adapter_preserves_multiline_punctuation_in_rendered_output(summary, expected):
    row = _summary_row()
    row["metadata"]["structured"] = {
        "title": "A captured moment",
        "overview": summary,
        "transcript_word_count": 24,
        "duration_seconds": 18.0,
    }
    _attest_row(row)

    evidence = today_card_postgres._evidence_from_row(
        row,
        previous_day_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        previous_day_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert evidence is not None
    assert evidence.summary == expected
    assert today_card_postgres.evidence_is_safe(evidence) is True
    content = asyncio.run(
        DeterministicTodayCardRenderer().render(
            selected=evidence,
            local_date=date(2026, 8, 1),
            timezone_name="UTC",
            private_consolidation={},
        )
    )
    serialized = content.model_dump_json()
    assert content.body == expected
    assert content.spoken_text.endswith(expected)
    assert ",." not in serialized
    assert "—." not in serialized


@pytest.mark.parametrize(
    "title",
    [
        "Alex planted tomatoes today",
        "Morning tea with Margaret",
        "母と庭でトマトを植えた朝",
    ],
)
def test_enriched_adapter_rejects_low_value_summary_despite_substantive_title(title):
    row = _summary_row(grounded=False)
    row["metadata"]["structured"] = {
        "title": title,
        "overview": "The recording was too short to provide a useful summary.",
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
