"""Transactional authority quarantine for resumable account deletion."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from database import authority_advisory_lock, managed_cloud_consent, voice_canary


class AccountDeletionUnavailable(RuntimeError):
    """The durable account deletion state could not be advanced safely."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AccountDeletionState:
    user_found: bool
    capacity_released: bool
    authority_quarantined: bool
    external_cleanup_required: tuple[str, ...]
    external_cleanup_references: tuple[str, ...]
    counts: dict[str, int]


def _affected(command_tag: str) -> int:
    return int(command_tag.rsplit(" ", 1)[-1])


async def quarantine_account_for_deletion(uid: str) -> AccountDeletionState:
    """Quarantine all database authority and release ordinary pilot capacity.

    The shared account/profile advisory lock is acquired before any protected
    row lock or mutation. External Hermes/Honcho artifacts are deliberately not
    reported deleted: the current self-hosted authority has no authenticated
    deprovision operation that can issue an authoritative completion receipt.
    """
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as connection:
            try:
                owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                    connection,
                    uid=uid,
                )
            except authority_advisory_lock.AuthorityLockError as exc:
                if exc.code == "authority_lock_owner_missing":
                    return AccountDeletionState(
                        user_found=False,
                        capacity_released=True,
                        authority_quarantined=True,
                        external_cleanup_required=(),
                        external_cleanup_references=(),
                        counts={},
                    )
                raise

            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                await voice_canary.lock_runtime_authority_on_connection(
                    connection,
                    uid=uid,
                )

                invitation_rows = await connection.fetch(
                    """
                    SELECT invitation.id, invitation.kind,
                           invitation.capacity_reservation_id
                    FROM ella_invitation_redemptions redemption
                    JOIN ella_invitations invitation
                      ON invitation.id = redemption.invitation_id
                    JOIN ella_invitation_capacity_reservations reservation
                      ON reservation.id = invitation.capacity_reservation_id
                    WHERE redemption.user_id = $1
                    ORDER BY invitation.id
                    FOR UPDATE OF redemption, invitation, reservation
                    """,
                    user_id,
                )

                await managed_cloud_consent._quarantine_on_connection(
                    connection,
                    uid=uid,
                    user_id=user_id,
                    reason="account_deletion_requested",
                    owner_lock=owner_lock,
                )
                consent_result = await connection.execute(
                    """
                    UPDATE ella_managed_cloud_consent_authority
                    SET decision = 'revoked',
                        consent_receipt_ref = NULL,
                        profile_binding_id = NULL,
                        policy_version = NULL,
                        processor_set_hash = NULL,
                        scope_version = NULL,
                        scope_hash = NULL,
                        authority_epoch = gen_random_uuid(),
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND decision <> 'revoked'
                    """,
                    user_id,
                )

                invitation_ids = [row["id"] for row in invitation_rows if row["kind"] == "ordinary"]
                reservation_ids = [
                    row["capacity_reservation_id"] for row in invitation_rows if row["kind"] == "ordinary"
                ]
                invitation_result = "UPDATE 0"
                capacity_result = "UPDATE 0"
                if invitation_ids:
                    invitation_result = await connection.execute(
                        """
                        UPDATE ella_invitations
                        SET state = 'revoked',
                            delivery_state = 'suppressed',
                            revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
                            version = CASE WHEN state = 'revoked' THEN version ELSE version + 1 END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ANY($1::uuid[])
                          AND state <> 'revoked'
                        """,
                        invitation_ids,
                    )
                    capacity_result = await connection.execute(
                        """
                        UPDATE ella_invitation_capacity_reservations
                        SET state = 'released',
                            released_at = COALESCE(released_at, CURRENT_TIMESTAMP),
                            version = version + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ANY($1::uuid[])
                          AND state IN ('reserved', 'consumed')
                        """,
                        reservation_ids,
                    )

                cluster_result = "UPDATE 0"
                if await connection.fetchval("SELECT to_regclass('agent_clusters') IS NOT NULL"):
                    cluster_result = await connection.execute(
                        """
                        UPDATE agent_clusters
                        SET status = 'INACTIVE', agents = '{}'::jsonb
                        WHERE user_id = $1
                          AND (status <> 'INACTIVE' OR agents <> '{}'::jsonb)
                        """,
                        user_id,
                    )

                photon_result = await connection.execute(
                    """
                    UPDATE ella_photon_channel_bindings
                    SET status = 'quarantined',
                        quarantined_at = COALESCE(quarantined_at, CURRENT_TIMESTAMP),
                        quarantine_reason = 'account_deletion_requested',
                        sidecar_connection_key = NULL,
                        sidecar_connected_at = NULL,
                        oauth_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND status <> 'quarantined'
                    """,
                    user_id,
                )

                user_result = await connection.execute(
                    """
                    UPDATE users
                    SET status = 'DELETION_PENDING',
                        email = 'deleted+' || replace(id::text, '-', '') || '@invalid.ella',
                        name = 'Deleted User',
                        identities = '{}'::jsonb,
                        settings = '{}'::jsonb,
                        tags = ARRAY[]::text[],
                        conditions = ARRAY[]::text[],
                        medications = ARRAY[]::text[],
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                      AND status <> 'DELETED'
                    """,
                    user_id,
                )

                provider_rows = await connection.fetch(
                    """
                    SELECT DISTINCT provider
                    FROM ella_runtime_bindings
                    WHERE user_id = $1
                      AND provider IN ('hermes', 'hermes_cloud')
                    ORDER BY provider
                    """,
                    user_id,
                )
                attempt_rows = await connection.fetch(
                    """
                    SELECT correlation_ref
                    FROM ella_provider_attempts
                    WHERE user_id = $1
                      AND proof_state = 'unproven'
                    ORDER BY correlation_ref
                    """,
                    user_id,
                )
                external_cleanup_required: tuple[str, ...] = ()
                if provider_rows or attempt_rows:
                    external_cleanup_required = (
                        "hermes_profile",
                        "honcho_tenancy",
                        "runtime_registry",
                    )
                external_cleanup_references = tuple(str(row["correlation_ref"]) for row in attempt_rows)

                return AccountDeletionState(
                    user_found=True,
                    capacity_released=True,
                    authority_quarantined=True,
                    external_cleanup_required=external_cleanup_required,
                    external_cleanup_references=external_cleanup_references,
                    counts={
                        "consent_authorities": _affected(consent_result),
                        "invitations": _affected(invitation_result),
                        "capacity_reservations": _affected(capacity_result),
                        "agent_clusters": _affected(cluster_result),
                        "photon_bindings": _affected(photon_result),
                        "users": _affected(user_result),
                    },
                )
    except AccountDeletionUnavailable:
        raise
    except Exception as exc:
        raise AccountDeletionUnavailable("account_deletion_authority_unavailable") from exc


async def finalize_account_deletion(uid: str) -> bool:
    """Mark a quarantined identity complete only after external state is absent."""
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as connection:
            try:
                owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                    connection,
                    uid=uid,
                )
            except authority_advisory_lock.AuthorityLockError as exc:
                if exc.code == "authority_lock_owner_missing":
                    return False
                raise
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                external_count = await connection.fetchval(
                    """
                    SELECT
                        (SELECT COUNT(*)
                         FROM ella_runtime_bindings
                         WHERE user_id = $1
                           AND provider IN ('hermes', 'hermes_cloud'))
                      + (SELECT COUNT(*)
                         FROM ella_provider_attempts
                         WHERE user_id = $1
                           AND proof_state = 'unproven')
                    """,
                    user_id,
                )
                if int(external_count or 0) != 0:
                    raise AccountDeletionUnavailable("account_deletion_external_cleanup_incomplete")
                result = await connection.execute(
                    """
                    UPDATE users
                    SET status = 'DELETED', updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1 AND status <> 'DELETED'
                    """,
                    user_id,
                )
                return _affected(result) == 1
    except AccountDeletionUnavailable:
        raise
    except Exception as exc:
        raise AccountDeletionUnavailable("account_deletion_finalize_unavailable") from exc
