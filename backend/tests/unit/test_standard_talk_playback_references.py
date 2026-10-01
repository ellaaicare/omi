import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from database.standard_talk_playback_references import (
    NORMALIZATION_VERSION,
    PlaybackAssociation,
    PostgresStandardTalkPlaybackReferenceStore,
    reference_sha256,
)


def association():
    return PlaybackAssociation("owner-a", "event-a", "source-a", "a" * 64, "b" * 64, "c" * 64)


class Connection:
    def __init__(self):
        self.calls = []
        self.row = {"reference_sha256": "d" * 64}
        self.transaction_active = True
        self.transactions_started = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions_started += 1
        yield

    def is_in_transaction(self):
        return self.transaction_active

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return [self.row] if self.row else []


class Pool:
    def __init__(self):
        self.connection = Connection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def fixture():
    pool = Pool()

    async def factory():
        return pool

    return PostgresStandardTalkPlaybackReferenceStore(factory), pool.connection


@pytest.mark.parametrize("field", ["uid", "event_id", "source_identity"])
@pytest.mark.parametrize("value", ["", None, "x\x00y", "x" * 513])
def test_invalid_exact_coordinate_rejected(field, value):
    with pytest.raises(ValueError, match="association_invalid"):
        replace(association(), **{field: value})


@pytest.mark.parametrize("field", ["canonical_text_sha256", "runtime_authority_sha256", "consent_receipt_sha256"])
@pytest.mark.parametrize("value", ["", None, "sha256:" + "a" * 64, "A" * 64])
def test_invalid_digest_rejected(field, value):
    with pytest.raises(ValueError, match="digest_invalid"):
        replace(association(), **{field: value})


@pytest.mark.parametrize("reference", [None, "", "a" * 42, "a" * 44, "a" * 42 + "B", "a" * 42 + "/"])
def test_invalid_capability_rejected_before_database(reference):
    store, conn = fixture()
    with pytest.raises(ValueError, match="reference_invalid"):
        asyncio.run(store.claim(reference, association()))
    assert conn.calls == []


def test_issue_persists_only_hash_and_exact_association():
    store, conn = fixture()
    issued = asyncio.run(store.issue(association()))
    assert issued is not None
    assert len(issued.reference) == 43
    assert reference_sha256(issued.reference) == hashlib.sha256(issued.reference.encode("ascii")).hexdigest()
    query, args = conn.calls[0]
    assert args == (reference_sha256(issued.reference), *association().values())
    assert issued.reference not in repr(args)
    assert issued.reference not in repr(issued)
    assert "owner-a" not in repr(association())
    assert "ON CONFLICT DO NOTHING" in query
    conn.row = None
    assert asyncio.run(store.issue(association())) is None


def test_claim_has_server_ids_db_clock_and_no_plaintext_capability():
    store, conn = fixture()
    reference = secrets.token_urlsafe(32)
    claim = asyncio.run(store.claim(reference, association()))
    assert claim is not None
    assert len(claim.claim_id) == len(claim.playback_id) == 64
    lock_query, lock_args = conn.calls[0]
    assert "FOR UPDATE" in lock_query
    assert "expires_at" not in lock_query
    assert lock_args == (reference_sha256(reference), association().uid)
    assert conn.transactions_started == 1
    query, args = conn.calls[1]
    assert args == (reference_sha256(reference), *association().values(), claim.claim_id, claim.playback_id)
    assert reference not in repr(args)
    assert "state = 'issued' AND expires_at > clock_timestamp()" in query
    conn.row = None
    assert asyncio.run(store.claim(reference, association())) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", "owner-b"),
        ("event_id", "event-b"),
        ("source_identity", "source-b"),
        ("canonical_text_sha256", "d" * 64),
        ("runtime_authority_sha256", "e" * 64),
        ("consent_receipt_sha256", "f" * 64),
    ],
)
def test_publication_rejects_changed_original_association_without_write(field, value):
    store, conn = fixture()
    claim = asyncio.run(store.claim(secrets.token_urlsafe(32), association()))
    conn.calls.clear()
    assert not asyncio.run(store.publish(claim, replace(association(), **{field: value})))
    assert conn.calls == []


def test_terminal_transition_has_exact_claim_and_expiry_guard():
    store, conn = fixture()
    claim = asyncio.run(store.claim(secrets.token_urlsafe(32), association()))
    assert asyncio.run(store.publish(claim, association()))
    assert "FOR UPDATE" in conn.calls[-2][0]
    assert "expires_at" not in conn.calls[-2][0]
    query, args = conn.calls[-1]
    assert args == (
        claim.reference_sha256,
        *association().values(),
        claim.claim_id,
        claim.playback_id,
        "generated",
        True,
    )
    assert "state = 'synthesizing'" in query
    assert "expires_at > clock_timestamp()" in query
    assert asyncio.run(store.fail(claim))
    assert conn.calls[-1][1][-2:] == ("failed", False)
    conn.row = None
    assert not asyncio.run(store.publish(claim, association()))


def test_revoke_retires_exact_association_without_reissuing():
    store, conn = fixture()
    assert asyncio.run(store.revoke(association())) == 1
    query, args = conn.calls[-1]
    assert args == association().values()
    assert "state = 'revoked'" in query
    assert "state IN ('issued', 'synthesizing', 'generated')" in query


def test_supplied_connection_requires_transaction_and_never_uses_other_pool():
    async def forbidden_factory():
        raise AssertionError("must use the caller's locked transaction")

    store = PostgresStandardTalkPlaybackReferenceStore(forbidden_factory)
    conn = Connection()
    issued = asyncio.run(store.issue(association(), connection=conn))
    claim = asyncio.run(store.claim(issued.reference, association(), connection=conn))
    assert asyncio.run(store.publish(claim, association(), connection=conn))
    assert conn.transactions_started == 0
    conn.transaction_active = False
    with pytest.raises(ValueError, match="transaction_required"):
        asyncio.run(store.issue(association(), connection=conn))


def test_unknown_normalization_denied_and_shared_vectors_explicitly_proposed():
    with pytest.raises(ValueError, match="normalization_invalid"):
        replace(association(), normalization_version="unreviewed")
    path = Path(__file__).resolve().parents[2] / "ella/docs/standard_talk_spoken_vectors.json"
    payload = json.loads(path.read_text())
    assert payload["contract"] == NORMALIZATION_VERSION
    assert payload["proposal_only"] is True
    assert len(payload["vectors"]) == 9
    assert (
        next(item for item in payload["vectors"] if item["id"] == "astral_split_boundary")["error"] == "invalid_unicode"
    )
