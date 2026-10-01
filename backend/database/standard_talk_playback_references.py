"""Dormant protected associations for server-issued Standard Talk playback.

This store does not resolve authorization. Future server-only callers must lock
and revalidate canonical/runtime/consent authority before every transition.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import asyncpg

from database.ella_postgres import get_ella_postgres_pool

NORMALIZATION_VERSION = "standard-talk-utf16-500-emoji-v1"
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True, repr=False)
class PlaybackAssociation:
    uid: str
    event_id: str
    source_identity: str
    canonical_text_sha256: str
    runtime_authority_sha256: str
    consent_receipt_sha256: str
    normalization_version: str = NORMALIZATION_VERSION

    def __post_init__(self):
        for value in (self.uid, self.event_id, self.source_identity):
            if type(value) is not str or not value or len(value) > 512 or "\x00" in value:
                raise ValueError("standard_talk_association_invalid")
        for value in (self.canonical_text_sha256, self.runtime_authority_sha256, self.consent_receipt_sha256):
            if type(value) is not str or not _SHA256.fullmatch(value):
                raise ValueError("standard_talk_digest_invalid")
        if self.normalization_version != NORMALIZATION_VERSION:
            raise ValueError("standard_talk_normalization_invalid")

    def values(self) -> tuple[str, ...]:
        return (
            self.uid,
            self.event_id,
            self.source_identity,
            self.canonical_text_sha256,
            self.runtime_authority_sha256,
            self.consent_receipt_sha256,
            self.normalization_version,
        )


@dataclass(frozen=True)
class IssuedPlaybackReference:
    reference: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class PlaybackClaim:
    reference_sha256: str
    association: PlaybackAssociation
    claim_id: str
    playback_id: str


def reference_sha256(reference: str) -> str:
    if type(reference) is not str or not _REFERENCE.fullmatch(reference):
        raise ValueError("standard_talk_reference_invalid")
    # Require canonical base64url framing of exactly 32 random bytes.
    if reference[-1] not in "AEIMQUYcgkosw048":
        raise ValueError("standard_talk_reference_invalid")
    return hashlib.sha256(reference.encode("ascii")).hexdigest()


class PostgresStandardTalkPlaybackReferenceStore:
    def __init__(self, pool_factory: Callable[[], Awaitable[asyncpg.Pool]] = get_ella_postgres_pool):
        self._pool_factory = pool_factory

    @asynccontextmanager
    async def _connection(self, connection: asyncpg.Connection | None):
        if connection is not None:
            if not connection.is_in_transaction():
                raise ValueError("standard_talk_transaction_required")
            yield connection
            return
        pool = await self._pool_factory()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def _lock_reference(self, conn: asyncpg.Connection, digest: str, uid: str) -> bool:
        # A waiting UPDATE can evaluate its clock predicate before obtaining the
        # row lock. Lock first; the following CAS evaluates expiry after the wait.
        row = await conn.fetchrow(
            "SELECT reference_sha256 FROM ella_standard_talk_playback_references "
            "WHERE reference_sha256 = $1 AND uid = $2 FOR UPDATE",
            digest,
            uid,
        )
        return row is not None

    async def issue(
        self, association: PlaybackAssociation, *, connection: asyncpg.Connection | None = None
    ) -> IssuedPlaybackReference | None:
        reference = secrets.token_urlsafe(32)
        async with self._connection(connection) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ella_standard_talk_playback_references (
                    reference_sha256, uid, event_id, source_identity, canonical_text_sha256,
                    runtime_authority_sha256, consent_receipt_sha256, normalization_version
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT DO NOTHING RETURNING reference_sha256
                """,
                reference_sha256(reference),
                *association.values(),
            )
        # An existing association never receives another capability, even after
        # failure, expiry, or response loss. The stored hash cannot be replayed.
        return IssuedPlaybackReference(reference) if row is not None else None

    async def claim(
        self, reference: str, current: PlaybackAssociation, *, connection: asyncpg.Connection | None = None
    ) -> PlaybackClaim | None:
        digest = reference_sha256(reference)
        claim_id = secrets.token_hex(32)
        playback_id = secrets.token_hex(32)
        async with self._connection(connection) as conn:
            if not await self._lock_reference(conn, digest, current.uid):
                return None
            row = await conn.fetchrow(
                """
                UPDATE ella_standard_talk_playback_references
                SET state = 'synthesizing', claim_id = $9, playback_id = $10,
                    claimed_at = clock_timestamp()
                WHERE reference_sha256 = $1 AND uid = $2 AND event_id = $3 AND source_identity = $4
                    AND canonical_text_sha256 = $5 AND runtime_authority_sha256 = $6
                    AND consent_receipt_sha256 = $7 AND normalization_version = $8
                    AND state = 'issued' AND expires_at > clock_timestamp()
                RETURNING reference_sha256
                """,
                digest,
                *current.values(),
                claim_id,
                playback_id,
            )
        return PlaybackClaim(digest, current, claim_id, playback_id) if row is not None else None

    async def publish(
        self, claim: PlaybackClaim, current: PlaybackAssociation, *, connection: asyncpg.Connection | None = None
    ) -> bool:
        if current != claim.association:
            return False
        return await self._finish(claim, state="generated", require_unexpired=True, connection=connection)

    async def fail(self, claim: PlaybackClaim, *, connection: asyncpg.Connection | None = None) -> bool:
        return await self._finish(claim, state="failed", require_unexpired=False, connection=connection)

    async def _finish(
        self, claim: PlaybackClaim, *, state: str, require_unexpired: bool, connection: asyncpg.Connection | None
    ) -> bool:
        async with self._connection(connection) as conn:
            if not await self._lock_reference(conn, claim.reference_sha256, claim.association.uid):
                return False
            row = await conn.fetchrow(
                """
                UPDATE ella_standard_talk_playback_references
                SET state = $11, terminal_at = clock_timestamp()
                WHERE reference_sha256 = $1 AND uid = $2 AND event_id = $3 AND source_identity = $4
                    AND canonical_text_sha256 = $5 AND runtime_authority_sha256 = $6
                    AND consent_receipt_sha256 = $7 AND normalization_version = $8
                    AND claim_id = $9 AND playback_id = $10 AND state = 'synthesizing'
                    AND (NOT $12::boolean OR expires_at > clock_timestamp())
                RETURNING reference_sha256
                """,
                claim.reference_sha256,
                *claim.association.values(),
                claim.claim_id,
                claim.playback_id,
                state,
                require_unexpired,
            )
        return row is not None

    async def revoke(self, association: PlaybackAssociation, *, connection: asyncpg.Connection | None = None) -> int:
        """Retire this exact association; never reopen it when authority returns."""
        async with self._connection(connection) as conn:
            rows = await conn.fetch(
                """
                UPDATE ella_standard_talk_playback_references
                SET state = 'revoked', terminal_at = clock_timestamp()
                WHERE uid = $1 AND event_id = $2 AND source_identity = $3 AND canonical_text_sha256 = $4
                    AND runtime_authority_sha256 = $5 AND consent_receipt_sha256 = $6
                    AND normalization_version = $7 AND state IN ('issued', 'synthesizing', 'generated')
                RETURNING reference_sha256
                """,
                *association.values(),
            )
        return len(rows)
