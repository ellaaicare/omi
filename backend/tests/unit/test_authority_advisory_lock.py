import asyncio
import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from database.authority_advisory_lock import (
    AUTHORITY_LOCK_DOMAIN,
    CONTRACT_ARTIFACT_SHA256,
    AuthorityLockError,
    AuthorityLockProof,
    AuthorityOwner,
    acquire_authority_lock,
    authority_lock_digest,
    authority_lock_key,
    authority_lock_preimage,
    canonical_uuid_bytes,
    contract_artifact_path,
    load_verified_contract_artifact,
    require_self_owner_lock,
)


class UUIDStringifiable:
    def __str__(self):
        return "00000000-0000-4000-8000-000000000000"


def test_vendored_contract_artifact_has_exact_reviewed_bytes():
    artifact_path = contract_artifact_path()
    assert artifact_path == Path(__file__).resolve().parents[2] / "contracts" / "authority_advisory_lock_v1.json"
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == CONTRACT_ARTIFACT_SHA256
    artifact = load_verified_contract_artifact()
    assert artifact["version"] == "1"
    assert artifact["encoding"]["domain_ascii"] == AUTHORITY_LOCK_DOMAIN.decode("ascii")
    assert len(artifact["test_vectors"]) == 6


def test_all_six_cross_repo_vectors_match_exact_bytes_digest_and_signed_key():
    vectors = json.loads(contract_artifact_path().read_text(encoding="utf-8"))["test_vectors"]
    for vector in vectors:
        preimage = authority_lock_preimage(vector["account_id"], vector["profile_id"])
        assert len(preimage) == vector["preimage_length"]
        assert preimage.hex() == vector["preimage_hex"]
        assert authority_lock_digest(vector["account_id"], vector["profile_id"]).hex() == vector["sha256_hex"]
        assert authority_lock_key(vector["account_id"], vector["profile_id"]) == vector["advisory_key_int64"]


@pytest.mark.parametrize(
    ("value", "code"),
    (
        (None, "authority_lock_owner_missing"),
        ("", "authority_lock_owner_missing"),
        (b"00000000-0000-4000-8000-000000000000", "authority_lock_owner_not_text"),
        (uuid.UUID("00000000-0000-4000-8000-000000000000"), "authority_lock_owner_not_text"),
        (UUIDStringifiable(), "authority_lock_owner_not_text"),
        (123, "authority_lock_owner_not_text"),
        (["00000000-0000-4000-8000-000000000000"], "authority_lock_owner_not_text"),
        ("{00000000-0000-4000-8000-000000000000}", "authority_lock_owner_not_canonical_uuid"),
        ("urn:uuid:00000000-0000-4000-8000-000000000000", "authority_lock_owner_not_canonical_uuid"),
        ("00000000000040008000000000000000", "authority_lock_owner_not_canonical_uuid"),
        ("not-an-owner", "authority_lock_owner_not_canonical_uuid"),
    ),
)
def test_noncanonical_owner_forms_fail_closed_without_echoing_value(value, code):
    with pytest.raises(AuthorityLockError) as raised:
        canonical_uuid_bytes(value, field="account_id")
    assert raised.value.code == code
    if value not in (None, ""):
        assert str(value) not in str(raised.value)


@pytest.mark.parametrize(
    "value",
    (
        uuid.UUID("00000000-0000-4000-8000-000000000000"),
        UUIDStringifiable(),
        123,
        ["00000000-0000-4000-8000-000000000000"],
        b"00000000-0000-4000-8000-000000000000",
    ),
)
def test_key_contract_rejects_non_text_owners(value):
    canonical = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(AuthorityLockError, match="authority_lock_owner_not_text"):
        authority_lock_key(value, canonical)
    with pytest.raises(AuthorityLockError, match="authority_lock_owner_not_text"):
        authority_lock_key(canonical, value)


def test_authority_owner_accepts_only_trusted_database_uuid_values():
    account_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    profile_id = uuid.UUID("22222222-2222-4222-8222-222222222222")

    assert AuthorityOwner.from_values(account_id, profile_id) == AuthorityOwner(
        account_id=account_id,
        profile_id=profile_id,
    )
    with pytest.raises(AuthorityLockError, match="authority_lock_owner_not_database_uuid"):
        AuthorityOwner.from_values(str(account_id), profile_id)
    with pytest.raises(AuthorityLockError, match="authority_lock_owner_not_database_uuid"):
        AuthorityOwner.from_values(account_id, UUIDStringifiable())


def test_account_profile_order_is_part_of_key():
    account_id = "11111111-1111-4111-8111-111111111111"
    profile_id = "22222222-2222-4222-8222-222222222222"
    assert authority_lock_key(account_id, profile_id) != authority_lock_key(profile_id, account_id)


class Connection:
    def __init__(self, *, backend_pid=1234, transaction_id=5678, lock_held=True):
        self.backend_pid = backend_pid
        self.transaction_id = transaction_id
        self.lock_held = lock_held
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "SELECT 1"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return {
            "backend_pid": self.backend_pid,
            "transaction_id": self.transaction_id,
            "lock_held": self.lock_held,
        }


def test_self_owner_proof_rejects_missing_cross_owner_forged_and_copied_values():
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    owner = AuthorityOwner.from_values(owner_id, owner_id)
    connection = Connection()
    proof = asyncio.run(acquire_authority_lock(connection, owner=owner))

    asyncio.run(require_self_owner_lock(connection, proof, user_id=owner_id))
    with pytest.raises(AuthorityLockError, match="authority_lock_proof_missing"):
        asyncio.run(require_self_owner_lock(connection, None, user_id=owner_id))
    with pytest.raises(
        AuthorityLockError,
        match="authority_lock_proof_owner_mismatch",
    ):
        asyncio.run(require_self_owner_lock(connection, proof, user_id=other_id))
    with pytest.raises(AuthorityLockError, match="authority_lock_proof_construction_forbidden"):
        AuthorityLockProof()
    forged = object.__new__(AuthorityLockProof)
    with pytest.raises(AuthorityLockError, match="authority_lock_proof_forged"):
        asyncio.run(require_self_owner_lock(connection, forged, user_id=owner_id))
    with pytest.raises(AuthorityLockError, match="authority_lock_proof_copy_forbidden"):
        copy.copy(proof)


def test_acquisition_uses_one_signed_bigint_and_returns_owner_proof():
    account_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    profile_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    owner = AuthorityOwner.from_values(account_id, profile_id)
    connection = Connection()

    proof = asyncio.run(acquire_authority_lock(connection, owner=owner))

    expected_key = authority_lock_key(str(account_id), str(profile_id))
    assert type(proof) is AuthorityLockProof
    assert repr(proof) == "AuthorityLockProof(<opaque>)"
    assert connection.calls[0] == (
        "execute",
        "SELECT pg_advisory_xact_lock($1::bigint)",
        (expected_key,),
    )
    assert connection.calls[1][0] == "fetchrow"
    assert connection.calls[1][2] == (
        (expected_key & ((1 << 64) - 1)) >> 32,
        expected_key & 0xFFFFFFFF,
    )


def test_acquisition_fails_when_transaction_lock_is_not_observable():
    owner_id = uuid.uuid4()
    owner = AuthorityOwner.from_values(owner_id, owner_id)
    connection = Connection(lock_held=False)
    with pytest.raises(AuthorityLockError, match="authority_lock_not_held"):
        asyncio.run(
            acquire_authority_lock(
                connection,
                owner=owner,
            )
        )
