"""Durable self-hosted iMessage runtime claims and delivery outbox."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from database.ella_provisioning import get_pool


class ImessageRuntimeRepositoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImessageRuntimeAuthority:
    uid: str
    user_id: uuid.UUID
    profile_user_id: uuid.UUID
    runtime_binding_id: uuid.UUID
    runtime_target_id: uuid.UUID
    runtime_binding_revision: int
    runtime_target_entitlement_revision: int
    runtime_target_updated_at: datetime
    runtime_authority_digest: str
    runtime_agent_id: str
    runtime_instance_id: Optional[str]
    runtime_profile_name: str


def _row(row: Any) -> Optional[dict[str, Any]]:
    return dict(row) if row else None


class ImessageRuntimeRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def create(cls) -> "ImessageRuntimeRepository":
        return cls(await get_pool())

    async def assert_schema_ready(self) -> None:
        ready = await self.pool.fetchval(
            """
            SELECT
                to_regclass('ella_imessage_message_receipts') IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'ella_imessage_channel_bindings'
                      AND column_name = 'transport_connection_ref_hmac'
                )
            """
        )
        if ready is not True:
            raise ImessageRuntimeRepositoryError("imessage_runtime_schema_not_ready")

    async def resolve_binding(
        self,
        *,
        line_identity_hmac: str,
        contact_identity_hmac: str,
    ) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT
                b.*,
                u.omi_uid,
                u.status AS user_status,
                c.current_receipt_id,
                c.decision AS current_consent_decision,
                c.authority_epoch AS current_consent_authority_epoch,
                c.revision AS current_consent_revision,
                rb.status AS runtime_binding_status,
                rb.active AS runtime_binding_active,
                rb.revision AS current_runtime_revision,
                rb.account_user_id AS runtime_account_user_id,
                rb.profile_user_id AS runtime_profile_user_id,
                rb.role AS runtime_binding_role,
                rb.health_state AS runtime_binding_health_state,
                rb.agent_id AS runtime_agent_id,
                rb.runtime_instance_id,
                rb.profile_name AS runtime_profile_name,
                rt.status AS runtime_target_status,
                rt.mode AS runtime_target_mode,
                rt.provider AS runtime_target_provider,
                rt.role AS runtime_target_role,
                rt.account_user_id AS target_account_user_id,
                rt.profile_user_id AS target_profile_user_id,
                rt.entitlement_revision AS runtime_target_entitlement_revision,
                rt.updated_at AS runtime_target_updated_at
            FROM ella_imessage_channel_bindings b
            JOIN users u ON u.id = b.user_id
            JOIN ella_imessage_consent_authority c ON c.user_id = b.user_id
            JOIN ella_runtime_bindings rb ON rb.id = b.runtime_binding_id
            JOIN ella_runtime_targets rt ON rt.id = b.runtime_target_id
            WHERE b.line_identity_hmac = $1
              AND b.contact_identity_hmac = $2
              AND b.status = 'active'
              AND u.status = 'ACTIVE'
              AND c.decision = 'granted'
              AND c.current_receipt_id = b.consent_receipt_id
              AND c.authority_epoch = b.consent_authority_epoch
              AND rb.status = 'active'
              AND rb.active = true
              AND rb.health_state = 'healthy'
              AND rb.role = 'user'
              AND rb.user_id = b.user_id
              AND rb.account_user_id = b.user_id
              AND rb.profile_user_id = b.user_id
              AND rt.status = 'ready'
              AND rt.provider = 'hermes'
              AND rt.role = 'user'
              AND rt.mode = 'hermes-chat'
              AND rt.runtime_binding_id = b.runtime_binding_id
              AND rt.account_user_id = b.user_id
              AND rt.profile_user_id = b.user_id
              AND rt.entitlement_revision IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM ella_imessage_account_deletion_fences deletion
                  WHERE deletion.user_id = b.user_id
              )
            """,
            line_identity_hmac,
            contact_identity_hmac,
        )
        return _row(row)

    async def record_heartbeat(
        self,
        *,
        binding_id: str,
        generation: int,
        connection_ref_hmac: str,
        authority: ImessageRuntimeAuthority,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_imessage_channel_bindings b
            SET transport_connection_ref_hmac = $3,
                transport_connected_at = CURRENT_TIMESTAMP,
                last_transport_healthy_at = CURRENT_TIMESTAMP,
                revision = b.revision + 1,
                updated_at = CURRENT_TIMESTAMP
            FROM users u,
                 ella_imessage_consent_authority c,
                 ella_runtime_bindings rb,
                 ella_runtime_targets rt
            WHERE b.id = $1
              AND b.generation = $2
              AND b.user_id = $4
              AND b.runtime_binding_id = $5
              AND b.runtime_target_id = $6
              AND b.runtime_authority_digest = $7
              AND b.status = 'active'
              AND u.id = b.user_id
              AND u.status = 'ACTIVE'
              AND u.omi_uid = $8
              AND c.user_id = b.user_id
              AND c.decision = 'granted'
              AND c.current_receipt_id = b.consent_receipt_id
              AND c.authority_epoch = b.consent_authority_epoch
              AND rb.id = b.runtime_binding_id
              AND rb.status = 'active'
              AND rb.active = true
              AND rb.health_state = 'healthy'
              AND rb.role = 'user'
              AND rb.user_id = $4
              AND rb.account_user_id = $4
              AND rb.profile_user_id = $9
              AND rb.revision = $10
              AND rb.agent_id = $11
              AND rb.runtime_instance_id IS NOT DISTINCT FROM $12
              AND rb.profile_name = $13
              AND rt.id = b.runtime_target_id
              AND rt.status = 'ready'
              AND rt.provider = 'hermes'
              AND rt.role = 'user'
              AND rt.mode = 'hermes-chat'
              AND rt.runtime_binding_id = rb.id
              AND rt.account_user_id = $4
              AND rt.profile_user_id = $9
              AND rt.entitlement_revision = $14
              AND rt.updated_at = $15
            RETURNING b.*
            """,
            uuid.UUID(str(binding_id)),
            generation,
            connection_ref_hmac,
            authority.user_id,
            authority.runtime_binding_id,
            authority.runtime_target_id,
            authority.runtime_authority_digest,
            authority.uid,
            authority.profile_user_id,
            authority.runtime_binding_revision,
            authority.runtime_agent_id,
            authority.runtime_instance_id,
            authority.runtime_profile_name,
            authority.runtime_target_entitlement_revision,
            authority.runtime_target_updated_at,
        )
        if not row:
            raise ImessageRuntimeRepositoryError("imessage_transport_authority_changed")
        return dict(row)

    async def claim_message(
        self,
        *,
        binding: dict[str, Any],
        inbound_provider_ref_hmac: str,
        inbound_payload_sha256: str,
        message_text: str,
        occurred_at: datetime,
        lease_seconds: int,
        authority: ImessageRuntimeAuthority,
    ) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 900:
            raise ImessageRuntimeRepositoryError("imessage_runtime_lease_invalid")
        lease_token = uuid.uuid4()
        binding_id = uuid.UUID(str(binding["id"]))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    """
                    SELECT b.*
                    FROM ella_imessage_channel_bindings b
                    JOIN users u ON u.id = b.user_id
                    JOIN ella_imessage_consent_authority c ON c.user_id = b.user_id
                    JOIN ella_runtime_bindings rb ON rb.id = b.runtime_binding_id
                    JOIN ella_runtime_targets rt ON rt.id = b.runtime_target_id
                    WHERE b.id = $1
                      AND b.user_id = $2
                      AND b.status = 'active'
                      AND b.generation = $3
                      AND b.consent_receipt_id = $4
                      AND b.consent_authority_epoch = $5
                      AND b.runtime_binding_id = $6
                      AND b.runtime_target_id = $7
                      AND b.runtime_authority_digest = $8
                      AND u.status = 'ACTIVE'
                      AND u.omi_uid = $9
                      AND c.decision = 'granted'
                      AND c.current_receipt_id = b.consent_receipt_id
                      AND c.authority_epoch = b.consent_authority_epoch
                      AND rb.status = 'active'
                      AND rb.active = true
                      AND rb.health_state = 'healthy'
                      AND rb.role = 'user'
                      AND rb.user_id = $2
                      AND rb.account_user_id = $2
                      AND rb.profile_user_id = $10
                      AND rb.revision = $11
                      AND rb.agent_id = $12
                      AND rb.runtime_instance_id IS NOT DISTINCT FROM $13
                      AND rb.profile_name = $14
                      AND rt.status = 'ready'
                      AND rt.provider = 'hermes'
                      AND rt.role = 'user'
                      AND rt.mode = 'hermes-chat'
                      AND rt.runtime_binding_id = b.runtime_binding_id
                      AND rt.account_user_id = $2
                      AND rt.profile_user_id = $10
                      AND rt.entitlement_revision = $15
                      AND rt.updated_at = $16
                    FOR SHARE OF b, u, c, rb, rt
                    """,
                    binding_id,
                    authority.user_id,
                    int(binding["generation"]),
                    binding["consent_receipt_id"],
                    binding["consent_authority_epoch"],
                    binding["runtime_binding_id"],
                    binding["runtime_target_id"],
                    binding["runtime_authority_digest"],
                    authority.uid,
                    authority.profile_user_id,
                    authority.runtime_binding_revision,
                    authority.runtime_agent_id,
                    authority.runtime_instance_id,
                    authority.runtime_profile_name,
                    authority.runtime_target_entitlement_revision,
                    authority.runtime_target_updated_at,
                )
                if not current:
                    raise ImessageRuntimeRepositoryError("imessage_authority_changed")
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO ella_imessage_message_receipts (
                        binding_id, user_id, inbound_provider_ref_hmac,
                        inbound_payload_sha256, message_text, occurred_at,
                        binding_generation, consent_receipt_id,
                        consent_authority_epoch, runtime_binding_id,
                        runtime_target_id, runtime_authority_digest,
                        lease_token, lease_expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, CURRENT_TIMESTAMP + ($14 * INTERVAL '1 second')
                    )
                    ON CONFLICT (binding_id, inbound_provider_ref_hmac) DO NOTHING
                    RETURNING *
                    """,
                    binding_id,
                    current["user_id"],
                    inbound_provider_ref_hmac,
                    inbound_payload_sha256,
                    message_text,
                    occurred_at,
                    int(current["generation"]),
                    current["consent_receipt_id"],
                    current["consent_authority_epoch"],
                    current["runtime_binding_id"],
                    current["runtime_target_id"],
                    current["runtime_authority_digest"],
                    lease_token,
                    lease_seconds,
                )
                if inserted:
                    result = dict(inserted)
                    result.update(acquired=True, duplicate=False, reclaimed=False)
                    return result

                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_message_receipts
                    WHERE binding_id = $1 AND inbound_provider_ref_hmac = $2
                    FOR UPDATE
                    """,
                    binding_id,
                    inbound_provider_ref_hmac,
                )
                if not existing:
                    raise ImessageRuntimeRepositoryError("imessage_message_claim_lost")
                result = dict(existing)
                if result["inbound_payload_sha256"] != inbound_payload_sha256:
                    raise ImessageRuntimeRepositoryError("imessage_duplicate_payload_conflict")
                if (
                    int(result["binding_generation"]) != int(current["generation"])
                    or result["consent_receipt_id"] != current["consent_receipt_id"]
                    or result["runtime_authority_digest"] != current["runtime_authority_digest"]
                ):
                    await connection.execute(
                        """
                        UPDATE ella_imessage_message_receipts
                        SET status = 'quarantined',
                            reconciliation_status = 'manual_required',
                            error_code = 'imessage_authority_changed',
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                          AND status IN ('claimed', 'running', 'awaiting_delivery')
                        """,
                        result["id"],
                    )
                    raise ImessageRuntimeRepositoryError("imessage_authority_changed")

                lease_expires_at = result.get("lease_expires_at")
                stale = result["status"] in {"claimed", "running"} and (
                    not isinstance(lease_expires_at, datetime) or lease_expires_at <= datetime.now(timezone.utc)
                )
                if stale and not result["model_started"]:
                    reclaimed = await connection.fetchrow(
                        """
                        UPDATE ella_imessage_message_receipts
                        SET status = 'claimed',
                            lease_token = $2,
                            lease_expires_at = CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'),
                            attempt_count = attempt_count + 1,
                            reconciliation_status = 'recovered',
                            error_code = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                        RETURNING *
                        """,
                        result["id"],
                        lease_token,
                        lease_seconds,
                    )
                    recovered = dict(reclaimed)
                    recovered.update(acquired=True, duplicate=True, reclaimed=True)
                    return recovered
                if stale and result["model_started"]:
                    uncertain = await connection.fetchrow(
                        """
                        UPDATE ella_imessage_message_receipts
                        SET status = 'uncertain',
                            reconciliation_status = 'manual_required',
                            error_code = 'imessage_model_outcome_unconfirmed',
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                        RETURNING *
                        """,
                        result["id"],
                    )
                    result = dict(uncertain)
                result.update(acquired=False, duplicate=True, reclaimed=False)
                return result

    async def mark_model_started(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        authority: ImessageRuntimeAuthority,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_imessage_message_receipts r
            SET status = 'running',
                model_started = true,
                updated_at = CURRENT_TIMESTAMP
            FROM ella_imessage_channel_bindings b,
                 users u,
                 ella_imessage_consent_authority c,
                 ella_runtime_bindings rb,
                 ella_runtime_targets rt
            WHERE r.id = $1
              AND r.status = 'claimed'
              AND r.lease_token = $2
              AND r.lease_expires_at > CURRENT_TIMESTAMP
              AND b.id = r.binding_id
              AND b.status = 'active'
              AND b.generation = r.binding_generation
              AND b.consent_receipt_id = r.consent_receipt_id
              AND b.consent_authority_epoch = r.consent_authority_epoch
              AND b.runtime_binding_id = r.runtime_binding_id
              AND b.runtime_target_id = r.runtime_target_id
              AND b.runtime_authority_digest = r.runtime_authority_digest
              AND b.user_id = $3
              AND b.runtime_binding_id = $5
              AND b.runtime_target_id = $6
              AND b.runtime_authority_digest = $7
              AND u.id = r.user_id
              AND u.status = 'ACTIVE'
              AND u.id = $3
              AND u.omi_uid = $8
              AND c.user_id = r.user_id
              AND c.decision = 'granted'
              AND c.current_receipt_id = r.consent_receipt_id
              AND c.authority_epoch = r.consent_authority_epoch
              AND rb.id = r.runtime_binding_id
              AND rb.status = 'active'
              AND rb.active = true
              AND rb.health_state = 'healthy'
              AND rb.role = 'user'
              AND rb.user_id = $3
              AND rb.account_user_id = $3
              AND rb.profile_user_id = $4
              AND rb.revision = $9
              AND rb.agent_id = $10
              AND rb.runtime_instance_id IS NOT DISTINCT FROM $11
              AND rb.profile_name = $12
              AND rt.id = r.runtime_target_id
              AND rt.status = 'ready'
              AND rt.provider = 'hermes'
              AND rt.role = 'user'
              AND rt.mode = 'hermes-chat'
              AND rt.runtime_binding_id = rb.id
              AND rt.account_user_id = $3
              AND rt.profile_user_id = $4
              AND rt.entitlement_revision = $13
              AND rt.updated_at = $14
            RETURNING r.*
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(lease_token)),
            authority.user_id,
            authority.profile_user_id,
            authority.runtime_binding_id,
            authority.runtime_target_id,
            authority.runtime_authority_digest,
            authority.uid,
            authority.runtime_binding_revision,
            authority.runtime_agent_id,
            authority.runtime_instance_id,
            authority.runtime_profile_name,
            authority.runtime_target_entitlement_revision,
            authority.runtime_target_updated_at,
        )
        if not row:
            raise ImessageRuntimeRepositoryError("imessage_message_claim_conflict")
        return dict(row)

    async def complete_model(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        canonical_inbound_event_id: str,
        canonical_outbound_event_id: str,
        outbound_text: str,
        authority: ImessageRuntimeAuthority,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_imessage_message_receipts r
            SET status = 'awaiting_delivery',
                canonical_inbound_event_id = $3,
                canonical_outbound_event_id = $4,
                outbound_text = $5,
                runtime_revision = $6,
                runtime_agent_id = $7,
                lease_token = NULL,
                lease_expires_at = NULL,
                error_code = NULL,
                updated_at = CURRENT_TIMESTAMP
            FROM ella_imessage_channel_bindings b,
                 users u,
                 ella_imessage_consent_authority c,
                 ella_runtime_bindings rb,
                 ella_runtime_targets rt
            WHERE r.id = $1
              AND r.status = 'running'
              AND r.lease_token = $2
              AND r.lease_expires_at > CURRENT_TIMESTAMP
              AND b.id = r.binding_id
              AND b.status = 'active'
              AND b.generation = r.binding_generation
              AND b.runtime_authority_digest = r.runtime_authority_digest
              AND b.user_id = $8
              AND b.runtime_binding_id = $10
              AND b.runtime_target_id = $11
              AND b.runtime_authority_digest = $12
              AND u.id = r.user_id
              AND u.status = 'ACTIVE'
              AND u.id = $8
              AND u.omi_uid = $13
              AND c.user_id = r.user_id
              AND c.decision = 'granted'
              AND c.current_receipt_id = r.consent_receipt_id
              AND c.authority_epoch = r.consent_authority_epoch
              AND rb.id = r.runtime_binding_id
              AND rb.status = 'active'
              AND rb.active = true
              AND rb.health_state = 'healthy'
              AND rb.role = 'user'
              AND rb.user_id = $8
              AND rb.account_user_id = $8
              AND rb.profile_user_id = $9
              AND rb.revision = $6
              AND rb.agent_id = $7
              AND rb.runtime_instance_id IS NOT DISTINCT FROM $14
              AND rb.profile_name = $15
              AND rt.id = r.runtime_target_id
              AND rt.status = 'ready'
              AND rt.provider = 'hermes'
              AND rt.role = 'user'
              AND rt.mode = 'hermes-chat'
              AND rt.runtime_binding_id = rb.id
              AND rt.account_user_id = $8
              AND rt.profile_user_id = $9
              AND rt.entitlement_revision = $16
              AND rt.updated_at = $17
            RETURNING r.*
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(lease_token)),
            canonical_inbound_event_id,
            canonical_outbound_event_id,
            outbound_text,
            authority.runtime_binding_revision,
            authority.runtime_agent_id,
            authority.user_id,
            authority.profile_user_id,
            authority.runtime_binding_id,
            authority.runtime_target_id,
            authority.runtime_authority_digest,
            authority.uid,
            authority.runtime_instance_id,
            authority.runtime_profile_name,
            authority.runtime_target_entitlement_revision,
            authority.runtime_target_updated_at,
        )
        if not row:
            raise ImessageRuntimeRepositoryError("imessage_model_completion_conflict")
        return dict(row)

    async def fail_message(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        error_code: str,
        uncertain: bool,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE ella_imessage_message_receipts
            SET status = $3,
                error_code = $4,
                reconciliation_status = CASE WHEN $3 = 'uncertain' THEN 'manual_required' ELSE 'none' END,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND lease_token = $2
              AND status IN ('claimed', 'running')
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(lease_token)),
            "uncertain" if uncertain else "failed",
            error_code[:120],
        )

    async def start_delivery(
        self,
        *,
        receipt_id: str,
        delivery_idempotency_key: str,
        binding_id: str,
        generation: int,
        connection_ref_hmac: str,
        authority: ImessageRuntimeAuthority,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                receipt = await connection.fetchrow(
                    """
                    SELECT r.*
                    FROM ella_imessage_message_receipts r
                    JOIN ella_imessage_channel_bindings b ON b.id = r.binding_id
                    JOIN users u ON u.id = r.user_id
                    JOIN ella_imessage_consent_authority c ON c.user_id = r.user_id
                    JOIN ella_runtime_bindings rb ON rb.id = r.runtime_binding_id
                    JOIN ella_runtime_targets rt ON rt.id = r.runtime_target_id
                    WHERE r.id = $1
                      AND r.delivery_idempotency_key = $2
                      AND r.binding_id = $3
                      AND r.binding_generation = $4
                      AND r.runtime_binding_id = $8
                      AND r.runtime_target_id = $9
                      AND r.runtime_authority_digest = $10
                      AND b.status = 'active'
                      AND b.generation = r.binding_generation
                      AND b.user_id = $6
                      AND b.runtime_binding_id = $8
                      AND b.runtime_target_id = $9
                      AND b.runtime_authority_digest = $10
                      AND b.transport_connection_ref_hmac = $5
                      AND b.last_transport_healthy_at >= CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                      AND u.status = 'ACTIVE'
                      AND u.id = $6
                      AND u.omi_uid = $11
                      AND c.decision = 'granted'
                      AND c.current_receipt_id = r.consent_receipt_id
                      AND c.authority_epoch = r.consent_authority_epoch
                      AND rb.status = 'active'
                      AND rb.active = true
                      AND rb.health_state = 'healthy'
                      AND rb.role = 'user'
                      AND rb.user_id = $6
                      AND rb.account_user_id = $6
                      AND rb.profile_user_id = $7
                      AND rb.revision = $12
                      AND rb.agent_id = $13
                      AND rb.runtime_instance_id IS NOT DISTINCT FROM $14
                      AND rb.profile_name = $15
                      AND rt.status = 'ready'
                      AND rt.provider = 'hermes'
                      AND rt.role = 'user'
                      AND rt.mode = 'hermes-chat'
                      AND rt.runtime_binding_id = rb.id
                      AND rt.account_user_id = $6
                      AND rt.profile_user_id = $7
                      AND rt.entitlement_revision = $16
                      AND rt.updated_at = $17
                    FOR UPDATE OF r
                    """,
                    uuid.UUID(str(receipt_id)),
                    uuid.UUID(str(delivery_idempotency_key)),
                    uuid.UUID(str(binding_id)),
                    generation,
                    connection_ref_hmac,
                    authority.user_id,
                    authority.profile_user_id,
                    authority.runtime_binding_id,
                    authority.runtime_target_id,
                    authority.runtime_authority_digest,
                    authority.uid,
                    authority.runtime_binding_revision,
                    authority.runtime_agent_id,
                    authority.runtime_instance_id,
                    authority.runtime_profile_name,
                    authority.runtime_target_entitlement_revision,
                    authority.runtime_target_updated_at,
                )
                if not receipt:
                    raise ImessageRuntimeRepositoryError("imessage_delivery_authority_changed")
                if receipt["status"] == "sending":
                    raise ImessageRuntimeRepositoryError("imessage_delivery_outcome_uncertain")
                if receipt["status"] != "awaiting_delivery":
                    raise ImessageRuntimeRepositoryError("imessage_delivery_not_ready")
                row = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_message_receipts
                    SET status = 'sending',
                        send_started = true,
                        send_connection_ref_hmac = $2,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1 AND status = 'awaiting_delivery'
                    RETURNING *
                    """,
                    receipt["id"],
                    connection_ref_hmac,
                )
                if not row:
                    raise ImessageRuntimeRepositoryError("imessage_delivery_claim_conflict")
                return dict(row)

    async def acknowledge_delivery(
        self,
        *,
        receipt_id: str,
        delivery_idempotency_key: str,
        outbound_provider_ref_hmac: str,
        binding_generation: int,
        line_identity_hmac: str,
        contact_identity_hmac: str,
        connection_ref_hmac: str,
    ) -> dict[str, Any]:
        try:
            row = await self.pool.fetchrow(
                """
                UPDATE ella_imessage_message_receipts r
                SET status = 'delivered',
                    outbound_provider_ref_hmac = $3,
                    reconciliation_status = 'none',
                    error_code = NULL,
                    completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                FROM ella_imessage_channel_bindings b
                WHERE r.id = $1
                  AND r.delivery_idempotency_key = $2
                  AND r.binding_generation = $4
                  AND r.status IN ('sending', 'delivered', 'uncertain')
                  AND r.send_started = true
                  AND r.send_connection_ref_hmac = $7
                  AND (r.outbound_provider_ref_hmac IS NULL OR r.outbound_provider_ref_hmac = $3)
                  AND b.id = r.binding_id
                  AND b.line_identity_hmac = $5
                  AND b.contact_identity_hmac = $6
                RETURNING r.*
                """,
                uuid.UUID(str(receipt_id)),
                uuid.UUID(str(delivery_idempotency_key)),
                outbound_provider_ref_hmac,
                binding_generation,
                line_identity_hmac,
                contact_identity_hmac,
                connection_ref_hmac,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ImessageRuntimeRepositoryError("imessage_outbound_message_conflict") from exc
        if not row:
            raise ImessageRuntimeRepositoryError("imessage_delivery_ack_conflict")
        return dict(row)

    async def mark_delivery_uncertain(
        self,
        *,
        receipt_id: str,
        delivery_idempotency_key: str,
        binding_generation: int,
        line_identity_hmac: str,
        contact_identity_hmac: str,
        connection_ref_hmac: str,
        error_code: str,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_imessage_message_receipts r
            SET status = 'uncertain',
                reconciliation_status = 'manual_required',
                error_code = $7,
                updated_at = CURRENT_TIMESTAMP
            FROM ella_imessage_channel_bindings b
            WHERE r.id = $1
              AND r.delivery_idempotency_key = $2
              AND r.binding_generation = $3
              AND r.status IN ('sending', 'uncertain')
              AND r.send_started = true
              AND r.send_connection_ref_hmac = $6
              AND (r.error_code IS NULL OR r.error_code = $7)
              AND b.id = r.binding_id
              AND b.line_identity_hmac = $4
              AND b.contact_identity_hmac = $5
            RETURNING r.*
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(delivery_idempotency_key)),
            binding_generation,
            line_identity_hmac,
            contact_identity_hmac,
            connection_ref_hmac,
            error_code[:120],
        )
        if not row:
            raise ImessageRuntimeRepositoryError("imessage_delivery_uncertain_conflict")
        return dict(row)

    async def reconcile_pre_send_delivery(
        self,
        *,
        receipt_id: str,
        delivery_idempotency_key: str,
        binding_generation: int,
        line_identity_hmac: str,
        contact_identity_hmac: str,
        connection_ref_hmac: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                receipt = await connection.fetchrow(
                    """
                    SELECT r.*
                    FROM ella_imessage_message_receipts r
                    JOIN ella_imessage_channel_bindings b ON b.id = r.binding_id
                    WHERE r.id = $1
                      AND r.delivery_idempotency_key = $2
                      AND r.binding_generation = $3
                      AND b.line_identity_hmac = $4
                      AND b.contact_identity_hmac = $5
                    FOR UPDATE OF r
                    """,
                    uuid.UUID(str(receipt_id)),
                    uuid.UUID(str(delivery_idempotency_key)),
                    binding_generation,
                    line_identity_hmac,
                    contact_identity_hmac,
                )
                if not receipt:
                    raise ImessageRuntimeRepositoryError("imessage_delivery_reconcile_conflict")
                status = str(receipt["status"])
                if status in {"uncertain", "quarantined", "failed", "delivered"}:
                    return dict(receipt)
                if status == "sending":
                    if not receipt["send_started"] or receipt["send_connection_ref_hmac"] != connection_ref_hmac:
                        raise ImessageRuntimeRepositoryError("imessage_delivery_reconcile_conflict")
                    next_status = "uncertain"
                    error_code = "imessage_send_outcome_unconfirmed_after_restart"
                elif status == "awaiting_delivery" and not receipt["send_started"]:
                    next_status = "quarantined"
                    error_code = "imessage_delivery_abandoned_before_send"
                else:
                    raise ImessageRuntimeRepositoryError("imessage_delivery_reconcile_conflict")
                row = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_message_receipts
                    SET status = $2,
                        reconciliation_status = 'manual_required',
                        error_code = $3,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1 AND status = $4
                    RETURNING *
                    """,
                    receipt["id"],
                    next_status,
                    error_code,
                    status,
                )
                if not row:
                    raise ImessageRuntimeRepositoryError("imessage_delivery_reconcile_conflict")
                return dict(row)

    async def quarantine_binding(
        self,
        *,
        binding_id: str,
        generation: int,
        reason: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                binding = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_channel_bindings
                    SET status = 'quarantined',
                        revision = revision + 1,
                        transport_connection_ref_hmac = NULL,
                        transport_connected_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                      AND generation = $2
                      AND status = 'active'
                    RETURNING *
                    """,
                    uuid.UUID(str(binding_id)),
                    generation,
                )
                if not binding:
                    raise ImessageRuntimeRepositoryError("imessage_transport_deregister_conflict")
                await connection.execute(
                    """
                    UPDATE ella_imessage_message_receipts
                    SET status = CASE WHEN status = 'sending' THEN 'uncertain' ELSE 'quarantined' END,
                        reconciliation_status = 'manual_required',
                        error_code = $3,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = $1
                      AND binding_generation = $2
                      AND status IN ('claimed', 'running', 'awaiting_delivery', 'sending')
                    """,
                    binding["id"],
                    generation,
                    reason[:120],
                )
                return dict(binding)
