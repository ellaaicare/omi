"""Read-only, self-profile transaction authority for dormant Standard Talk.

The owner lock must precede every other transaction statement. No consent or
runtime enrollment is performed here; missing managed lineage is a denial.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

import asyncpg

from database import authority_advisory_lock, managed_cloud_consent, voice_canary


class StandardTalkDenied(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class ServerAssistantTurn:
    """Only a freshly produced server turn may construct this internal input.

    Canonical public writes are not trusted issuance provenance. This is NOT a
    public event selector or a proof that a future caller originated the turn.
    """

    uid: str
    turn_id: str
    session_id: str
    canonical_text_sha256: str

    def __post_init__(self):
        if any(
            type(value) is not str or not value or len(value) > 512 or "\x00" in value
            for value in (self.uid, self.turn_id, self.session_id)
        ):
            raise StandardTalkDenied("canonical_turn_invalid")
        if type(self.canonical_text_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.canonical_text_sha256):
            raise StandardTalkDenied("canonical_turn_invalid")

    @property
    def source_identity(self) -> str:
        return f"ios_chat:{self.uid}:{self.turn_id}"

    @property
    def event_id(self) -> str:
        return f"{self.source_identity}:assistant"


async def read_server_turn_on_connection(conn: asyncpg.Connection, turn: ServerAssistantTurn) -> str:
    row = await conn.fetchrow(
        """
        SELECT text FROM canonical_events
        WHERE uid = $1 AND canonical_identity = $1 AND event_id = $2 AND source_identity = $3 AND session_id = $4
          AND role = 'assistant' AND channel = 'ios_chat' AND provider = 'omi-ios-chat'
          AND privacy_scope = 'user_private' AND scan_policy = 'none'
        FOR SHARE
        """,
        turn.uid,
        turn.event_id,
        turn.source_identity,
        turn.session_id,
    )
    if (
        row is None
        or type(row["text"]) is not str
        or hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != turn.canonical_text_sha256
    ):
        raise StandardTalkDenied("canonical_turn_changed")
    return row["text"]


async def lock_current_authority(
    conn: asyncpg.Connection,
    *,
    uid: str,
    owner: authority_advisory_lock.AuthorityOwner,
    grant: managed_cloud_consent.ManagedCloudGrant,
    binding_id: str,
    target_id: str,
    entitlement_revision: int,
    model: str,
) -> tuple[dict, str, int]:
    """Lock and read existing managed Hermes-chat lineage on this connection."""
    if owner.account_id != owner.profile_id or grant.account_uid != uid or grant.profile_uid != uid:
        raise StandardTalkDenied("unsupported_authority")
    grant.validate()
    if not target_id:
        raise StandardTalkDenied("authority_mirror_missing")
    proof = await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
    await voice_canary.lock_runtime_authority_on_connection(conn, uid=uid, provider="hermes", mode="hermes-chat")
    await authority_advisory_lock.verify_self_owner_after_lock(conn, proof=proof, uid=uid, owner=owner)
    authority = await conn.fetchrow(
        "SELECT * FROM ella_managed_cloud_consent_authority WHERE user_id = $1 FOR UPDATE", owner.account_id
    )
    if authority is None or not managed_cloud_consent._grant_matches(authority, grant):
        raise StandardTalkDenied("authority_mirror_missing_or_stale")
    epoch = str(authority["authority_epoch"])
    decision = await voice_canary.revalidate_runtime_resolution_on_connection(
        conn,
        uid=uid,
        admitted_entitlement_revision=entitlement_revision,
        provider="hermes",
        model=model,
        mode="hermes-chat",
    )
    entitlement = decision.entitlement or {}
    if not decision.allowed or (
        str(entitlement.get("consent_authority_epoch") or "") != epoch
        or entitlement.get("consent_authority_revision") != authority["revision"]
        or entitlement.get("invitation_consent_pending") is not False
    ):
        raise StandardTalkDenied("entitlement_stale")
    row = await conn.fetchrow(
        """
        SELECT b.*, u.omi_uid, u.name, u.status AS user_status, u.profile_class,
               t.id AS runtime_target_id, t.mode AS resolved_mode,
               t.invitation_target_id AS attestation_runtime_target_id,
               t.endpoint_ref AS target_endpoint_ref, t.credential_ref AS target_credential_ref,
               t.entitlement_revision AS target_entitlement_revision,
               t.updated_at AS runtime_target_updated_at,
               t.policy_version AS target_policy_version,
               t.processor_set_hash AS target_processor_set_hash,
               t.scope_version AS target_scope_version, t.scope_hash AS target_scope_hash
        FROM users u
        JOIN ella_runtime_bindings b ON b.user_id = u.id
        JOIN ella_runtime_targets t ON t.runtime_binding_id = b.id
        JOIN voice_entitlements e ON e.uid = u.omi_uid
        JOIN ella_invitation_redemptions r ON r.invitation_id = e.invitation_id AND r.user_id = u.id
        JOIN ella_invitations i ON i.id = r.invitation_id
        JOIN ella_invitation_targets it ON it.id = r.invitation_target_id AND it.invitation_id = i.id
        WHERE u.omi_uid = $1 AND u.id = $2 AND u.status = 'ACTIVE' AND u.profile_class = 'real'
          AND b.id = $3 AND b.role = 'user' AND b.provider = 'hermes'
          AND b.status = 'active' AND b.active = TRUE AND b.health_state = 'healthy'
          AND b.account_user_id = u.id AND b.profile_user_id = u.id
          AND t.id = $4 AND t.account_user_id = u.id AND t.profile_user_id = u.id
          AND t.role = b.role AND t.provider = 'hermes' AND t.status = 'ready' AND t.mode = 'hermes-chat'
          AND t.invitation_target_id = it.id AND t.entitlement_revision = e.revision
          AND r.user_mapping_state = 'mapped' AND r.consent_pending = FALSE
          AND it.required_profile_class = 'real' AND it.consumed_at IS NOT NULL AND it.revoked_at IS NULL
          AND i.delivery_state = 'sent'
          AND ((i.kind = 'ordinary' AND i.state = 'redeemed') OR (i.kind = 'app_review' AND i.state = 'sent'))
          AND t.policy_version = $5 AND t.processor_set_hash = $6 AND t.scope_version = $7 AND t.scope_hash = $8
          AND i.required_consent_policy_version = t.policy_version
          AND i.required_consent_processor_set_hash = t.processor_set_hash
          AND i.required_consent_scope_version = t.scope_version
          AND i.required_consent_scope_hash = t.scope_hash
          AND e.consent_policy_version = t.policy_version
          AND e.consent_processor_set_hash = t.processor_set_hash
          AND e.consent_scope_version = t.scope_version AND e.consent_scope_hash = t.scope_hash
          AND e.provider_allowlist = ARRAY['hermes']::text[] AND e.model_allowlist = ARRAY[$9]::text[]
          AND e.mode_allowlist = $10::text[] AND e.fallback_policy = '{"enabled":false,"order":[]}'::jsonb
        FOR SHARE OF u, b, t, i, it, r
        """,
        uid,
        owner.account_id,
        uuid.UUID(binding_id),
        uuid.UUID(target_id),
        grant.policy_version,
        grant.processor_set_hash,
        grant.scope_version,
        grant.scope_hash,
        model,
        ["hermes-chat", "hermes-voice"],
    )
    if row is None:
        raise StandardTalkDenied("runtime_lineage_changed")
    binding = dict(row)
    binding["runtime_target_mode"] = binding.pop("resolved_mode")
    binding["consent_authority_epoch"] = epoch
    return binding, epoch, int(authority["revision"])
