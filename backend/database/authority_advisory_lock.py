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
import weakref
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
IDENTITY_OWNER_NAMESPACE = uuid.UUID("d9b4e06b-3553-5f97-b04d-6e88f730b94f")

_CANONICAL_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_PROOF_ISSUER = object()


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
class IdentityOwnerResolution:
    owner: AuthorityOwner
    allow_create: bool


class AuthorityLockProof:
    """Opaque handle verified against PostgreSQL before every helper mutation."""

    __slots__ = ("__weakref__",)

    def __new__(cls, issuer: object = None) -> "AuthorityLockProof":
        if issuer is not _PROOF_ISSUER:
            raise AuthorityLockError("authority_lock_proof_construction_forbidden")
        return super().__new__(cls)

    def __copy__(self) -> "AuthorityLockProof":
        raise AuthorityLockError("authority_lock_proof_copy_forbidden")

    def __deepcopy__(self, memo: dict[int, Any]) -> "AuthorityLockProof":
        del memo
        raise AuthorityLockError("authority_lock_proof_copy_forbidden")

    def __reduce__(self) -> tuple[Any, ...]:
        raise AuthorityLockError("authority_lock_proof_serialization_forbidden")

    def __repr__(self) -> str:
        return "AuthorityLockProof(<opaque>)"


@dataclass(frozen=True)
class _AuthorityLockProofState:
    owner: AuthorityOwner
    key: int
    connection: asyncpg.Connection
    backend_pid: int
    transaction_id: int


_PROOF_STATES: weakref.WeakKeyDictionary[AuthorityLockProof, _AuthorityLockProofState] = weakref.WeakKeyDictionary()


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


def _advisory_lock_parts(key: int) -> tuple[int, int]:
    unsigned_key = key & ((1 << 64) - 1)
    return (unsigned_key >> 32) & 0xFFFFFFFF, unsigned_key & 0xFFFFFFFF


async def _read_transaction_lock_state(
    connection: asyncpg.Connection,
    *,
    key: int,
) -> tuple[int, int, bool]:
    class_id, object_id = _advisory_lock_parts(key)
    row = await connection.fetchrow(
        """
        SELECT
            pg_backend_pid() AS backend_pid,
            txid_current() AS transaction_id,
            EXISTS (
                SELECT 1
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND pid = pg_backend_pid()
                  AND classid::bigint = $1
                  AND objid::bigint = $2
                  AND objsubid = 1
                  AND mode = 'ExclusiveLock'
                  AND granted
            ) AS lock_held
        """,
        class_id,
        object_id,
    )
    if not row:
        raise AuthorityLockError("authority_lock_proof_state_unavailable")
    return int(row["backend_pid"]), int(row["transaction_id"]), bool(row["lock_held"])


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
    backend_pid, transaction_id, lock_held = await _read_transaction_lock_state(
        connection,
        key=key,
    )
    if not lock_held:
        raise AuthorityLockError("authority_lock_not_held")
    proof = AuthorityLockProof(_PROOF_ISSUER)
    _PROOF_STATES[proof] = _AuthorityLockProofState(
        owner=owner,
        key=key,
        connection=connection,
        backend_pid=backend_pid,
        transaction_id=transaction_id,
    )
    return proof


async def require_authority_lock(
    connection: asyncpg.Connection,
    proof: AuthorityLockProof,
    *,
    owner: AuthorityOwner,
) -> None:
    """Verify an opaque proof against this connection's current transaction."""
    if type(proof) is not AuthorityLockProof:
        raise AuthorityLockError("authority_lock_proof_missing")
    state = _PROOF_STATES.get(proof)
    if state is None:
        raise AuthorityLockError("authority_lock_proof_forged")
    if connection is not state.connection:
        raise AuthorityLockError("authority_lock_proof_connection_mismatch")
    if owner != state.owner:
        raise AuthorityLockError("authority_lock_proof_owner_mismatch")
    backend_pid, transaction_id, lock_held = await _read_transaction_lock_state(
        connection,
        key=state.key,
    )
    if backend_pid != state.backend_pid:
        raise AuthorityLockError("authority_lock_proof_connection_mismatch")
    if transaction_id != state.transaction_id:
        raise AuthorityLockError("authority_lock_proof_transaction_stale")
    if not lock_held:
        raise AuthorityLockError("authority_lock_proof_lock_missing")


async def require_self_owner_lock(
    connection: asyncpg.Connection,
    proof: AuthorityLockProof,
    *,
    user_id: Any,
) -> None:
    """Fail closed unless this transaction holds the exact self-profile lock."""
    current = _validated_database_uuid(user_id, field="user_id")
    await require_authority_lock(
        connection,
        proof,
        owner=AuthorityOwner.from_values(current, current),
    )


async def require_user_write_status(
    connection: asyncpg.Connection,
    proof: AuthorityLockProof,
    *,
    user_id: Any,
    allowed_statuses: tuple[str, ...] = ("ACTIVE",),
) -> str:
    """Fence authority writes after the owner lock has been proven.

    PENDING is allowed only at explicitly named bootstrap/activation call sites.
    Tombstoned accounts are never writable through this helper.
    """
    await require_self_owner_lock(
        connection,
        proof,
        user_id=user_id,
    )
    if not allowed_statuses or any(status in {"DELETION_PENDING", "DELETED"} for status in allowed_statuses):
        raise AuthorityLockError("authority_write_status_policy_invalid")
    status = await connection.fetchval(
        "SELECT status FROM users WHERE id = $1 FOR UPDATE",
        _validated_database_uuid(user_id, field="user_id"),
    )
    if status is None:
        raise AuthorityLockError("authority_write_owner_missing")
    normalized = str(status)
    if normalized not in allowed_statuses:
        raise AuthorityLockError("authority_write_user_not_active")
    return normalized


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


def provisional_identity_owner(uid: str) -> AuthorityOwner:
    if not isinstance(uid, str) or not uid:
        raise AuthorityLockError("authority_lock_identity_uid_missing")
    owner_id = uuid.uuid5(IDENTITY_OWNER_NAMESPACE, uid)
    return AuthorityOwner.from_values(owner_id, owner_id)


async def resolve_identity_owner_unlocked(
    connection: asyncpg.Connection,
    *,
    uid: str,
    email: str,
) -> IdentityOwnerResolution:
    """Resolve one existing owner or a deterministic not-yet-created owner."""
    rows = await connection.fetch(
        """
        SELECT id
        FROM users
        WHERE omi_uid = $1 OR lower(email) = lower($2)
        ORDER BY id
        """,
        uid,
        email,
    )
    owner_ids = {_validated_database_uuid(row["id"], field="user_id") for row in rows}
    if len(owner_ids) > 1:
        raise AuthorityLockError("authority_lock_identity_conflict")
    if owner_ids:
        owner_id = next(iter(owner_ids))
        return IdentityOwnerResolution(
            owner=AuthorityOwner.from_values(owner_id, owner_id),
            allow_create=False,
        )
    return IdentityOwnerResolution(
        owner=provisional_identity_owner(uid),
        allow_create=True,
    )


async def verify_identity_owner_after_lock(
    connection: asyncpg.Connection,
    *,
    uid: str,
    email: str,
    resolution: IdentityOwnerResolution,
    proof: AuthorityLockProof,
) -> tuple[Mapping[str, Any], ...]:
    """Re-read and lock identity ownership; unexpected ownership fails closed."""
    await require_authority_lock(
        connection,
        proof,
        owner=resolution.owner,
    )
    rows = await connection.fetch(
        """
        SELECT id, omi_uid, email, name, timezone, status
        FROM users
        WHERE omi_uid = $1 OR lower(email) = lower($2)
        ORDER BY id
        FOR UPDATE
        """,
        uid,
        email,
    )
    owner_ids = {_validated_database_uuid(row["id"], field="user_id") for row in rows}
    if len(owner_ids) > 1 or (owner_ids and owner_ids != {resolution.owner.account_id}):
        raise AuthorityLockError("authority_lock_owner_drift")
    if not owner_ids:
        if not resolution.allow_create or resolution.owner != provisional_identity_owner(uid):
            raise AuthorityLockError("authority_lock_owner_drift")
        return ()
    return tuple(rows)


async def verify_self_owner_after_lock(
    connection: asyncpg.Connection,
    *,
    uid: str,
    owner: AuthorityOwner,
    proof: AuthorityLockProof,
) -> uuid.UUID:
    """Lock and re-read ownership after the v1 advisory lock; drift fails closed."""
    await require_authority_lock(
        connection,
        proof,
        owner=owner,
    )
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
