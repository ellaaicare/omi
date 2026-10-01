"""Single-use refresh credential storage and coherent read-only grant snapshots."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.cloud.firestore_v1 import transactional

from database.authority_advisory_lock import AuthorityOwner, acquire_authority_lock
from database.ella_postgres import get_ella_postgres_pool


@dataclass(frozen=True)
class RefreshAuthoritySnapshot:
    grant: dict[str, Any] = field(repr=False)
    state: dict[str, Any] = field(repr=False)
    receipt: dict[str, Any] = field(repr=False)
    revisions: tuple[str, str, str] = field(repr=False)


def _revision(snapshot: Any) -> str:
    if not snapshot.exists or snapshot.update_time is None:
        raise ValueError("refresh_authority_revision_required")
    timestamp = snapshot.update_time.timestamp_pb()
    return f"{timestamp.seconds}:{timestamp.nanos}"


@transactional
def _read_authority(transaction: Any, grant_ref: Any, user_ref: Any) -> RefreshAuthoritySnapshot:
    grant = grant_ref.get(transaction=transaction)
    user = user_ref.get(transaction=transaction)
    state = dict((user.to_dict() or {}).get("ai_consent") or {})
    receipt_id = state.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id or "/" in receipt_id:
        raise ValueError("refresh_consent_required")
    receipt = user_ref.collection("ai_consent_receipts").document(receipt_id).get(transaction=transaction)
    return RefreshAuthoritySnapshot(
        grant=dict(grant.to_dict() or {}),
        state=state,
        receipt=dict(receipt.to_dict() or {}),
        revisions=(_revision(grant), _revision(user), _revision(receipt)),
    )


def read_refresh_authority(db: Any, *, uid: str, grant_id: str, collection: str) -> RefreshAuthoritySnapshot:
    if not uid or not grant_id or "/" in uid or "/" in grant_id:
        raise ValueError("refresh_authority_required")
    return _read_authority(
        db.transaction(), db.collection(collection).document(grant_id), db.collection("users").document(uid)
    )


def _family(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    if isinstance(result["authority"], str):
        result["authority"] = json.loads(result["authority"])
    return result


class MCPRefreshRepository:
    def __init__(self, pool: Any):
        self.pool = pool

    @classmethod
    async def create(cls) -> "MCPRefreshRepository":
        return cls(await get_ella_postgres_pool())

    async def read(self, family_id: str) -> dict[str, Any] | None:
        return _family(
            await self.pool.fetchrow(
                "SELECT *, expires_at > clock_timestamp() AND revoked_at IS NULL AS usable "
                "FROM ella_mcp_refresh_families WHERE id=$1",
                uuid.UUID(family_id),
            )
        )

    async def find(self, digest: str) -> dict[str, Any] | None:
        return _family(
            await self.pool.fetchrow(
                "SELECT family.*, token.consumed_at FROM ella_mcp_refresh_families family "
                "JOIN ella_mcp_refresh_tokens token ON token.family_id=family.id WHERE token.digest=$1",
                digest,
            )
        )

    async def create_family(self, authority: dict[str, Any], digest: str, token_digest: str) -> dict[str, Any]:
        owner = AuthorityOwner.from_values(
            uuid.UUID(authority["account_user_id"]), uuid.UUID(authority["profile_user_id"])
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await acquire_authority_lock(connection, owner=owner)
                row = await connection.fetchrow(
                    "INSERT INTO ella_mcp_refresh_families "
                    "(id,account_user_id,profile_user_id,client_id,authority,authority_digest,expires_at) "
                    "VALUES($1,$2,$3,$4,$5::jsonb,$6,clock_timestamp()+interval '7 days') RETURNING *",
                    uuid.uuid4(),
                    owner.account_id,
                    owner.profile_id,
                    authority["client_id"],
                    json.dumps(authority),
                    digest,
                )
                await connection.execute(
                    "INSERT INTO ella_mcp_refresh_tokens(digest,family_id) VALUES($1,$2)", token_digest, row["id"]
                )
        return _family(row)

    async def revoke(self, family_id: str) -> None:
        row = await self.read(family_id)
        if row is None:
            return
        owner = AuthorityOwner.from_values(row["account_user_id"], row["profile_user_id"])
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await acquire_authority_lock(connection, owner=owner)
                await connection.execute(
                    "UPDATE ella_mcp_refresh_families SET revoked_at=COALESCE(revoked_at,clock_timestamp()) "
                    "WHERE id=$1 AND account_user_id=$2 AND profile_user_id=$3",
                    row["id"],
                    owner.account_id,
                    owner.profile_id,
                )

    async def rotate(
        self, old_digest: str, new_digest: str, *, client_id: str, authority_digest: str
    ) -> dict[str, Any]:
        # Resolve only persisted owner coordinates before taking the shared lock.
        initial = await self.find(old_digest)
        if initial is None:
            raise ValueError("invalid_grant")
        owner = AuthorityOwner.from_values(initial["account_user_id"], initial["profile_user_id"])
        error = ""
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await acquire_authority_lock(connection, owner=owner)
                row = await connection.fetchrow(
                    "SELECT family.*, token.consumed_at FROM ella_mcp_refresh_families family "
                    "JOIN ella_mcp_refresh_tokens token ON token.family_id=family.id "
                    "WHERE token.digest=$1 FOR UPDATE OF family,token",
                    old_digest,
                )
                if (
                    row is None
                    or row["account_user_id"] != owner.account_id
                    or row["profile_user_id"] != owner.profile_id
                ):
                    raise ValueError("invalid_grant")
                now = await connection.fetchval("SELECT clock_timestamp()")
                if row["client_id"] != client_id:
                    error = "invalid_grant"
                elif row["revoked_at"] is not None or row["expires_at"] <= now:
                    error = "invalid_grant"
                elif row["consumed_at"] is not None or row["authority_digest"] != authority_digest:
                    await connection.execute(
                        "UPDATE ella_mcp_refresh_families SET revoked_at=clock_timestamp() WHERE id=$1", row["id"]
                    )
                    error = "invalid_grant"
                else:
                    await connection.execute(
                        "UPDATE ella_mcp_refresh_tokens SET consumed_at=$2 WHERE digest=$1", old_digest, now
                    )
                    await connection.execute(
                        "INSERT INTO ella_mcp_refresh_tokens(digest,family_id) VALUES($1,$2)", new_digest, row["id"]
                    )
        # A replay revocation must commit, not roll back with its error response.
        if error:
            raise ValueError(error)
        return _family(row)
