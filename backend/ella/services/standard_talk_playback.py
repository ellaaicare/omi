"""Dormant Standard Talk transaction composition; no route or provider caller.

Production snapshot/normalization wiring is deliberately absent. Enabling this
internal object cannot enroll consent, issue grants, synthesize or play audio.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncpg

from database import authority_advisory_lock, managed_cloud_consent
from database.runtime_targets import RuntimeTargetLineage
from database.standard_talk_playback_authority import (
    ServerAssistantTurn,
    StandardTalkDenied,
    lock_current_authority,
    read_server_turn_on_connection,
)
from database.standard_talk_playback_references import (
    NORMALIZATION_VERSION,
    PlaybackAssociation,
    PlaybackClaim,
    PostgresStandardTalkPlaybackReferenceStore,
)
from ella.services import guardian_playback_ledger
from ella.services.ai_consent import consent_policy_contract
from ella.services.runtime_resolver import IsolatedRuntime, runtime_authority_identity, runtime_from_binding


@dataclass(frozen=True, repr=False)
class FreshAuthority:
    """Trusted server loader must re-resolve runtime AND current Firestore receipt.

    This is not an HTTP input or an authorization proof by itself. Every field
    is checked against existing SQL lineage under the owner/runtime locks.
    """

    runtime: IsolatedRuntime
    grant: managed_cloud_consent.ManagedCloudGrant


@dataclass(frozen=True, repr=False)
class AdmittedPlayback:
    claim: PlaybackClaim
    turn: ServerAssistantTurn
    spoken_text_sha256: str


@dataclass(frozen=True, repr=False)
class VerifiedSpokenText:
    """Output of a future reviewed SERVER normalizer, not client input."""

    text: str
    canonical_text_sha256: str
    spoken_text_sha256: str
    normalization_version: str = NORMALIZATION_VERSION


class StandardTalkPlaybackComposition:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        fresh_authority: Callable[[str], Awaitable[FreshAuthority]] | None = None,
        verified_spoken_text: Callable[[ServerAssistantTurn, str], VerifiedSpokenText] | None = None,
        enabled: bool = False,
    ):
        self._pool = pool
        self._fresh_authority = fresh_authority
        # A reviewed server normalizer MUST verify canonical input/version.
        # No implementation is supplied in this stage; arbitrary client text is
        # not accepted, and absence denies before any protected mutation.
        self._verified_spoken_text = verified_spoken_text
        self._enabled = enabled

        async def factory():
            return pool

        self._references = PostgresStandardTalkPlaybackReferenceStore(factory)

    async def _fresh(self, uid: str) -> tuple[FreshAuthority, authority_advisory_lock.AuthorityOwner]:
        if not self._enabled or self._fresh_authority is None or self._verified_spoken_text is None:
            raise StandardTalkDenied("standard_talk_unavailable")
        fresh = await self._fresh_authority(uid)
        runtime = fresh.runtime
        if runtime.uid != uid or fresh.grant.account_uid != uid or fresh.grant.profile_uid != uid:
            raise StandardTalkDenied("authority_owner_changed")
        if runtime.provider != "hermes" or runtime.runtime_target_mode != "hermes-chat":
            raise StandardTalkDenied("unsupported_authority")
        if runtime.account_user_id != runtime.profile_user_id:
            raise StandardTalkDenied("unsupported_authority")
        if (
            consent_policy_contract(
                fresh.grant.policy_version,
                fresh.grant.processor_set_hash,
                fresh.grant.scope_version,
                fresh.grant.scope_hash,
            )
            is None
        ):
            raise StandardTalkDenied("consent_contract_stale")
        # The shared-lock API deliberately requires actual database UUIDs,
        # never parsed identity strings supplied by a runtime/request object.
        async with self._pool.acquire() as conn:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(conn, uid=uid)
        if str(owner.account_id) != runtime.account_user_id or str(owner.profile_id) != runtime.profile_user_id:
            raise StandardTalkDenied("authority_owner_changed")
        return fresh, owner

    async def _current(self, conn, prepared, turn: ServerAssistantTurn) -> tuple[PlaybackAssociation, str]:
        fresh, owner = prepared
        runtime = fresh.runtime
        binding, epoch, authority_revision = await lock_current_authority(
            conn,
            uid=turn.uid,
            owner=owner,
            grant=fresh.grant,
            binding_id=runtime.binding_id,
            target_id=runtime.runtime_target_id,
            entitlement_revision=runtime.target_entitlement_revision,
            model=runtime.expected_model,
        )
        lineage = RuntimeTargetLineage(
            fresh.grant.policy_version,
            fresh.grant.processor_set_hash,
            fresh.grant.scope_version,
            fresh.grant.scope_hash,
        )
        readback = runtime_from_binding(binding, turn.uid, self_hosted_authority_lineage=lineage)
        identity = runtime_authority_identity(runtime)
        if runtime_authority_identity(readback).digest != identity.digest:
            raise StandardTalkDenied("runtime_authority_changed")
        raw = await read_server_turn_on_connection(conn, turn)
        authority_material = json.dumps(
            [identity.digest, epoch, authority_revision, runtime.target_entitlement_revision], separators=(",", ":")
        )
        association = PlaybackAssociation(
            turn.uid,
            turn.event_id,
            turn.source_identity,
            turn.canonical_text_sha256,
            hashlib.sha256(authority_material.encode()).hexdigest(),
            managed_cloud_consent.consent_receipt_ref(turn.uid, fresh.grant.consent_receipt_id)[7:],
            NORMALIZATION_VERSION,
        )
        return association, raw

    def _spoken(self, turn: ServerAssistantTurn, raw: str) -> str:
        verified = self._verified_spoken_text(turn, raw)
        if (
            not isinstance(verified, VerifiedSpokenText)
            or type(verified.text) is not str
            or not verified.text
            or verified.normalization_version != NORMALIZATION_VERSION
            or verified.canonical_text_sha256 != turn.canonical_text_sha256
        ):
            raise StandardTalkDenied("normalization_unavailable")
        try:
            length = len(verified.text.encode("utf-16-le")) // 2
            digest = hashlib.sha256(verified.text.encode("utf-8")).hexdigest()
        except UnicodeEncodeError as exc:
            raise StandardTalkDenied("normalization_unavailable") from exc
        if length > 500 or digest != verified.spoken_text_sha256:
            raise StandardTalkDenied("normalization_unavailable")
        return verified.text

    async def issue_server_turn(self, turn: ServerAssistantTurn):
        fresh = await self._fresh(turn.uid)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                association, raw = await self._current(conn, fresh, turn)
                self._spoken(turn, raw)
                return await self._references.issue(association, connection=conn)

    async def claim_for_synthesis(self, reference: str, turn: ServerAssistantTurn) -> AdmittedPlayback | None:
        fresh = await self._fresh(turn.uid)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                association, raw = await self._current(conn, fresh, turn)
                spoken = self._spoken(turn, raw)
                claim = await self._references.claim(reference, association, connection=conn)
                return AdmittedPlayback(claim, turn, hashlib.sha256(spoken.encode()).hexdigest()) if claim else None

    async def publish_generated(self, admitted: AdmittedPlayback) -> bool:
        # Called only after future synthesis, with a NEW Firestore/runtime read.
        # Never hold the owner lock across synthesis, network or provider work.
        fresh = await self._fresh(admitted.turn.uid)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                association, raw = await self._current(conn, fresh, admitted.turn)
                spoken = self._spoken(admitted.turn, raw)
                if hashlib.sha256(spoken.encode()).hexdigest() != admitted.spoken_text_sha256:
                    raise StandardTalkDenied("normalization_changed")
                if not await self._references.publish(admitted.claim, association, connection=conn):
                    return False
                await guardian_playback_ledger.record_generated(
                    self._pool,
                    uid=association.uid,
                    playback_id=admitted.claim.playback_id,
                    purpose="standard_talk",
                    playback_text=spoken,
                    text_provenance="server_canonical_standard_talk_v1",
                    connection=conn,
                    require_new=True,
                )
                return True
