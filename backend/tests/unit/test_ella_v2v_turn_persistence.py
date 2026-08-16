import asyncio
import json
import os
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path

import pytest
from fastapi import HTTPException

from ella.routers import canonical_events, chat
from ella.routers.canonical_events import CanonicalEventIn, InMemoryCanonicalEventStore, SessionCompleteIn
from utils.ella.canonical_context import (
    MAX_CANONICAL_EVENT_SEQUENCE,
    MAX_CANONICAL_TURN_ORDINAL,
    canonical_events_to_server_messages,
)


def _postgres_binary(name: str) -> str:
    direct = shutil.which(name)
    if direct:
        return direct
    for prefix in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        for candidate in sorted(prefix.glob(f"postgresql*/bin/{name}"), reverse=True):
            if candidate.is_file():
                return str(candidate)
    pytest.fail(f"real PostgreSQL test requires {name}")


async def _ensure_canonical_events_table(pool) -> None:
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS canonical_events (
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
            privacy_scope TEXT NOT NULL,
            scan_policy TEXT NOT NULL,
            source_ref JSONB NOT NULL,
            metadata JSONB NOT NULL,
            raw_event JSONB NOT NULL,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (event_id, source_identity)
        )
    """)


@pytest.fixture(scope="module")
def canonical_postgres(tmp_path_factory):
    service_host = os.environ.get("ELLA_TEST_POSTGRES_HOST")
    if service_host:
        yield {
            "host": service_host,
            "port": int(os.environ.get("ELLA_TEST_POSTGRES_PORT", "5432")),
            "user": os.environ.get("ELLA_TEST_POSTGRES_USER", "postgres"),
            "password": os.environ.get("ELLA_TEST_POSTGRES_PASSWORD", "postgres"),
            "database": os.environ.get("ELLA_TEST_POSTGRES_DATABASE", "postgres"),
        }
        return

    initdb = _postgres_binary("initdb")
    pg_ctl = _postgres_binary("pg_ctl")
    root = tmp_path_factory.mktemp("canonical-postgres")
    data_dir = root / "data"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]

    subprocess.run(
        [initdb, "-D", str(data_dir), "--auth=trust", "--username=postgres", "--no-locale"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-F -p {port} -k {socket_dir} -c listen_addresses=''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        yield {"host": str(socket_dir), "port": port, "user": "postgres", "database": "postgres"}
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-w", "stop", "-m", "fast"],
            check=True,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(root, ignore_errors=True)


def _canonical_event(event_id: str, metadata: dict, *, role: str = "user") -> CanonicalEventIn:
    return CanonicalEventIn(
        uid="uid-postgres-ordering",
        canonical_identity="uid-postgres-ordering",
        event_id=event_id,
        session_id="session-1",
        channel="ios_voice",
        provider="ordering-test",
        role=role,
        text=event_id,
        started_at=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
        source_ref={"source_identity": "ordering-test-source"},
        metadata=metadata,
    )


def test_ella_source_ci_checks_out_and_receipts_the_immutable_pr_head():
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ella-ios-source-ci.yml"
    source = workflow.read_text(encoding="utf-8")

    immutable_head_expression = (
        "github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha"
    )
    assert f"ref: ${{{{ {immutable_head_expression} }}}}" in source
    assert f"ELLA_EXPECTED_HEAD_SHA: ${{{{ {immutable_head_expression} }}}}" in source
    assert 'checked_out_head="$(git rev-parse HEAD)"' in source
    assert '[[ "$checked_out_head" == "$head_sha" ]]' in source
    assert "Ella source gate receipt: headSha=%s checkedOutHead=%s baseSha=%s" in source


class _ReadbackPool:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return []


def _request(**overrides):
    started_at = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    values = {
        "uid": "uid-a",
        "session_id": "session-1",
        "turn_id": "turn-000001",
        "user_event_id": "turn-000001:user",
        "assistant_event_id": "turn-000001:assistant",
        "user_transcript": "Durable user turn",
        "assistant_transcript": "Durable assistant turn",
        "user_terminal": True,
        "assistant_terminal": True,
        "started_at": started_at,
        "completed_at": started_at + timedelta(seconds=2),
    }
    values.update(overrides)
    return chat.EllaVoiceTurnRequest(**values)


def test_v2v_turn_write_is_idempotent_across_rebound_transport_sessions(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    first = asyncio.run(chat.persist_v2v_voice_turn(_request(session_id="session-jti-1"), authenticated_uid="uid-a"))
    replay = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(session_id="session-jti-2"),
            authenticated_uid="uid-a",
        )
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["session_id"] == "session-jti-2"
    assert {message["text"] for message in replay["messages"]} == {
        "Durable user turn",
        "Durable assistant turn",
    }
    assert len(store._events) == 2
    assert {source_identity for _, source_identity in store._events} == {"ios_voice:uid-a:turn-000001"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_transcript": "Conflicting retry"},
        {"assistant_transcript": "Conflicting reply"},
        {"started_at": datetime(2026, 8, 15, 20, 0, 1, tzinfo=timezone.utc)},
        {"completed_at": datetime(2026, 8, 15, 20, 0, 3, tzinfo=timezone.utc)},
    ],
)
def test_v2v_turn_replay_fails_closed_on_payload_or_timestamp_collision(monkeypatch, overrides):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    asyncio.run(chat.persist_v2v_voice_turn(_request(session_id="session-jti-1"), authenticated_uid="uid-a"))

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.persist_v2v_voice_turn(
                _request(session_id="session-jti-2", **overrides),
                authenticated_uid="uid-a",
            )
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "canonical_voice_turn_collision"
    assert len(store._events) == 2


def test_v2v_turn_replay_fails_closed_on_canonical_role_collision(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    asyncio.run(chat.persist_v2v_voice_turn(_request(session_id="session-jti-1"), authenticated_uid="uid-a"))
    user_key = ("turn-000001:user", "ios_voice:uid-a:turn-000001")
    store._events[user_key]["role"] = "assistant"

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chat.persist_v2v_voice_turn(_request(session_id="session-jti-2"), authenticated_uid="uid-a"))

    assert raised.value.status_code == 409
    assert raised.value.detail == "canonical_voice_turn_collision"
    assert len(store._events) == 2


def test_v2v_turn_returns_full_accepted_transcripts_without_history_truncation(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    long_user_text = "u" * 20000
    long_assistant_text = "a" * 20000

    result = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(user_transcript=long_user_text, assistant_transcript=long_assistant_text),
            authenticated_uid="uid-a",
        )
    )

    messages_by_sender = {message["sender"]: message["text"] for message in result["messages"]}
    assert messages_by_sender == {"human": long_user_text, "ai": long_assistant_text}


def test_v2v_turn_equal_timestamp_returns_user_before_assistant_with_stable_event_ids(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)

    result = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(started_at=timestamp, completed_at=timestamp),
            authenticated_uid="uid-a",
        )
    )

    assert [(message["id"], message["sender"], message["created_at"]) for message in result["messages"]] == [
        ("turn-000001:user", "human", timestamp.isoformat()),
        ("turn-000001:assistant", "ai", timestamp.isoformat()),
    ]


def test_v2v_turn_readback_uses_leading_event_id_index_with_exact_owner_binding(monkeypatch):
    pool = _ReadbackPool()

    async def get_pool():
        return pool

    monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
    store = canonical_events.PostgresCanonicalEventStore()
    event_ids = [
        "v2v-turn-00000000000000000000000000000001:user",
        "v2v-turn-00000000000000000000000000000001:assistant",
    ]

    result = asyncio.run(
        store.events_by_event_ids(
            uid="uid-a",
            source_identity="ios_voice:uid-a:turn-1",
            event_ids=event_ids,
        )
    )

    assert result == []
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "event_id = ANY($1::text[])" in query
    assert "source_identity = $2" in query
    assert "uid = $3" in query
    assert "lower(uid)" not in query
    assert args == (event_ids, "ios_voice:uid-a:turn-1", "uid-a")


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_transcript": ""},
        {"assistant_transcript": "  "},
        {"user_terminal": False},
        {"assistant_terminal": False},
        {"session_id": "cross/authority"},
        {"assistant_event_id": "turn-000001:user"},
    ],
)
def test_v2v_turn_rejects_partial_nonterminal_or_invalid_identity_without_writes(monkeypatch, overrides):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chat.persist_v2v_voice_turn(_request(**overrides), authenticated_uid="uid-a"))

    assert raised.value.status_code == 422
    assert store._events == {}


def test_v2v_turn_rejects_cross_authority_before_canonical_write(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chat.persist_v2v_voice_turn(_request(uid="uid-b"), authenticated_uid="uid-a"))

    assert raised.value.status_code == 403
    assert store._events == {}


def test_v2v_turn_survives_canonical_history_refresh(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    long_user_text = "u" * 20000
    long_assistant_text = "a" * 20000
    asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(user_transcript=long_user_text, assistant_transcript=long_assistant_text),
            authenticated_uid="uid-a",
        )
    )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50)

    assert len(refreshed) == 2
    assert {message["sender"] for message in refreshed} == {"human", "ai"}
    assert {message["text"] for message in refreshed} == {long_user_text, long_assistant_text}


def test_equal_timestamp_canonical_history_preserves_terminal_user_assistant_chronology(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(started_at=timestamp, completed_at=timestamp),
            authenticated_uid="uid-a",
        )
    )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50)

    assert [message["id"] for message in refreshed] == ["turn-000001:user", "turn-000001:assistant"]
    assert [message["sender"] for message in refreshed] == ["human", "ai"]


def test_multiple_equal_timestamp_turns_preserve_turn_pairs_across_store_and_serializer(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    for ordinal in (2, 1):
        turn_id = f"turn-{ordinal:06d}"
        asyncio.run(
            chat.persist_v2v_voice_turn(
                _request(
                    turn_id=turn_id,
                    user_event_id=f"{turn_id}:user",
                    assistant_event_id=f"{turn_id}:assistant",
                    started_at=timestamp,
                    completed_at=timestamp,
                ),
                authenticated_uid="uid-a",
            )
        )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50, newest_first=False)

    assert [message["id"] for message in refreshed] == [
        "turn-000001:user",
        "turn-000001:assistant",
        "turn-000002:user",
        "turn-000002:assistant",
    ]
    assert [message["metadata"]["event_sequence"] for message in refreshed] == [0, 1, 0, 1]
    assert {message["metadata"]["conversation_id"] for message in refreshed} == {"session-1"}


def test_equal_timestamp_reverse_lexical_v2v_ids_follow_persisted_turn_ordinal(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    chronological_turns = [
        "v2v-turn-ffffffffffffffffffffffffffffffff",
        "v2v-turn-00000000000000000000000000000000",
    ]
    for turn_ordinal, turn_id in enumerate(chronological_turns):
        asyncio.run(
            chat.persist_v2v_voice_turn(
                _request(
                    turn_id=turn_id,
                    user_event_id=f"{turn_id}:user",
                    assistant_event_id=f"{turn_id}:assistant",
                    started_at=timestamp,
                    completed_at=timestamp,
                    turn_ordinal=turn_ordinal,
                ),
                authenticated_uid="uid-a",
            )
        )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50, newest_first=False)

    assert [message["id"] for message in refreshed] == [
        f"{chronological_turns[0]}:user",
        f"{chronological_turns[0]}:assistant",
        f"{chronological_turns[1]}:user",
        f"{chronological_turns[1]}:assistant",
    ]
    assert [message["metadata"]["turn_ordinal"] for message in refreshed] == [0, 0, 1, 1]


def test_canonical_ingestion_rejects_invalid_ordering_metadata_atomically():
    invalid_values = [
        ("turn_ordinal", -1),
        ("turn_ordinal", "-1"),
        ("turn_ordinal", MAX_CANONICAL_TURN_ORDINAL + 1),
        ("turn_ordinal", str(MAX_CANONICAL_TURN_ORDINAL + 1)),
        ("turn_ordinal", "1.0"),
        ("turn_ordinal", True),
        ("event_sequence", -1),
        ("event_sequence", "-1"),
        ("event_sequence", MAX_CANONICAL_EVENT_SEQUENCE + 1),
        ("event_sequence", str(MAX_CANONICAL_EVENT_SEQUENCE + 1)),
        ("event_sequence", "1.0"),
        ("event_sequence", False),
    ]
    for field_name, invalid_value in invalid_values:
        store = InMemoryCanonicalEventStore()
        valid = _canonical_event("valid-before-invalid", {"turn_id": "valid", "event_sequence": "0"})
        invalid = _canonical_event("invalid", {"turn_id": "invalid", field_name: invalid_value})

        with pytest.raises(HTTPException) as raised:
            asyncio.run(store.write_batch([valid, invalid]))

        assert raised.value.status_code == 422
        assert raised.value.detail == f"invalid_{field_name}"
        assert store._events == {}

    session_store = InMemoryCanonicalEventStore()
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            session_store.complete_session(
                "session-1",
                SessionCompleteIn(metadata={"event_sequence": str(MAX_CANONICAL_EVENT_SEQUENCE + 1)}),
            )
        )
    assert raised.value.detail == "invalid_event_sequence"
    assert session_store._sessions == {}


def test_real_postgres_bounds_and_legacy_oversized_metadata_are_range_safe(monkeypatch, canonical_postgres):
    async def exercise_postgres():
        pool = await canonical_events.asyncpg.create_pool(min_size=1, max_size=2, **canonical_postgres)
        try:
            await _ensure_canonical_events_table(pool)

            async def get_pool():
                return pool

            monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
            store = canonical_events.PostgresCanonicalEventStore()
            for field_name in ("turn_ordinal", "event_sequence"):
                invalid_batch = [
                    _canonical_event("would-partially-write", {"turn_id": "valid", "event_sequence": 0}),
                    _canonical_event(
                        f"oversized-{field_name}-ingestion",
                        {"turn_id": "invalid", field_name: "9" * 10000},
                    ),
                ]
                with pytest.raises(HTTPException) as raised:
                    await store.write_batch(invalid_batch)
                assert raised.value.detail == f"invalid_{field_name}"
                assert await pool.fetchval("SELECT count(*) FROM canonical_events") == 0

            valid_events = [
                _canonical_event(
                    "valid-sequence-near",
                    {
                        "turn_id": "turn-sequence",
                        "turn_ordinal": str(MAX_CANONICAL_TURN_ORDINAL - 1),
                        "event_sequence": str(MAX_CANONICAL_EVENT_SEQUENCE - 1),
                    },
                ),
                _canonical_event(
                    "valid-sequence-max",
                    {
                        "turn_id": "turn-sequence",
                        "turn_ordinal": str(MAX_CANONICAL_TURN_ORDINAL - 1),
                        "event_sequence": str(MAX_CANONICAL_EVENT_SEQUENCE),
                    },
                ),
                _canonical_event(
                    "valid-ordinal-max",
                    {
                        "turn_id": "turn-ordinal",
                        "turn_ordinal": str(MAX_CANONICAL_TURN_ORDINAL),
                        "event_sequence": 0,
                    },
                ),
            ]
            result = await store.write_batch(valid_events)
            assert result["inserted"] == 3

            insert_legacy_sql = """
                INSERT INTO canonical_events (
                    uid, canonical_identity, event_id, source_identity,
                    session_id, channel, provider, role, text,
                    started_at, ended_at, privacy_scope, scan_policy,
                    source_ref, metadata, raw_event
                )
                VALUES (
                    $1, $1, $2, 'ordering-test-source',
                    'session-1', 'ios_voice', 'legacy-test', $3, $2,
                    $4, NULL, 'user_private', 'none',
                    '{}'::jsonb, $5::jsonb, '{}'::jsonb
                )
            """
            timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
            await pool.execute(
                insert_legacy_sql,
                "uid-postgres-ordering",
                "legacy-overflow-ordinal",
                "user",
                timestamp,
                json.dumps(
                    {
                        "turn_id": "legacy-a",
                        "turn_ordinal": str(MAX_CANONICAL_TURN_ORDINAL + 1),
                        "event_sequence": 0,
                    }
                ),
            )
            await pool.execute(
                insert_legacy_sql,
                "uid-postgres-ordering",
                "legacy-overflow-sequence",
                "assistant",
                timestamp,
                json.dumps({"turn_id": "legacy-b", "event_sequence": str(MAX_CANONICAL_EVENT_SEQUENCE + 1)}),
            )
            await pool.execute(
                insert_legacy_sql,
                "uid-postgres-ordering",
                "legacy-arbitrary-digits",
                "user",
                timestamp,
                json.dumps({"turn_id": "legacy-c", "turn_ordinal": "9" * 10000, "event_sequence": "9" * 10000}),
            )

            expected_order = [
                "valid-sequence-near",
                "valid-sequence-max",
                "valid-ordinal-max",
                "legacy-overflow-ordinal",
                "legacy-overflow-sequence",
                "legacy-arbitrary-digits",
            ]
            timeline = await store.timeline(uid="uid-postgres-ordering", since=None, limit=50, channels=None)
            assert [event["event_id"] for event in timeline] == expected_order
            assert timeline[1]["metadata"]["event_sequence"] == MAX_CANONICAL_EVENT_SEQUENCE
            assert timeline[2]["metadata"]["turn_ordinal"] == MAX_CANONICAL_TURN_ORDINAL
            assert timeline[1]["raw_event"]["metadata"]["event_sequence"] == MAX_CANONICAL_EVENT_SEQUENCE
            assert timeline[2]["raw_event"]["metadata"]["turn_ordinal"] == MAX_CANONICAL_TURN_ORDINAL

            readback = await store.events_by_event_ids(
                uid="uid-postgres-ordering",
                source_identity="ordering-test-source",
                event_ids=list(reversed(expected_order)),
            )
            assert [event["event_id"] for event in readback] == expected_order

            insert_limit_boundary_sql = """
                INSERT INTO canonical_events (
                    uid, canonical_identity, event_id, source_identity,
                    session_id, channel, provider, role, text,
                    started_at, ended_at, privacy_scope, scan_policy,
                    source_ref, metadata, raw_event
                )
                VALUES (
                    $1, $1, $2, 'legacy-limit-source',
                    NULL, 'ios_voice', 'legacy-test', 'user', $2,
                    $3, NULL, 'user_private', 'none',
                    $4::jsonb, $5::jsonb, '{}'::jsonb
                )
            """
            limit_uid = "uid-postgres-limit-boundary"
            await pool.executemany(
                insert_limit_boundary_sql,
                [
                    (
                        limit_uid,
                        "legacy-limit-a",
                        timestamp,
                        json.dumps({"turn_id": "legacy-a"}),
                        json.dumps({"turn_id": ""}),
                    ),
                    (
                        limit_uid,
                        "legacy-limit-b",
                        timestamp,
                        json.dumps({"client_message_id": "", "turn_id": "legacy-b"}),
                        json.dumps({"turn_id": ""}),
                    ),
                    (
                        limit_uid,
                        "legacy-limit-c",
                        timestamp,
                        json.dumps({}),
                        json.dumps({"turn_id": "legacy-c"}),
                    ),
                ],
            )

            limit_boundary = await store.timeline(uid=limit_uid, since=None, limit=2, channels=None)
            assert [event["event_id"] for event in limit_boundary] == ["legacy-limit-b", "legacy-limit-c"]
            exact_legacy_order = await store.events_by_event_ids(
                uid=limit_uid,
                source_identity="legacy-limit-source",
                event_ids=["legacy-limit-c", "legacy-limit-b", "legacy-limit-a"],
            )
            assert [event["event_id"] for event in exact_legacy_order] == [
                "legacy-limit-a",
                "legacy-limit-b",
                "legacy-limit-c",
            ]
        finally:
            await pool.close()

    asyncio.run(exercise_postgres())


def test_real_postgres_bounds_and_legacy_oversized_metadata_are_range_safe_for_composed_rebound_replay(
    monkeypatch, canonical_postgres
):
    async def exercise_postgres():
        pool = await canonical_events.asyncpg.create_pool(min_size=1, max_size=2, **canonical_postgres)
        uid = "uid-postgres-rebound-replay"
        turn_id = "v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        source_identity = f"ios_voice:{uid}:{turn_id}"

        def request(session_id: str, **overrides):
            return _request(
                uid=uid,
                session_id=session_id,
                turn_id=turn_id,
                user_event_id=f"{turn_id}:user",
                assistant_event_id=f"{turn_id}:assistant",
                **overrides,
            )

        try:
            await _ensure_canonical_events_table(pool)
            await pool.execute("DELETE FROM canonical_events WHERE uid = $1", uid)

            async def get_pool():
                return pool

            monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
            monkeypatch.setattr(chat, "_canonical_event_store", canonical_events.PostgresCanonicalEventStore())

            canonical_success_with_lost_ack = await chat.persist_v2v_voice_turn(
                request("session-jti-1"), authenticated_uid=uid
            )
            assert canonical_success_with_lost_ack["idempotent_replay"] is False

            # Both app and proxy may restart after canonical success but before the ACK arrives.
            monkeypatch.setattr(chat, "_canonical_event_store", canonical_events.PostgresCanonicalEventStore())
            replay_ack = await chat.persist_v2v_voice_turn(request("session-jti-2"), authenticated_uid=uid)

            assert replay_ack["ok"] is True
            assert replay_ack["session_id"] == "session-jti-2"
            assert replay_ack["turn_id"] == turn_id
            assert replay_ack["idempotent_replay"] is True
            assert {(message["id"], message["sender"]) for message in replay_ack["messages"]} == {
                (f"{turn_id}:user", "human"),
                (f"{turn_id}:assistant", "ai"),
            }
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM canonical_events WHERE uid = $1 AND source_identity = $2",
                    uid,
                    source_identity,
                )
                == 2
            )

            for collision in (
                request("session-jti-2", user_transcript="payload collision"),
                request(
                    "session-jti-2",
                    completed_at=datetime(2026, 8, 15, 20, 0, 3, tzinfo=timezone.utc),
                ),
            ):
                with pytest.raises(HTTPException) as raised:
                    await chat.persist_v2v_voice_turn(collision, authenticated_uid=uid)
                assert raised.value.status_code == 409
                assert raised.value.detail == "canonical_voice_turn_collision"

            await pool.execute(
                "UPDATE canonical_events SET role = 'assistant' WHERE uid = $1 AND event_id = $2",
                uid,
                f"{turn_id}:user",
            )
            with pytest.raises(HTTPException) as raised:
                await chat.persist_v2v_voice_turn(request("session-jti-2"), authenticated_uid=uid)
            assert raised.value.status_code == 409
            assert raised.value.detail == "canonical_voice_turn_collision"
            assert await pool.fetchval("SELECT count(*) FROM canonical_events WHERE uid = $1", uid) == 2
        finally:
            await pool.execute("DELETE FROM canonical_events WHERE uid = $1", uid)
            await pool.close()

    asyncio.run(exercise_postgres())


def test_backend_server_message_order_preserves_one_microsecond_at_high_datetime_range():
    older = {
        "event_id": "z-older:user",
        "session_id": "session-high-range",
        "role": "user",
        "text": "older",
        "started_at": "2500-01-01T00:00:00.000000Z",
        "metadata": {"turn_id": "z-older", "event_sequence": 0},
    }
    newer = {
        "event_id": "a-newer:user",
        "session_id": "session-high-range",
        "role": "user",
        "text": "newer",
        "started_at": "2500-01-01T00:00:00.000001Z",
        "metadata": {"turn_id": "a-newer", "event_sequence": 0},
    }

    chronological = canonical_events_to_server_messages([newer, older], newest_first=False)
    newest_first = canonical_events_to_server_messages([older, newer], newest_first=True)

    assert [message["id"] for message in chronological] == ["z-older:user", "a-newer:user"]
    assert [message["id"] for message in newest_first] == ["a-newer:user", "z-older:user"]


def test_backend_mixed_legacy_total_key_matches_shared_ios_fixture_for_all_permutations():
    fixture_path = (
        Path(__file__).resolve().parents[3] / "app" / "test" / "fixtures" / "canonical_ordering_mixed_legacy.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    timestamp = datetime.fromisoformat(fixture["timestamp"].replace("Z", "+00:00"))
    records = [
        {
            "event_id": record["event_id"],
            "session_id": record.get("session_id"),
            "role": record["role"],
            "started_at": timestamp,
            "source_ref": {},
            "metadata": {
                "turn_id": record["turn_id"],
                "event_sequence": record["event_sequence"],
                **({"turn_ordinal": record["turn_ordinal"]} if "turn_ordinal" in record else {}),
            },
        }
        for record in fixture["records"]
    ]
    records_by_id = {record["event_id"]: record for record in records}
    pair = [records_by_id[event_id] for event_id in fixture["pair_input"]]
    assert [record["event_id"] for record in sorted(pair, key=canonical_events._canonical_event_order)] == fixture[
        "pair_expected"
    ]
    for permutation in permutations(records):
        ordered = sorted(permutation, key=canonical_events._canonical_event_order)
        assert [record["event_id"] for record in ordered] == fixture["expected_order"]
