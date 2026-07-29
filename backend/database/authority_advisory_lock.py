"""Shared managed-cloud authority advisory-lock contract.

The vendored artifact is byte-pinned to ellaaicare/ella-ai#1155. This module is
the only OMI implementation of its UUID framing and signed PostgreSQL bigint
derivation. Authority writers must take this transaction-scoped lock before any
row lock or mutation for the same account/profile pair.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import asyncpg

CONTRACT_VERSION = "1"
CONTRACT_ARTIFACT_NAME = "authority_advisory_lock_v1.json"
CONTRACT_ARTIFACT_SHA256 = "1a92c0d335742560fff71b4630b95e1424bccdafb15c6245c8a554f4ea19eb69"
AUTHORITY_LOCK_DOMAIN = b"ella-managed-cloud-authority-lock-v1"
AUTHORITY_LOCK_FIELD_COUNT = 2
UUID_BYTE_LENGTH = 16

_CANONICAL_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class AuthorityLockError(RuntimeError):
    """The cross-service lock contract could not be satisfied."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class AuthorityOwner:
    account_id: uuid.UUID
    profile_id: uuid.UUID

    def __post_init__(self) -> None:
        _validated_database_uuid(self.account_id, field="account_id")
        _validated_database_uuid(self.profile_id, field="profile_id")

    @classmethod
    def from_values(cls, account_id: Any, profile_id: Any) -> "AuthorityOwner":
        return cls(
            account_id=_validated_database_uuid(account_id, field="account_id"),
            profile_id=_validated_database_uuid(profile_id, field="profile_id"),
        )


@dataclass(frozen=True)
class AuthorityLockProof:
    """In-process proof that the caller acquired the v1 transaction lock."""

    owner: AuthorityOwner
    key: int


def contract_artifact_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / CONTRACT_ARTIFACT_NAME


@lru_cache(maxsize=1)
def load_verified_contract_artifact() -> Mapping[str, Any]:
    artifact_bytes = contract_artifact_path().read_bytes()
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if artifact_sha256 != CONTRACT_ARTIFACT_SHA256:
        raise AuthorityLockError("authority_lock_contract_artifact_drift")
    try:
        artifact = json.loads(artifact_bytes)
    except json.JSONDecodeError as exc:
        raise AuthorityLockError("authority_lock_contract_artifact_invalid") from exc
    if (
        not isinstance(artifact, dict)
        or artifact.get("contract") != "ella-managed-cloud-authority-lock"
        or artifact.get("version") != CONTRACT_VERSION
        or artifact.get("encoding", {}).get("domain_ascii") != AUTHORITY_LOCK_DOMAIN.decode("ascii")
        or artifact.get("encoding", {}).get("field_count") != AUTHORITY_LOCK_FIELD_COUNT
        or artifact.get("key", {}).get("postgres_call") != "pg_advisory_xact_lock(bigint)"
    ):
        raise AuthorityLockError("authority_lock_contract_artifact_mismatch")
    return artifact


def _validated_database_uuid(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise AuthorityLockError("authority_lock_owner_not_database_uuid", field)
    canonical_uuid_bytes(str(value), field=field)
    return value


def canonical_uuid_bytes(value: Any, *, field: str) -> bytes:
    if value is None:
        raise AuthorityLockError("authority_lock_owner_missing", field)
    if not isinstance(value, str):
        raise AuthorityLockError("authority_lock_owner_not_text", field)
    if not value:
        raise AuthorityLockError("authority_lock_owner_missing", field)
    if not _CANONICAL_UUID_RE.match(value):
        raise AuthorityLockError("authority_lock_owner_not_canonical_uuid", field)
    return bytes.fromhex(value.replace("-", "").lower())


def authority_lock_preimage(account_id: Any, profile_id: Any) -> bytes:
    load_verified_contract_artifact()
    account_bytes = canonical_uuid_bytes(account_id, field="account_id")
    profile_bytes = canonical_uuid_bytes(profile_id, field="profile_id")
    return b"".join(
        (
            struct.pack("!I", len(AUTHORITY_LOCK_DOMAIN)),
            AUTHORITY_LOCK_DOMAIN,
            struct.pack("!I", AUTHORITY_LOCK_FIELD_COUNT),
            struct.pack("!I", UUID_BYTE_LENGTH),
            account_bytes,
            struct.pack("!I", UUID_BYTE_LENGTH),
            profile_bytes,
        )
    )


def authority_lock_digest(account_id: Any, profile_id: Any) -> bytes:
    return hashlib.sha256(authority_lock_preimage(account_id, profile_id)).digest()


def authority_lock_key(account_id: Any, profile_id: Any) -> int:
    return int.from_bytes(authority_lock_digest(account_id, profile_id)[:8], byteorder="big", signed=True)


async def acquire_authority_lock(
    connection: asyncpg.Connection,
    *,
    owner: AuthorityOwner,
) -> AuthorityLockProof:
    """Take the v1 lock. This must be the first statement in the transaction."""
    if not isinstance(owner, AuthorityOwner):
        raise AuthorityLockError("authority_lock_owner_invalid")
    account_id = str(_validated_database_uuid(owner.account_id, field="account_id"))
    profile_id = str(_validated_database_uuid(owner.profile_id, field="profile_id"))
    key = authority_lock_key(account_id, profile_id)
    await connection.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)
    return AuthorityLockProof(owner=owner, key=key)


def require_self_owner_lock(
    proof: AuthorityLockProof,
    *,
    user_id: Any,
) -> None:
    """Fail closed unless a helper received the exact self-profile lock proof."""
    if not isinstance(proof, AuthorityLockProof):
        raise AuthorityLockError("authority_lock_proof_missing")
    current = _validated_database_uuid(user_id, field="user_id")
    if proof.owner.account_id != current or proof.owner.profile_id != current:
        raise AuthorityLockError("authority_lock_proof_owner_mismatch")


async def resolve_self_owner_unlocked(
    connection: asyncpg.Connection,
    *,
    uid: str,
) -> AuthorityOwner:
    """Resolve a candidate self-profile owner outside the mutation transaction."""
    row = await connection.fetchrow(
        "SELECT id FROM users WHERE omi_uid = $1",
        uid,
    )
    if not row:
        raise AuthorityLockError("authority_lock_owner_missing")
    return AuthorityOwner.from_values(row["id"], row["id"])


async def verify_self_owner_after_lock(
    connection: asyncpg.Connection,
    *,
    uid: str,
    owner: AuthorityOwner,
) -> uuid.UUID:
    """Lock and re-read ownership after the v1 advisory lock; drift fails closed."""
    row = await connection.fetchrow(
        "SELECT id FROM users WHERE omi_uid = $1 FOR UPDATE",
        uid,
    )
    if not row:
        raise AuthorityLockError("authority_lock_owner_missing")
    current = _validated_database_uuid(row["id"], field="user_id")
    if current != owner.account_id or current != owner.profile_id:
        raise AuthorityLockError("authority_lock_owner_drift")
    return current
