"""Postgres access for isolated Ella provisioning and runtime bindings."""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from database import authority_advisory_lock
from database import voice_canary as voice_canary_db
from database.honcho_attestation import (
    ATTESTATION_VERSION,
    HonchoAttestationError,
    authority_credential,
    observed_runtime_fields,
    verify_persisted_attestation,
)
from database.runtime_targets import (
    CLOUD_RUNTIME_MODEL,
    CLOUD_RUNTIME_PROVIDER,
    CLOUD_RUNTIME_TARGET_MODES,
    RuntimeTargetLineage,
    SELF_HOSTED_RUNTIME_MODEL,
    SELF_HOSTED_RUNTIME_PROVIDER,
    SELF_HOSTED_RUNTIME_TARGET_MODES,
)

_pool: Optional[asyncpg.Pool] = None
_SELF_HOSTED_BINDING_NAMESPACE = uuid.UUID("fa9b67e0-982d-4cab-9814-d38e992ecf6a")
REQUIRED_PROVISIONING_INDEXES = (
    "ella_provisioning_jobs_user_schema_key",
    "ella_provisioning_jobs_state_updated_idx",
    "ella_runtime_bindings_profile_name_key",
    "ella_runtime_bindings_gateway_port_key",
    "ella_runtime_bindings_service_label_key",
    "ella_runtime_bindings_honcho_workspace_key",
    "ella_runtime_bindings_observed_peer_key",
    "ella_runtime_bindings_observer_peer_key",
    "ella_runtime_bindings_user_role_provider_key",
    "ella_runtime_bindings_one_active_role_key",
    "ella_runtime_bindings_user_role_active_idx",
    "ella_runtime_bindings_health_updated_idx",
)
REQUIRED_CLOUD_RUNTIME_INDEXES = (
    "users_profile_class_idx",
    "ella_runtime_bindings_runtime_instance_key",
    "ella_runtime_bindings_claim_job_key",
    "ella_runtime_bindings_claim_token_key",
    "ella_runtime_bindings_pool_lookup_idx",
    "ella_runtime_session_scopes_session_key",
    "ella_runtime_session_scopes_binding_role_channel_key",
    "ella_runtime_session_scopes_user_updated_idx",
    "ella_runtime_interactions_scope_client_key",
    "ella_runtime_interactions_hermes_session_key",
    "ella_runtime_interactions_idempotency_key",
    "ella_runtime_interactions_scope_status_idx",
    "ella_runtime_ingestion_event_revision_key",
    "ella_runtime_ingestion_status_idx",
    "ella_runtime_pool_alerts_one_pending_key",
    "ella_runtime_pool_alerts_provider_state_idx",
    "ella_photon_channel_bindings_runtime_key",
    "ella_photon_channel_bindings_identity_key",
    "ella_photon_channel_bindings_one_owner_key",
    "ella_photon_channel_bindings_status_idx",
    "ella_photon_message_receipts_inbound_key",
    "ella_photon_message_receipts_outbound_key",
    "ella_photon_message_receipts_delivery_key",
    "ella_photon_message_receipts_status_idx",
    "ella_runtime_bindings_account_profile_idx",
    "ella_runtime_targets_active_cloud_mode_key",
    "ella_runtime_targets_active_retained_key",
    "ella_runtime_targets_binding_idx",
)
CLOUD_RUNTIME_TABLES = (
    "voice_entitlements",
    "ella_runtime_session_scopes",
    "ella_runtime_interactions",
    "ella_runtime_ingestion_receipts",
    "ella_runtime_pool_alerts",
    "ella_photon_channel_bindings",
    "ella_photon_message_receipts",
    "ella_photon_quota_buckets",
    "ella_runtime_targets",
)
REQUIRED_CLOUD_RUNTIME_BINDING_COLUMNS = (
    "status",
    "runtime_instance_id",
    "api_base_url_ref",
    "api_key_ref",
    "honcho_api_key_ref",
    "prompt_pack_version",
    "prompt_artifact_receipt",
    "expected_model",
    "allowed_tools",
    "required_capabilities",
    "claim_job_id",
    "claim_token",
    "claim_lease_expires_at",
    "claimed_at",
    "disabled_at",
    "quarantined_at",
    "quarantine_reason",
    "account_user_id",
    "profile_user_id",
    "runtime_target_mode",
    "target_endpoint_ref",
    "target_credential_ref",
)
REQUIRED_CLOUD_PROVISIONING_JOB_COLUMNS = (
    "external_side_effects",
    "rollback_receipt",
    "manual_intervention_at",
)
REQUIRED_CLOUD_USER_COLUMNS = ("profile_class",)
REQUIRED_CLOUD_VOICE_ENTITLEMENT_COLUMNS = (
    "consent_policy_version",
    "consent_processor_set_hash",
    "consent_scope_version",
    "consent_scope_hash",
)
REQUIRED_CLOUD_RUNTIME_CONSTRAINTS = (
    "ella_runtime_bindings_claim_job_id_fkey",
    "ella_runtime_bindings_status_check",
    "ella_runtime_bindings_cloud_pool_shape_check",
    "ella_runtime_bindings_account_user_id_fkey",
    "ella_runtime_bindings_profile_user_id_fkey",
    "ella_runtime_bindings_cloud_target_shape_check",
    "voice_entitlements_invitation_consent_lineage_check",
    "users_profile_class_check",
)
REQUIRED_SELF_HOSTED_INVITE_COLUMNS = (
    ("ella_invitations", "allowed_email_hash"),
    ("ella_invitation_redemptions", "consent_pending"),
    ("ella_invitation_redemptions", "user_id"),
    ("ella_invitation_redemptions", "user_mapping_state"),
    ("ella_invitation_targets", "revoked_at"),
    ("voice_entitlements", "invitation_consent_pending"),
    ("ella_runtime_targets", "invitation_target_id"),
)


def deterministic_runtime_binding_id(*, uid: str, provider: str, role: str) -> str:
    """Return one stable, non-authorizing binding identity for retry reconciliation."""
    parts = (str(uid), str(provider), str(role))
    if not all(parts):
        raise ValueError("runtime_binding_identity_incomplete")
    return str(uuid.uuid5(_SELF_HOSTED_BINDING_NAMESPACE, "\0".join(parts)))


REQUIRED_SELF_HOSTED_INVITE_CONSTRAINTS = (
    "ella_invitation_targets_required_profile_class_check",
    "ella_invitation_redemptions_consent_shape_check",
    "ella_runtime_targets_provider_check",
    "ella_runtime_targets_status_check",
    "ella_runtime_targets_shape_check",
    "voice_entitlements_invitation_authority_epoch_check",
)
REQUIRED_SELF_HOSTED_INVITE_INDEXES = (
    "ella_runtime_targets_invitation_target_key",
    "ella_runtime_targets_active_hermes_profile_key",
)


class IdentityConflictError(RuntimeError):
    """The verified Firebase identity conflicts with an existing Ella user."""


class ProvisioningSchemaNotReadyError(RuntimeError):
    """The shared Ella provisioning migration has not been fully applied."""

    def __init__(self, missing: list[str]):
        self.missing = tuple(missing)
        super().__init__("provisioning_schema_not_ready")


class RuntimePoolClaimError(RuntimeError):
    """A warm runtime claim no longer has an unambiguous owner."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def invalidate_self_hosted_authority_on_connection(
    connection: asyncpg.Connection,
    *,
    uid: str,
    user_id: uuid.UUID,
    reason: str,
    owner_lock: authority_advisory_lock.AuthorityLockProof,
    invitation_id: Optional[uuid.UUID] = None,
) -> dict[str, int]:
    """Atomically make an invitation-owned local Hermes runtime unusable."""
    await authority_advisory_lock.require_self_owner_lock(
        connection,
        owner_lock,
        user_id=user_id,
    )
    if not await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE id = $1 AND omi_uid = $2)",
        user_id,
        uid,
    ):
        raise RuntimePoolClaimError("self_hosted_authority_owner_drift")

    entitlement_rows = await connection.fetch(
        """
        UPDATE voice_entitlements entitlement
        SET status = 'revoked',
            revision = revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE entitlement.uid = $1
          AND entitlement.status <> 'revoked'
          AND ($3::uuid IS NULL OR entitlement.invitation_id = $3)
          AND EXISTS (
              SELECT 1
              FROM ella_invitation_redemptions redemption
              JOIN ella_runtime_targets target
                ON target.invitation_target_id = redemption.invitation_target_id
               AND target.provider = 'hermes'
              WHERE redemption.user_id = $2
                AND redemption.invitation_id = entitlement.invitation_id
          )
        RETURNING entitlement.invitation_id
        """,
        uid,
        user_id,
        invitation_id,
    )
    session_result = await connection.execute(
        "DELETE FROM voice_active_sessions WHERE uid = $1",
        uid,
    )
    target_rows = await connection.fetch(
        """
        UPDATE ella_runtime_targets target
        SET status = 'revoked',
            revoked_at = COALESCE(target.revoked_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        FROM ella_invitation_redemptions redemption
        WHERE target.invitation_target_id = redemption.invitation_target_id
          AND redemption.user_id = $1
          AND ($2::uuid IS NULL OR redemption.invitation_id = $2)
          AND target.provider = 'hermes'
          AND target.status IN ('reserved', 'ready')
        RETURNING target.runtime_binding_id
        """,
        user_id,
        invitation_id,
    )
    invitation_target_rows = await connection.fetch(
        """
        UPDATE ella_invitation_targets invitation_target
        SET revoked_at = COALESCE(invitation_target.revoked_at, CURRENT_TIMESTAMP)
        FROM ella_invitation_redemptions redemption
        WHERE invitation_target.id = redemption.invitation_target_id
          AND redemption.user_id = $1
          AND ($2::uuid IS NULL OR redemption.invitation_id = $2)
          AND invitation_target.revoked_at IS NULL
        RETURNING invitation_target.id
        """,
        user_id,
        invitation_id,
    )
    runtime_session_result = await connection.execute(
        """
        DELETE FROM ella_runtime_session_scopes scope
        USING ella_runtime_bindings binding
        WHERE scope.binding_id = binding.id
          AND binding.user_id = $1
          AND binding.provider = 'hermes'
        """,
        user_id,
    )
    binding_rows = await connection.fetch(
        """
        UPDATE ella_runtime_bindings
        SET status = 'disabled',
            active = false,
            health_state = 'unhealthy',
            health_receipt = $2::jsonb,
            disabled_at = COALESCE(disabled_at, CURRENT_TIMESTAMP),
            quarantine_reason = $3,
            revision = revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = $1
          AND provider = 'hermes'
          AND (active = true OR status <> 'disabled' OR health_state <> 'unhealthy')
        RETURNING id
        """,
        user_id,
        json.dumps({"content_free": True, "reason": reason}),
        reason,
    )
    job_rows = await connection.fetch(
        """
        UPDATE ella_provisioning_jobs
        SET state = 'blocked',
            stage = 'runtime_ready',
            retryable = false,
            error_code = 'invitation_authority_revoked',
            error_detail = $2::jsonb,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = $1
          AND state NOT IN ('blocked', 'rolling_back', 'manual_intervention')
        RETURNING id
        """,
        user_id,
        json.dumps({"content_free": True, "reason": reason}),
    )

    def affected(command_tag: str) -> int:
        return int(command_tag.rsplit(" ", 1)[-1])

    return {
        "entitlements": len(entitlement_rows),
        "voice_sessions": affected(session_result),
        "runtime_targets": len(target_rows),
        "invitation_targets": len(invitation_target_rows),
        "runtime_sessions": affected(runtime_session_result),
        "bindings": len(binding_rows),
        "jobs": len(job_rows),
    }


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
            database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
            min_size=1,
            max_size=10,
        )
    return _pool


def _row_dict(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def _json_object(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return dict(decoded) if isinstance(decoded, dict) else decoded
    return value


class EllaProvisioningRepository:
    def __init__(self, pool: asyncpg.Pool, *, firestore_db: Any = None):
        self.pool = pool
        self.firestore_db = firestore_db

    @classmethod
    async def create(cls, *, firestore_db: Any = None) -> "EllaProvisioningRepository":
        return cls(await get_pool(), firestore_db=firestore_db)

    async def assert_schema_ready(self) -> None:
        """Fail before identity writes when the shared Prisma migration is absent."""
        row = await self.pool.fetchrow(
            """
            SELECT
                to_regclass('public.ella_provisioning_jobs') IS NOT NULL AS jobs_table,
                to_regclass('public.ella_runtime_bindings') IS NOT NULL AS bindings_table,
                ARRAY(
                    SELECT required.index_name
                    FROM unnest($1::text[]) AS required(index_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND indexname = required.index_name
                    )
                    ORDER BY required.index_name
                ) AS missing_indexes
            """,
            list(REQUIRED_PROVISIONING_INDEXES),
        )
        missing = []
        if not row or not row["jobs_table"]:
            missing.append("table:ella_provisioning_jobs")
        if not row or not row["bindings_table"]:
            missing.append("table:ella_runtime_bindings")
        if row:
            missing.extend(f"index:{name}" for name in (row["missing_indexes"] or []))
        if missing:
            raise ProvisioningSchemaNotReadyError(missing)

    async def assert_cloud_schema_ready(self) -> None:
        """Fail before cloud claims when the pool/session migration is absent."""
        row = await self.pool.fetchrow(
            """
            SELECT
                ARRAY(
                    SELECT required.table_name
                    FROM unnest($1::text[]) AS required(table_name)
                    WHERE to_regclass('public.' || required.table_name) IS NULL
                    ORDER BY required.table_name
                ) AS missing_tables,
                ARRAY(
                    SELECT required.index_name
                    FROM unnest($2::text[]) AS required(index_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND indexname = required.index_name
                    )
                    ORDER BY required.index_name
                ) AS missing_indexes,
                ARRAY(
                    SELECT required.column_name
                    FROM unnest($3::text[]) AS required(column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'ella_runtime_bindings'
                          AND column_name = required.column_name
                    )
                    ORDER BY required.column_name
                ) AS missing_columns,
                ARRAY(
                    SELECT required.column_name
                    FROM unnest($5::text[]) AS required(column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'ella_provisioning_jobs'
                          AND column_name = required.column_name
                    )
                    ORDER BY required.column_name
                ) AS missing_job_columns,
                ARRAY(
                    SELECT required.constraint_name
                    FROM unnest($4::text[]) AS required(constraint_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE connamespace = 'public'::regnamespace
                          AND conname = required.constraint_name
                    )
                    ORDER BY required.constraint_name
                ) AS missing_constraints,
                ARRAY(
                    SELECT required.column_name
                    FROM unnest($6::text[]) AS required(column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'users'
                          AND column_name = required.column_name
                    )
                    ORDER BY required.column_name
                ) AS missing_user_columns,
                ARRAY(
                    SELECT required.column_name
                    FROM unnest($7::text[]) AS required(column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'voice_entitlements'
                          AND column_name = required.column_name
                    )
                    ORDER BY required.column_name
                ) AS missing_voice_entitlement_columns,
                (
                    SELECT is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'ella_runtime_bindings'
                      AND column_name = 'user_id'
                ) AS binding_user_nullable
            """,
            list(CLOUD_RUNTIME_TABLES),
            list(REQUIRED_CLOUD_RUNTIME_INDEXES),
            list(REQUIRED_CLOUD_RUNTIME_BINDING_COLUMNS),
            list(REQUIRED_CLOUD_RUNTIME_CONSTRAINTS),
            list(REQUIRED_CLOUD_PROVISIONING_JOB_COLUMNS),
            list(REQUIRED_CLOUD_USER_COLUMNS),
            list(REQUIRED_CLOUD_VOICE_ENTITLEMENT_COLUMNS),
        )
        missing: list[str] = []
        if row:
            missing.extend(f"table:{name}" for name in (row["missing_tables"] or []))
            missing.extend(f"index:{name}" for name in (row["missing_indexes"] or []))
            missing.extend(f"column:ella_runtime_bindings.{name}" for name in (row["missing_columns"] or []))
            missing.extend(f"column:ella_provisioning_jobs.{name}" for name in (row["missing_job_columns"] or []))
            missing.extend(f"column:users.{name}" for name in (row["missing_user_columns"] or []))
            missing.extend(
                f"column:voice_entitlements.{name}" for name in (row["missing_voice_entitlement_columns"] or [])
            )
            missing.extend(f"constraint:{name}" for name in (row["missing_constraints"] or []))
            if row["binding_user_nullable"] != "YES":
                missing.append("column:ella_runtime_bindings.user_id_nullable")
        else:
            missing.append("cloud_runtime_schema_probe")
        if missing:
            raise ProvisioningSchemaNotReadyError(missing)

    async def assert_self_hosted_invite_schema_ready(self) -> None:
        """Require migration 015 before any invited self-hosted side effect."""
        row = await self.pool.fetchrow(
            """
            SELECT
                ARRAY(
                    SELECT required.table_name || '.' || required.column_name
                    FROM unnest($1::text[], $2::text[])
                        AS required(table_name, column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = required.table_name
                          AND column_name = required.column_name
                    )
                    ORDER BY required.table_name, required.column_name
                ) AS missing_columns,
                ARRAY(
                    SELECT required.constraint_name
                    FROM unnest($3::text[]) AS required(constraint_name)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE connamespace = current_schema()::regnamespace
                          AND conname = required.constraint_name
                    )
                    ORDER BY required.constraint_name
                ) AS missing_constraints,
                ARRAY(
                    SELECT required.index_name
                    FROM unnest($4::text[]) AS required(index_name)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND indexname = required.index_name
                    )
                    ORDER BY required.index_name
                ) AS missing_indexes
            """,
            [table for table, _column in REQUIRED_SELF_HOSTED_INVITE_COLUMNS],
            [column for _table, column in REQUIRED_SELF_HOSTED_INVITE_COLUMNS],
            list(REQUIRED_SELF_HOSTED_INVITE_CONSTRAINTS),
            list(REQUIRED_SELF_HOSTED_INVITE_INDEXES),
        )
        missing: list[str] = []
        if not row:
            missing.append("self_hosted_invite_schema_probe")
        else:
            missing.extend(f"column:{value}" for value in (row["missing_columns"] or []))
            missing.extend(f"constraint:{value}" for value in (row["missing_constraints"] or []))
            missing.extend(f"index:{value}" for value in (row["missing_indexes"] or []))
        if missing:
            raise ProvisioningSchemaNotReadyError(missing)

    async def get_self_hosted_invitation_admission(self, uid: str) -> Optional[dict[str, Any]]:
        """Return one consent-complete invitation target for local Hermes."""
        row = await self.pool.fetchrow(
            """
            SELECT
                entitlement.*,
                target.id AS runtime_target_id,
                target.invitation_target_id AS attestation_runtime_target_id,
                target.status AS runtime_target_status,
                target.entitlement_revision AS runtime_target_entitlement_revision,
                authority.decision AS consent_decision,
                authority.authority_epoch AS current_authority_epoch,
                app_user.id AS user_id,
                app_user.omi_uid,
                app_user.profile_class
            FROM users app_user
            JOIN voice_entitlements entitlement ON entitlement.uid = app_user.omi_uid
            JOIN ella_invitation_redemptions redemption
              ON redemption.invitation_id = entitlement.invitation_id
             AND redemption.user_id = app_user.id
             AND redemption.user_mapping_state = 'mapped'
            JOIN ella_invitations invitation
              ON invitation.id = redemption.invitation_id
            JOIN ella_invitation_targets invitation_target
              ON invitation_target.id = redemption.invitation_target_id
             AND invitation_target.invitation_id = invitation.id
            JOIN ella_runtime_targets target
              ON target.invitation_target_id = redemption.invitation_target_id
             AND target.account_user_id = app_user.id
             AND target.profile_user_id = app_user.id
             AND target.mode = 'hermes-chat'
            JOIN ella_runtime_targets voice_target
              ON voice_target.invitation_target_id = redemption.invitation_target_id
             AND voice_target.account_user_id = app_user.id
             AND voice_target.profile_user_id = app_user.id
             AND voice_target.mode = 'hermes-voice'
            JOIN ella_managed_cloud_consent_authority authority
              ON authority.user_id = app_user.id
            WHERE app_user.omi_uid = $1
              AND app_user.profile_class = 'real'
              AND invitation.delivery_state = 'sent'
              AND (
                    (invitation.kind = 'ordinary' AND invitation.state = 'redeemed')
                    OR (invitation.kind = 'app_review' AND invitation.state = 'sent')
                  )
              AND invitation_target.required_profile_class = 'real'
              AND invitation_target.consumed_at IS NOT NULL
              AND invitation_target.revoked_at IS NULL
              AND entitlement.status IN ('invited', 'active')
              AND entitlement.invitation_consent_pending = FALSE
              AND entitlement.consent_authority_epoch = authority.authority_epoch
              AND authority.decision = 'granted'
              AND authority.consent_receipt_ref IS NOT NULL
              AND authority.profile_binding_id IS NOT NULL
              AND redemption.consent_pending = FALSE
              AND target.provider = $2
              AND target.status IN ('reserved', 'ready')
              AND target.entitlement_revision = entitlement.revision
              AND target.policy_version = entitlement.consent_policy_version
              AND target.processor_set_hash = entitlement.consent_processor_set_hash
              AND target.scope_version = entitlement.consent_scope_version
              AND target.scope_hash = entitlement.consent_scope_hash
              AND voice_target.role = target.role
              AND voice_target.provider = target.provider
              AND voice_target.status = target.status
              AND voice_target.runtime_binding_id IS NOT DISTINCT FROM target.runtime_binding_id
              AND voice_target.entitlement_revision = target.entitlement_revision
              AND voice_target.policy_version = target.policy_version
              AND voice_target.processor_set_hash = target.processor_set_hash
              AND voice_target.scope_version = target.scope_version
              AND voice_target.scope_hash = target.scope_hash
              AND authority.policy_version = entitlement.consent_policy_version
              AND authority.processor_set_hash = entitlement.consent_processor_set_hash
              AND authority.scope_version = entitlement.consent_scope_version
              AND authority.scope_hash = entitlement.consent_scope_hash
              AND invitation.required_consent_policy_version = entitlement.consent_policy_version
              AND invitation.required_consent_processor_set_hash = entitlement.consent_processor_set_hash
              AND invitation.required_consent_scope_version = entitlement.consent_scope_version
              AND invitation.required_consent_scope_hash = entitlement.consent_scope_hash
              AND entitlement.provider_allowlist = ARRAY['hermes']::text[]
              AND entitlement.model_allowlist = ARRAY[$3]::text[]
              AND entitlement.mode_allowlist = $4::text[]
              AND entitlement.fallback_policy = '{"enabled":false,"order":[]}'::jsonb
            """,
            uid,
            SELF_HOSTED_RUNTIME_PROVIDER,
            SELF_HOSTED_RUNTIME_MODEL,
            list(SELF_HOSTED_RUNTIME_TARGET_MODES),
        )
        return _row_dict(row)

    async def has_invitation_owned_self_hosted_runtime(self, uid: str) -> bool:
        """Return sticky invitation ownership even after authority or owner drift."""
        return bool(
            await self.pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users app_user
                    JOIN ella_invitation_redemptions redemption
                      ON redemption.user_id = app_user.id
                     AND redemption.user_mapping_state = 'mapped'
                    JOIN ella_runtime_targets target
                      ON target.invitation_target_id = redemption.invitation_target_id
                     AND target.provider = $2
                    WHERE app_user.omi_uid = $1
                )
                """,
                uid,
                SELF_HOSTED_RUNTIME_PROVIDER,
            )
        )

    async def ensure_user_identity(
        self,
        *,
        uid: str,
        email: str,
        name: str,
        timezone_name: str,
    ) -> dict[str, Any]:
        """Bind a verified Firebase UID to exactly one Ella user row."""
        if not uid:
            raise ValueError("uid required")
        if not email:
            raise IdentityConflictError("identity_missing_email")

        async with self.pool.acquire() as connection:
            resolution = await authority_advisory_lock.resolve_identity_owner_unlocked(
                connection,
                uid=uid,
                email=email,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=resolution.owner,
                )
                identity_rows = await authority_advisory_lock.verify_identity_owner_after_lock(
                    connection,
                    uid=uid,
                    email=email,
                    resolution=resolution,
                    proof=owner_lock,
                )
                by_uid = next((row for row in identity_rows if row["omi_uid"] == uid), None)
                if by_uid:
                    if str(by_uid["email"]).lower() != email.lower():
                        raise IdentityConflictError("uid_email_mismatch")
                    updated = await connection.fetchrow(
                        """
                        UPDATE users
                        SET name = $2,
                            timezone = $3,
                            identities = COALESCE(identities, '{}'::jsonb)
                                || jsonb_build_object('omi_uid', $1::text, 'email', email),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE omi_uid = $1
                        RETURNING id, omi_uid, email, name, timezone, status
                        """,
                        uid,
                        name,
                        timezone_name,
                    )
                    return dict(updated)

                by_email = next(
                    (row for row in identity_rows if str(row["email"] or "").lower() == email.lower()),
                    None,
                )
                if by_email:
                    existing_uid = by_email["omi_uid"]
                    if existing_uid and existing_uid != uid:
                        raise IdentityConflictError("email_owned_by_different_uid")
                    updated = await connection.fetchrow(
                        """
                        UPDATE users
                        SET omi_uid = $1,
                            name = $2,
                            timezone = $3,
                            identities = COALESCE(identities, '{}'::jsonb)
                                || jsonb_build_object('omi_uid', $1::text, 'email', email),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $4
                        RETURNING id, omi_uid, email, name, timezone, status
                        """,
                        uid,
                        name,
                        timezone_name,
                        by_email["id"],
                    )
                    return dict(updated)

                inserted = await connection.fetchrow(
                    """
                    INSERT INTO users (
                        id, email, name, timezone, omi_uid, status,
                        identities, settings, tags, updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, 'PENDING',
                        jsonb_build_object('omi_uid', $5::text, 'email', $2::text),
                        '{}'::jsonb, ARRAY[]::text[], CURRENT_TIMESTAMP
                    )
                    RETURNING id, omi_uid, email, name, timezone, status
                    """,
                    resolution.owner.account_id,
                    email,
                    name,
                    timezone_name,
                    uid,
                )
                return dict(inserted)

    async def get_user_identity(self, uid: str) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT id, omi_uid, email, name, timezone, status, profile_class
            FROM users
            WHERE omi_uid = $1
            """,
            uid,
        )
        return _row_dict(row)

    async def ensure_omi_user_document(
        self,
        *,
        uid: str,
        email: str,
        name: str,
        timezone_name: str,
        private_cloud_sync_default: bool = False,
    ) -> bool:
        """Create or repair the upstream OMI identity with explicit consent defaults."""

        if self.firestore_db is None:
            raise RuntimeError("firestore_client_unavailable")

        def _ensure() -> bool:
            user_ref = self.firestore_db.collection("users").document(uid)
            snapshot = user_ref.get()
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                missing_defaults = {
                    key: value
                    for key, value in {
                        "uid": uid,
                        "email": email,
                        "name": name,
                        "time_zone": timezone_name,
                        "private_cloud_sync_enabled": private_cloud_sync_default,
                        "store_recording_permission": False,
                    }.items()
                    if key not in data
                }
                if missing_defaults:
                    missing_defaults["updated_at"] = datetime.now(timezone.utc)
                    user_ref.set(missing_defaults, merge=True)
                    return True
                return False
            now = datetime.now(timezone.utc)
            user_ref.set(
                {
                    "uid": uid,
                    "email": email,
                    "name": name,
                    "time_zone": timezone_name,
                    "created_at": now,
                    "updated_at": now,
                    "private_cloud_sync_enabled": private_cloud_sync_default,
                    "store_recording_permission": False,
                },
                merge=False,
            )
            return True

        return await asyncio.to_thread(_ensure)

    async def acquire_job(
        self,
        *,
        uid: str,
        target_schema_version: str,
        client_request_id: Optional[str],
        request_payload_hash: str,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            INSERT INTO ella_provisioning_jobs (
                id, user_id, target_schema_version, client_request_id,
                request_payload_hash, state, stage, retryable
            )
            SELECT $1, u.id, $3, $4, $5, 'pending', 'identity_ready', true
            FROM users u
            WHERE u.omi_uid = $2
            ON CONFLICT (user_id, target_schema_version) DO UPDATE
            SET client_request_id = EXCLUDED.client_request_id
            RETURNING *
            """,
            uuid.uuid4(),
            uid,
            target_schema_version,
            client_request_id,
            request_payload_hash,
        )
        if not row:
            raise LookupError("user_not_found")
        return dict(row)

    async def get_job(self, uid: str, target_schema_version: str) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT j.*
            FROM ella_provisioning_jobs j
            JOIN users u ON u.id = j.user_id
            WHERE u.omi_uid = $1 AND j.target_schema_version = $2
            """,
            uid,
            target_schema_version,
        )
        return _row_dict(row)

    async def claim_job(self, job_id: str) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_provisioning_jobs
            SET state = 'provisioning',
                stage = 'profile_ready',
                attempts = attempts + 1,
                retryable = true,
                error_code = NULL,
                error_detail = '{}'::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND state NOT IN ('ready', 'blocked', 'rolling_back', 'manual_intervention')
              AND (
                    state NOT IN ('provisioning', 'retryable')
                    OR updated_at < CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                  )
            RETURNING *
            """,
            uuid.UUID(str(job_id)),
        )
        return _row_dict(row)

    async def get_cloud_pool_admission_policy(self) -> Optional[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT expected_model, model_policy_version
            FROM ella_runtime_bindings
            WHERE provider = 'hermes_cloud'
              AND status = 'pool_available'
              AND health_state = 'healthy'
              AND active = false
              AND user_id IS NULL
            """
        )
        if not rows:
            return None
        policies = {(str(row["expected_model"] or ""), str(row["model_policy_version"] or "")) for row in rows}
        if len(policies) != 1 or not all(next(iter(policies))):
            raise RuntimePoolClaimError("runtime_pool_policy_ambiguous")
        model, policy = next(iter(policies))
        return {"provider": "hermes_cloud", "model": model, "model_policy_version": policy}

    async def get_cloud_profile_class(self, uid: str) -> str:
        row = await self.pool.fetchrow(
            """
            SELECT profile_class
            FROM users
            WHERE omi_uid = $1
            """,
            uid,
        )
        if not row:
            raise RuntimePoolClaimError("cloud_profile_class_missing")
        profile_class = str(row["profile_class"] or "").strip().lower()
        if profile_class not in {"real", "synthetic"}:
            raise RuntimePoolClaimError("cloud_profile_class_invalid")
        return profile_class

    async def register_cloud_pool_binding(
        self,
        *,
        runtime_instance_id: str,
        profile_name: str,
        agent_id: str,
        api_base_url_ref: str,
        api_key_ref: str,
        template_version: str,
        prompt_pack_version: str,
        prompt_artifact_receipt: dict[str, Any],
        model_policy_version: str,
        voice_policy_version: str,
        expected_model: str,
        allowed_tools: list[str],
        required_capabilities: list[str],
        health_receipt: dict[str, Any],
        honcho_api_key_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        """Publish a preflighted instance into the unbound warm pool."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO ella_runtime_bindings (
                id, user_id, role, provider, status, profile_name, agent_id,
                workspace_root, internal_gateway_url, gateway_port, service_label,
                credential_ref, runtime_instance_id, api_base_url_ref, api_key_ref,
                honcho_api_key_ref, honcho_workspace, observed_peer, observer_peer,
                template_version, prompt_pack_version, prompt_artifact_receipt, model_policy_version,
                voice_policy_version, expected_model, allowed_tools,
                required_capabilities, health_state, health_receipt, revision, active
            )
            VALUES (
                $1, NULL, 'user', 'hermes_cloud', 'pool_available', $3, $4,
                NULL, NULL, NULL, NULL, NULL, $2, $5, $6, $7,
                NULL, NULL, NULL, $8, $9, $10::jsonb, $11, $12, $13, $14::jsonb,
                $15::jsonb, 'healthy', $16::jsonb, 1, false
            )
            ON CONFLICT (runtime_instance_id)
            WHERE runtime_instance_id IS NOT NULL
            DO NOTHING
            RETURNING *
            """,
            uuid.uuid4(),
            runtime_instance_id,
            profile_name,
            agent_id,
            api_base_url_ref,
            api_key_ref,
            None,
            template_version,
            prompt_pack_version,
            json.dumps(prompt_artifact_receipt),
            model_policy_version,
            voice_policy_version,
            expected_model,
            json.dumps(sorted(set(allowed_tools))),
            json.dumps(sorted(set(required_capabilities))),
            json.dumps(health_receipt),
        )
        if row:
            result = dict(row)
            result["_registration_idempotent"] = False
            return result
        existing = await self.pool.fetchrow(
            """
            SELECT *
            FROM ella_runtime_bindings
            WHERE provider = 'hermes_cloud' AND runtime_instance_id = $1
            """,
            runtime_instance_id,
        )
        if not existing:
            raise RuntimePoolClaimError("runtime_pool_registration_lost")
        if existing["status"] != "pool_available" or existing["user_id"] is not None:
            raise RuntimePoolClaimError("runtime_instance_already_claimed")
        result = dict(existing)
        result["_registration_idempotent"] = True
        return result

    async def cleanup_cloud_pool_binding(
        self,
        *,
        binding_id: str,
        runtime_instance_id: str,
    ) -> dict[str, Any]:
        """Delete exactly one still-unclaimed pool row for operator rollback."""
        row = await self.pool.fetchrow(
            """
            DELETE FROM ella_runtime_bindings
            WHERE id = $1
              AND runtime_instance_id = $2
              AND provider = 'hermes_cloud'
              AND status = 'pool_available'
              AND user_id IS NULL
              AND active = false
            RETURNING id, runtime_instance_id
            """,
            uuid.UUID(str(binding_id)),
            runtime_instance_id,
        )
        if not row:
            raise RuntimePoolClaimError("runtime_pool_cleanup_refused")
        return dict(row)

    async def list_cloud_pool_bindings(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, runtime_instance_id, status, health_state, expected_model,
                   prompt_pack_version, revision, claimed_at, quarantined_at,
                   quarantine_reason, created_at, updated_at
            FROM ella_runtime_bindings
            WHERE provider = 'hermes_cloud'
            ORDER BY created_at ASC, id ASC
            """
        )
        return [dict(row) for row in rows]

    async def claim_cloud_pool_binding(
        self,
        *,
        uid: str,
        job_id: str,
        lease_seconds: int,
        admitted_entitlement_revision: int,
        provider: str,
        model: str,
        required_profile_class: str,
    ) -> Optional[dict[str, Any]]:
        """Atomically reserve one healthy unbound Hermes Cloud instance.

        A reconnect for the same provisioning receipt gets the same claim.
        Claims are never recycled automatically after external side effects.
        """
        claim_token = uuid.uuid4()
        if required_profile_class not in {"real", "synthetic"}:
            raise RuntimePoolClaimError("cloud_profile_class_invalid")
        lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        job_uuid = uuid.UUID(str(job_id))
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                admission = await voice_canary_db.revalidate_runtime_activation_on_connection(
                    connection,
                    uid=uid,
                    admitted_entitlement_revision=admitted_entitlement_revision,
                    provider=provider,
                    model=model,
                )
                if not admission.allowed:
                    raise RuntimePoolClaimError(f"runtime_admission_{admission.code}")
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                existing = await connection.fetchrow(
                    """
                    SELECT b.*, u.omi_uid, u.profile_class
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                      AND b.provider = 'hermes_cloud'
                      AND b.claim_job_id = $2
                      AND b.status IN ('claiming', 'shadow', 'internal_canary', 'active')
                      AND u.profile_class = $3
                    FOR UPDATE
                    """,
                    uid,
                    job_uuid,
                    required_profile_class,
                )
                if existing:
                    return dict(existing)

                user_row = await connection.fetchrow(
                    "SELECT id, profile_class FROM users WHERE omi_uid = $1 FOR UPDATE",
                    uid,
                )
                if not user_row:
                    raise LookupError("user_not_found")
                if str(user_row["profile_class"] or "").strip().lower() != required_profile_class:
                    raise RuntimePoolClaimError("hermes_cloud_synthetic_profile_required")

                candidate = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_runtime_bindings
                    WHERE provider = 'hermes_cloud'
                      AND status = 'pool_available'
                      AND health_state = 'healthy'
                      AND active = false
                      AND user_id IS NULL
                      AND runtime_instance_id IS NOT NULL
                      AND api_base_url_ref IS NOT NULL
                      AND api_key_ref IS NOT NULL
                      AND honcho_api_key_ref IS NULL
                      AND prompt_artifact_receipt <> '{}'::jsonb
                      AND expected_model IS NOT NULL
                      AND jsonb_typeof(allowed_tools) = 'array'
                      AND jsonb_typeof(required_capabilities) = 'array'
                      AND (
                            health_receipt->'staged_attestation' IS NULL
                            OR (
                                health_receipt #>> '{staged_attestation,uid}' = $1
                                AND health_receipt #>> '{staged_attestation,account_id}' = $2
                                AND health_receipt #>> '{staged_attestation,profile_id}' = $2
                            )
                      )
                    ORDER BY updated_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    uid,
                    str(user_row["id"]),
                )
                if not candidate:
                    return None

                claimed = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings
                    SET user_id = $2,
                        claim_job_id = $3,
                        claim_token = $4,
                        claim_lease_expires_at = $5,
                        status = 'claiming',
                        health_state = 'pending',
                        health_receipt = CASE
                            WHEN health_receipt->'staged_attestation' IS NOT NULL
                            THEN jsonb_build_object(
                                'staged_attestation',
                                health_receipt->'staged_attestation'
                            )
                            ELSE '{}'::jsonb
                        END,
                        quarantine_reason = NULL,
                        quarantined_at = NULL,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                      AND status = 'pool_available'
                      AND user_id IS NULL
                    RETURNING *
                    """,
                    candidate["id"],
                    user_row["id"],
                    job_uuid,
                    claim_token,
                    lease_expires_at,
                )
                if not claimed:
                    return None
                result = dict(claimed)
                result["omi_uid"] = uid
                result["profile_class"] = required_profile_class
                return result

    async def finalize_cloud_pool_claim(
        self,
        *,
        uid: str,
        job_id: str,
        claim_token: str,
        admitted_entitlement_revision: int,
        authority_lineage: RuntimeTargetLineage,
        health_receipt: dict[str, Any],
        status: str = "internal_canary",
        honcho_workspace: Optional[str] = None,
        observed_peer: Optional[str] = None,
        observer_peer: Optional[str] = None,
        mode: str = "hermes-cloud-chat",
        provider: str = CLOUD_RUNTIME_PROVIDER,
        model: str = CLOUD_RUNTIME_MODEL,
    ) -> dict[str, Any]:
        if status not in {"shadow", "internal_canary", "active"}:
            raise ValueError("invalid_cloud_binding_status")
        if mode not in CLOUD_RUNTIME_TARGET_MODES:
            raise ValueError("invalid_cloud_target_mode")
        if provider != CLOUD_RUNTIME_PROVIDER or model != CLOUD_RUNTIME_MODEL:
            raise ValueError("invalid_cloud_runtime_policy")
        lineage = authority_lineage.validate()
        if RuntimeTargetLineage.from_mapping(health_receipt) != lineage or int(
            health_receipt.get("admission_revision") or 0
        ) != int(admitted_entitlement_revision):
            raise RuntimePoolClaimError("runtime_cloud_target_lineage_mismatch")
        job_uuid = uuid.UUID(str(job_id))
        token_uuid = uuid.UUID(str(claim_token))
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                admission = await voice_canary_db.revalidate_runtime_activation_on_connection(
                    connection,
                    uid=uid,
                    admitted_entitlement_revision=int(admitted_entitlement_revision),
                    provider=provider,
                    model=model,
                    required_modes=CLOUD_RUNTIME_TARGET_MODES,
                    require_active=status != "shadow",
                )
                if not admission.allowed:
                    raise RuntimePoolClaimError(f"runtime_admission_{admission.code}")
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                entitlement = admission.entitlement or {}
                if (
                    str(entitlement.get("consent_policy_version") or "") != lineage.policy_version
                    or str(entitlement.get("consent_processor_set_hash") or "") != lineage.processor_set_hash
                    or str(entitlement.get("consent_scope_version") or "") != lineage.scope_version
                    or str(entitlement.get("consent_scope_hash") or "") != lineage.scope_hash
                ):
                    raise RuntimePoolClaimError("runtime_cloud_entitlement_lineage_stale")
                selected = await connection.fetchrow(
                    """
                    SELECT b.*, u.omi_uid, u.profile_class
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                      AND b.provider = 'hermes_cloud'
                      AND b.claim_job_id = $2
                      AND b.claim_token = $3
                    FOR UPDATE
                    """,
                    uid,
                    job_uuid,
                    token_uuid,
                )
                if not selected:
                    raise RuntimePoolClaimError("runtime_pool_claim_lost")
                if selected["status"] in {"shadow", "internal_canary", "active"}:
                    return dict(selected)
                if selected["status"] != "claiming":
                    raise RuntimePoolClaimError("runtime_pool_claim_not_finalizable")
                if selected["api_base_url_ref"] is None or selected["api_key_ref"] is None:
                    raise RuntimePoolClaimError("runtime_pool_claim_endpoint_incomplete")
                if selected["honcho_api_key_ref"] is not None:
                    raise RuntimePoolClaimError("runtime_pool_claim_legacy_honcho_candidate")
                if str(selected["expected_model"] or "") != model:
                    raise RuntimePoolClaimError("runtime_pool_policy_changed")
                if not selected["claim_lease_expires_at"] or selected["claim_lease_expires_at"] <= datetime.now(
                    timezone.utc
                ):
                    raise RuntimePoolClaimError("runtime_pool_claim_expired")

                await connection.execute(
                    """
                    UPDATE ella_runtime_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND role = $2
                      AND provider = 'hermes_cloud'
                      AND active = true
                      AND id <> $3
                    """,
                    selected["user_id"],
                    selected["role"],
                    selected["id"],
                )
                activated = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings
                    SET honcho_workspace = $2,
                        observed_peer = $3,
                        observer_peer = $4,
                        account_user_id = user_id,
                        profile_user_id = user_id,
                        runtime_target_mode = $7,
                        target_endpoint_ref = api_base_url_ref,
                        target_credential_ref = api_key_ref,
                        status = $5,
                        health_state = 'healthy',
                        health_receipt = $6::jsonb,
                        active = ($5 <> 'shadow'),
                        claimed_at = CURRENT_TIMESTAMP,
                        claim_lease_expires_at = NULL,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    RETURNING *
                    """,
                    selected["id"],
                    honcho_workspace,
                    observed_peer,
                    observer_peer,
                    status,
                    json.dumps(health_receipt),
                    mode,
                )
                if activated["status"] != "shadow":
                    await connection.execute(
                        """
                        INSERT INTO ella_runtime_targets (
                            account_user_id, profile_user_id, role, mode, provider,
                            runtime_binding_id, candidate_runtime_instance_id,
                            endpoint_ref, credential_ref, status,
                            policy_version, processor_set_hash, scope_version,
                            scope_hash, entitlement_revision, metadata
                        )
                        SELECT
                            $1, $1, $2, target_modes.mode, 'hermes_cloud',
                            $4, $5, $6, $7, 'ready',
                            $8, $9, $10, $11, $12, $13::jsonb
                        FROM unnest($3::text[]) AS target_modes(mode)
                        ON CONFLICT (account_user_id, profile_user_id, role, mode)
                        WHERE provider = 'hermes_cloud' AND status = 'ready'
                        DO UPDATE SET
                            runtime_binding_id = EXCLUDED.runtime_binding_id,
                            candidate_runtime_instance_id = EXCLUDED.candidate_runtime_instance_id,
                            endpoint_ref = EXCLUDED.endpoint_ref,
                            credential_ref = EXCLUDED.credential_ref,
                            policy_version = EXCLUDED.policy_version,
                            processor_set_hash = EXCLUDED.processor_set_hash,
                            scope_version = EXCLUDED.scope_version,
                            scope_hash = EXCLUDED.scope_hash,
                            entitlement_revision = EXCLUDED.entitlement_revision,
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        selected["user_id"],
                        selected["role"],
                        [
                            *CLOUD_RUNTIME_TARGET_MODES,
                        ],
                        selected["id"],
                        selected["runtime_instance_id"],
                        selected["api_base_url_ref"],
                        selected["api_key_ref"],
                        lineage.policy_version,
                        lineage.processor_set_hash,
                        lineage.scope_version,
                        lineage.scope_hash,
                        int(admitted_entitlement_revision),
                        json.dumps({"content_free": True, "profile_class": str(selected["profile_class"] or "")}),
                    )
                await connection.execute(
                    """
                    UPDATE users
                    SET status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    selected["user_id"],
                )
                result = dict(activated)
                result["omi_uid"] = uid
                return result

    async def record_cloud_side_effect(
        self,
        *,
        uid: str,
        job_id: str,
        claim_token: str,
        effect: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one receipt-owned external artifact before the next side effect."""
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT b.id
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                      AND b.claim_job_id = $2
                      AND b.claim_token = $3
                      AND b.status = 'claiming'
                    FOR UPDATE
                    """,
                    uid,
                    uuid.UUID(str(job_id)),
                    uuid.UUID(str(claim_token)),
                )
                if not binding:
                    raise RuntimePoolClaimError("runtime_pool_claim_lost")
                row = await connection.fetchrow(
                    """
                    UPDATE ella_provisioning_jobs
                    SET external_side_effects = external_side_effects || $2::jsonb,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    RETURNING *
                    """,
                    uuid.UUID(str(job_id)),
                    json.dumps([effect]),
                )
                if not row:
                    raise RuntimePoolClaimError("runtime_pool_job_lost")
                return dict(row)

    async def get_cloud_side_effects(self, job_id: str) -> list[dict[str, Any]]:
        value = await self.pool.fetchval(
            "SELECT external_side_effects FROM ella_provisioning_jobs WHERE id = $1",
            uuid.UUID(str(job_id)),
        )
        return [dict(item) for item in (value or []) if isinstance(item, dict)]

    async def record_cloud_rollback(
        self,
        *,
        job_id: str,
        state: str,
        rollback_receipt: dict[str, Any],
        error_code: str,
        retryable: bool,
    ) -> dict[str, Any]:
        if state not in {"retryable", "blocked", "manual_intervention"}:
            raise ValueError("invalid_cloud_rollback_state")
        row = await self.pool.fetchrow(
            """
            UPDATE ella_provisioning_jobs
            SET state = $2,
                stage = 'runtime_ready',
                retryable = $3,
                error_code = $4,
                rollback_receipt = $5::jsonb,
                manual_intervention_at = CASE
                    WHEN $2 = 'manual_intervention' THEN CURRENT_TIMESTAMP
                    ELSE manual_intervention_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING *
            """,
            uuid.UUID(str(job_id)),
            state,
            retryable,
            error_code[:120],
            json.dumps(rollback_receipt),
        )
        if not row:
            raise RuntimePoolClaimError("runtime_pool_job_lost")
        return dict(row)

    async def quarantine_cloud_pool_claim(
        self,
        *,
        uid: str,
        job_id: str,
        claim_token: str,
        reason: str,
        health_receipt: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                selected = await connection.fetchrow(
                    """
                    SELECT b.*
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                      AND b.provider = 'hermes_cloud'
                      AND b.claim_job_id = $2
                      AND b.claim_token = $3
                      AND b.status IN (
                          'claiming', 'shadow', 'internal_canary', 'active', 'quarantined'
                      )
                    FOR UPDATE OF b
                    """,
                    uid,
                    uuid.UUID(str(job_id)),
                    uuid.UUID(str(claim_token)),
                )
                if not selected:
                    return None
                await connection.execute(
                    """
                    UPDATE ella_runtime_targets
                    SET status = 'revoked',
                        revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE runtime_binding_id = $1
                      AND provider = 'hermes_cloud'
                      AND status = 'ready'
                    """,
                    selected["id"],
                )
                if selected["status"] == "quarantined":
                    return dict(selected)
                row = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings
                    SET status = 'quarantined',
                        active = false,
                        health_state = 'unhealthy',
                        health_receipt = $2::jsonb,
                        quarantine_reason = $3,
                        quarantined_at = CURRENT_TIMESTAMP,
                        claim_lease_expires_at = NULL,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    RETURNING *
                    """,
                    selected["id"],
                    json.dumps(health_receipt or {}),
                    reason[:200],
                )
                return _row_dict(row)

    async def reconcile_cloud_pool_alert(
        self,
        *,
        threshold: int,
        provider: str = "hermes_cloud",
    ) -> dict[str, Any]:
        threshold = max(1, threshold)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                available = int(
                    await connection.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM ella_runtime_bindings
                        WHERE provider = $1
                          AND status = 'pool_available'
                          AND health_state = 'healthy'
                          AND active = false
                          AND user_id IS NULL
                        """,
                        provider,
                    )
                    or 0
                )
                if available < threshold:
                    alert = await connection.fetchrow(
                        """
                        INSERT INTO ella_runtime_pool_alerts (
                            provider, alert_type, state, available_count, threshold, metadata
                        )
                        VALUES ($1, 'low_water', 'pending', $2, $3, $4::jsonb)
                        ON CONFLICT (provider, alert_type) WHERE state = 'pending'
                        DO UPDATE SET
                            available_count = EXCLUDED.available_count,
                            threshold = EXCLUDED.threshold,
                            metadata = EXCLUDED.metadata
                        RETURNING *
                        """,
                        provider,
                        available,
                        threshold,
                        json.dumps({"content_free": True}),
                    )
                    return {"available": available, "low_water": True, "alert": dict(alert)}

                await connection.execute(
                    """
                    UPDATE ella_runtime_pool_alerts
                    SET state = 'resolved', resolved_at = CURRENT_TIMESTAMP
                    WHERE provider = $1
                      AND alert_type = 'low_water'
                      AND state = 'pending'
                    """,
                    provider,
                )
                return {"available": available, "low_water": False, "alert": None}

    async def mark_cloud_pool_alert_delivered(self, alert_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE ella_runtime_pool_alerts
            SET delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP)
            WHERE id = $1
              AND state = 'pending'
            """,
            uuid.UUID(str(alert_id)),
        )

    async def resolve_cloud_binding_state(self, uid: str, role: str = "user") -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT b.*, u.omi_uid, u.name, u.status AS user_status, u.profile_class
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE u.omi_uid = $1
              AND b.role = $2
              AND b.provider = 'hermes_cloud'
            ORDER BY b.updated_at DESC, b.id DESC
            LIMIT 1
            """,
            uid,
            role,
        )
        return _row_dict(row)

    async def get_cloud_binding_for_owner(
        self,
        *,
        uid: str,
        binding_id: str,
        role: str = "user",
    ) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT b.*, u.omi_uid, u.name, u.status AS user_status, u.profile_class
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE b.id = $1
              AND u.omi_uid = $2
              AND b.role = $3
              AND b.provider = 'hermes_cloud'
            """,
            uuid.UUID(str(binding_id)),
            uid,
            role,
        )
        return _row_dict(row)

    async def promote_cloud_binding(
        self,
        *,
        uid: str,
        binding_id: str,
        expected_revision: int,
        target_status: str,
        required_profile_class: str,
        admitted_entitlement_revision: int,
        authority_lineage: RuntimeTargetLineage,
        provider: str = CLOUD_RUNTIME_PROVIDER,
        model: str = CLOUD_RUNTIME_MODEL,
    ) -> dict[str, Any]:
        """Promote a shadow binding through an explicit revision-checked CAS."""
        if target_status not in {"internal_canary", "active"}:
            raise ValueError("invalid_cloud_promotion_status")
        if required_profile_class not in {"real", "synthetic"}:
            raise ValueError("invalid_cloud_profile_class")
        if provider != CLOUD_RUNTIME_PROVIDER or model != CLOUD_RUNTIME_MODEL:
            raise ValueError("invalid_cloud_runtime_policy")
        lineage = authority_lineage.validate()
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                admission = await voice_canary_db.revalidate_runtime_activation_on_connection(
                    connection,
                    uid=uid,
                    admitted_entitlement_revision=int(admitted_entitlement_revision),
                    provider=provider,
                    model=model,
                    required_modes=CLOUD_RUNTIME_TARGET_MODES,
                    require_active=True,
                )
                if not admission.allowed:
                    raise RuntimePoolClaimError(f"runtime_admission_{admission.code}")
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                entitlement = admission.entitlement or {}
                if (
                    str(entitlement.get("consent_policy_version") or "") != lineage.policy_version
                    or str(entitlement.get("consent_processor_set_hash") or "") != lineage.processor_set_hash
                    or str(entitlement.get("consent_scope_version") or "") != lineage.scope_version
                    or str(entitlement.get("consent_scope_hash") or "") != lineage.scope_hash
                ):
                    raise RuntimePoolClaimError("runtime_cloud_entitlement_lineage_stale")
                row = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings b
                    SET status = $4,
                        active = true,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    FROM users u
                    WHERE b.id = $1
                      AND b.user_id = u.id
                      AND u.omi_uid = $2
                      AND b.provider = 'hermes_cloud'
                      AND b.status = 'shadow'
                      AND b.active = false
                      AND b.revision = $3
                      AND b.health_state = 'healthy'
                      AND u.profile_class = $5
                      AND b.account_user_id = u.id
                      AND b.profile_user_id = u.id
                      AND b.runtime_target_mode IS NOT NULL
                      AND b.target_endpoint_ref = b.api_base_url_ref
                      AND b.target_credential_ref = b.api_key_ref
                    RETURNING b.*
                    """,
                    uuid.UUID(str(binding_id)),
                    uid,
                    int(expected_revision),
                    target_status,
                    required_profile_class,
                )
                if not row:
                    raise RuntimePoolClaimError("runtime_cloud_promotion_conflict")
                result = dict(row)
                health_receipt = _json_object(result.get("health_receipt") or {})
                if (
                    RuntimeTargetLineage.from_mapping(health_receipt) != lineage
                    or int(health_receipt.get("admission_revision") or 0) != int(admitted_entitlement_revision)
                    or str(result.get("expected_model") or "") != model
                ):
                    raise RuntimePoolClaimError("runtime_cloud_target_lineage_mismatch")
                await connection.execute(
                    """
                    INSERT INTO ella_runtime_targets (
                        account_user_id, profile_user_id, role, mode, provider,
                        runtime_binding_id, candidate_runtime_instance_id,
                        endpoint_ref, credential_ref, status,
                        policy_version, processor_set_hash, scope_version,
                        scope_hash, entitlement_revision, metadata
                    )
                    SELECT
                        $1, $2, $3, target_modes.mode, 'hermes_cloud',
                        $5, $6, $7, $8, 'ready',
                        $9, $10, $11, $12, $13, $14::jsonb
                    FROM unnest($4::text[]) AS target_modes(mode)
                    ON CONFLICT (account_user_id, profile_user_id, role, mode)
                    WHERE provider = 'hermes_cloud' AND status = 'ready'
                    DO UPDATE SET
                        runtime_binding_id = EXCLUDED.runtime_binding_id,
                        candidate_runtime_instance_id = EXCLUDED.candidate_runtime_instance_id,
                        endpoint_ref = EXCLUDED.endpoint_ref,
                        credential_ref = EXCLUDED.credential_ref,
                        policy_version = EXCLUDED.policy_version,
                        processor_set_hash = EXCLUDED.processor_set_hash,
                        scope_version = EXCLUDED.scope_version,
                        scope_hash = EXCLUDED.scope_hash,
                        entitlement_revision = EXCLUDED.entitlement_revision,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    result["account_user_id"],
                    result["profile_user_id"],
                    result["role"],
                    [
                        *CLOUD_RUNTIME_TARGET_MODES,
                    ],
                    result["id"],
                    result["runtime_instance_id"],
                    result["target_endpoint_ref"],
                    result["target_credential_ref"],
                    lineage.policy_version,
                    lineage.processor_set_hash,
                    lineage.scope_version,
                    lineage.scope_hash,
                    int(admitted_entitlement_revision),
                    json.dumps({"content_free": True, "profile_class": required_profile_class}),
                )
                return result

    async def get_or_create_runtime_scope(
        self,
        *,
        uid: str,
        binding_id: str,
        role: str,
        channel: str,
        allow_shadow: bool = False,
    ) -> dict[str, Any]:
        opaque_key = f"ella:scope:{uuid.uuid4()}"
        row = await self.pool.fetchrow(
            """
            INSERT INTO ella_runtime_session_scopes (
                id, binding_id, user_id, role, channel, session_key
            )
            SELECT $1, b.id, b.user_id, $4, $5, $6
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE b.id = $2
              AND u.omi_uid = $3
              AND b.provider = 'hermes_cloud'
              AND (b.active = true OR ($7 AND b.status = 'shadow'))
              AND b.status IN ('shadow', 'internal_canary', 'active')
            ON CONFLICT (binding_id, role, channel)
            DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            RETURNING *
            """,
            uuid.uuid4(),
            uuid.UUID(str(binding_id)),
            uid,
            role,
            channel,
            opaque_key,
            allow_shadow,
        )
        if not row:
            raise LookupError("runtime_scope_binding_not_ready")
        return dict(row)

    async def get_or_create_runtime_interaction(
        self,
        *,
        scope_id: str,
        client_interaction_id: str,
        request_hash: str,
        correlation_id: str,
        canonical_user_event_id: str,
        canonical_assistant_event_id: str,
    ) -> dict[str, Any]:
        previous_response_id = await self.pool.fetchval(
            """
            SELECT provider_response_id
            FROM ella_runtime_interactions
            WHERE scope_id = $1
              AND status = 'completed'
              AND provider_response_id IS NOT NULL
            ORDER BY completed_at DESC, created_at DESC
            LIMIT 1
            """,
            uuid.UUID(str(scope_id)),
        )
        row = await self.pool.fetchrow(
            """
            INSERT INTO ella_runtime_interactions (
                id, scope_id, client_interaction_id, request_hash, hermes_session_id,
                idempotency_key, correlation_id, previous_response_id,
                canonical_user_event_id, canonical_assistant_event_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (scope_id, client_interaction_id)
            DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            RETURNING *
            """,
            uuid.uuid4(),
            uuid.UUID(str(scope_id)),
            client_interaction_id,
            request_hash,
            f"ella:interaction:{uuid.uuid4()}",
            f"ella:request:{uuid.uuid4()}",
            correlation_id,
            previous_response_id,
            canonical_user_event_id,
            canonical_assistant_event_id,
        )
        result = dict(row)
        if str(result.get("request_hash") or "") != request_hash:
            raise RuntimePoolClaimError("runtime_interaction_payload_conflict")
        return result

    async def count_runtime_interaction_failures(
        self,
        *,
        scope_id: str,
        client_interaction_id: str,
        error_code: str,
    ) -> int:
        value = await self.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM ella_runtime_interactions
            WHERE scope_id = $1
              AND error_code = $3
              AND (
                  client_interaction_id = $2
                  OR POSITION(($2 || ':format-retry:') IN client_interaction_id) = 1
              )
            """,
            uuid.UUID(str(scope_id)),
            client_interaction_id,
            error_code,
        )
        return int(value or 0)

    async def invalidate_completed_runtime_interaction(
        self,
        *,
        interaction_id: str,
        error_code: str,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE ella_runtime_interactions
            SET status = 'failed',
                error_code = $2,
                completed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'completed'
            """,
            uuid.UUID(str(interaction_id)),
            error_code[:120],
        )

    async def claim_runtime_interaction(self, interaction_id: str) -> Optional[dict[str, Any]]:
        interaction_uuid = uuid.UUID(str(interaction_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                selected = await connection.fetchrow(
                    """
                    SELECT i.*
                    FROM ella_runtime_interactions i
                    JOIN ella_runtime_session_scopes s ON s.id = i.scope_id
                    WHERE i.id = $1
                    FOR UPDATE OF s, i
                    """,
                    interaction_uuid,
                )
                if not selected:
                    return None
                running_other = await connection.fetchval(
                    """
                    SELECT 1
                    FROM ella_runtime_interactions
                    WHERE scope_id = $1
                      AND id <> $2
                      AND status = 'running'
                      AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                    LIMIT 1
                    """,
                    selected["scope_id"],
                    interaction_uuid,
                )
                if running_other:
                    return None
                previous_response = await connection.fetchrow(
                    """
                    SELECT provider_response_id, usage
                    FROM ella_runtime_interactions
                    WHERE scope_id = $1
                      AND id <> $2
                      AND status = 'completed'
                      AND provider_response_id IS NOT NULL
                    ORDER BY completed_at DESC, created_at DESC, id DESC
                    LIMIT 1
                    """,
                    selected["scope_id"],
                    interaction_uuid,
                )
                previous_response_id = previous_response["provider_response_id"] if previous_response else None
                previous_response_usage = _json_object(previous_response["usage"] or {}) if previous_response else {}
                row = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_interactions
                    SET status = 'running',
                        error_code = NULL,
                        previous_response_id = $2,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                      AND (
                          status IN ('pending', 'failed')
                          OR (
                              status = 'running'
                              AND updated_at < CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                          )
                      )
                    RETURNING *
                    """,
                    interaction_uuid,
                    previous_response_id,
                )
                result = _row_dict(row)
                if result is not None:
                    result["previous_response_usage"] = previous_response_usage
                return result

    async def record_runtime_provider_receipt(
        self,
        *,
        interaction_id: str,
        provider_response_id: str,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_runtime_interactions
            SET provider_response_id = $2,
                usage = $3::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'running'
            RETURNING *
            """,
            uuid.UUID(str(interaction_id)),
            provider_response_id,
            json.dumps(usage),
        )
        if not row:
            raise LookupError("runtime_interaction_not_running")
        return dict(row)

    async def complete_runtime_interaction(
        self,
        *,
        interaction_id: str,
        provider_response_id: str,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_runtime_interactions
            SET status = 'completed',
                provider_response_id = $2,
                usage = $3::jsonb,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'running'
            RETURNING *
            """,
            uuid.UUID(str(interaction_id)),
            provider_response_id,
            json.dumps(usage),
        )
        if not row:
            raise LookupError("runtime_interaction_not_running")
        return dict(row)

    async def fail_runtime_interaction(self, *, interaction_id: str, error_code: str) -> None:
        await self.pool.execute(
            """
            UPDATE ella_runtime_interactions
            SET status = 'failed',
                error_code = $2,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'running'
            """,
            uuid.UUID(str(interaction_id)),
            error_code[:120],
        )

    async def claim_runtime_ingestion(
        self,
        *,
        binding_id: str,
        canonical_event_id: str,
        source_identity: str,
        event_revision: int,
        provenance: str,
    ) -> dict[str, Any]:
        inserted = await self.pool.fetchrow(
            """
            INSERT INTO ella_runtime_ingestion_receipts (
                id, binding_id, canonical_event_id, source_identity,
                event_revision, provenance, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'claimed')
            ON CONFLICT (
                binding_id, canonical_event_id, source_identity, event_revision
            ) DO NOTHING
            RETURNING *
            """,
            uuid.uuid4(),
            uuid.UUID(str(binding_id)),
            canonical_event_id,
            source_identity,
            max(1, int(event_revision)),
            provenance,
        )
        if inserted:
            result = dict(inserted)
            result["inserted"] = True
            return result
        existing = await self.pool.fetchrow(
            """
            SELECT *
            FROM ella_runtime_ingestion_receipts
            WHERE binding_id = $1
              AND canonical_event_id = $2
              AND source_identity = $3
              AND event_revision = $4
            """,
            uuid.UUID(str(binding_id)),
            canonical_event_id,
            source_identity,
            max(1, int(event_revision)),
        )
        result = dict(existing)
        result["inserted"] = False
        return result

    async def complete_runtime_ingestion(
        self,
        *,
        receipt_id: str,
        status: str,
        provider_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if status not in {"written", "skipped", "failed"}:
            raise ValueError("invalid_runtime_ingestion_status")
        row = await self.pool.fetchrow(
            """
            UPDATE ella_runtime_ingestion_receipts
            SET status = $2,
                provider_ref = $3,
                metadata = $4::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING *
            """,
            uuid.UUID(str(receipt_id)),
            status,
            provider_ref,
            json.dumps(metadata or {}),
        )
        if not row:
            raise LookupError("runtime_ingestion_receipt_not_found")
        return dict(row)

    async def resolve_photon_channel_binding(
        self,
        *,
        line_identity_key: str,
        contact_identity_key: str,
    ) -> Optional[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT
                b.*,
                p.id AS photon_binding_id,
                p.role AS photon_role,
                p.status AS photon_status,
                p.line_identity_key,
                p.contact_identity_key,
                p.policy_commit_sha AS photon_policy_commit_sha,
                p.command_tier_version,
                p.allow_all,
                p.attachments_enabled,
                p.caregiver_delivery_enabled,
                p.rollout_phase,
                p.daily_message_limit,
                p.daily_initiation_limit,
                p.sidecar_connection_key,
                p.sidecar_connected_at,
                p.oauth_expires_at,
                p.preflight_receipt AS photon_preflight_receipt,
                u.omi_uid,
                u.profile_class
            FROM ella_photon_channel_bindings p
            JOIN ella_runtime_bindings b ON b.id = p.runtime_binding_id
            JOIN users u ON u.id = p.user_id AND u.id = b.user_id
            WHERE p.line_identity_key = $1
              AND p.contact_identity_key = $2
              AND p.status = 'enabled'
            ORDER BY p.id
            LIMIT 2
            """,
            line_identity_key,
            contact_identity_key,
        )
        if len(rows) > 1:
            raise RuntimePoolClaimError("photon_identity_mapping_ambiguous")
        return _row_dict(rows[0]) if rows else None

    async def record_photon_sidecar_preflight(
        self,
        *,
        photon_binding_id: str,
        connection_key: str,
        oauth_expires_at: datetime,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_photon_channel_bindings
            SET sidecar_connection_key = $2,
                sidecar_connected_at = CURRENT_TIMESTAMP,
                oauth_expires_at = $3,
                preflight_receipt = $4::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'enabled'
              AND allow_all = false
              AND attachments_enabled = false
              AND caregiver_delivery_enabled = false
            RETURNING *
            """,
            uuid.UUID(str(photon_binding_id)),
            connection_key,
            oauth_expires_at,
            json.dumps(receipt),
        )
        if not row:
            raise RuntimePoolClaimError("photon_binding_not_ready")
        return dict(row)

    async def get_photon_message_by_inbound(
        self,
        *,
        photon_binding_id: str,
        inbound_provider_message_key: str,
        inbound_payload_sha256: str,
    ) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT *
            FROM ella_photon_message_receipts
            WHERE photon_binding_id = $1
              AND inbound_provider_message_key = $2
            """,
            uuid.UUID(str(photon_binding_id)),
            inbound_provider_message_key,
        )
        result = _row_dict(row)
        if result and str(result.get("inbound_payload_sha256") or "") != inbound_payload_sha256:
            raise RuntimePoolClaimError("photon_duplicate_payload_conflict")
        return result

    async def claim_photon_message(
        self,
        *,
        photon_binding_id: str,
        inbound_provider_message_key: str,
        inbound_payload_sha256: str,
        command_tier_version: str,
        consent_grant_epoch: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 900:
            raise RuntimePoolClaimError("photon_receipt_lease_invalid")
        if not 16 <= len(consent_grant_epoch) <= 96:
            raise RuntimePoolClaimError("photon_consent_grant_epoch_invalid")
        binding_uuid = uuid.UUID(str(photon_binding_id))
        new_lease_token = uuid.uuid4()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO ella_photon_message_receipts (
                        id, photon_binding_id, inbound_provider_message_key,
                        inbound_payload_sha256, status, command_tier_version,
                        consent_grant_epoch, lease_token, lease_expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, 'claimed', $5, $6, $7,
                        CURRENT_TIMESTAMP + ($8 * INTERVAL '1 second')
                    )
                    ON CONFLICT (
                        photon_binding_id, inbound_provider_message_key
                    ) DO NOTHING
                    RETURNING *
                    """,
                    uuid.uuid4(),
                    binding_uuid,
                    inbound_provider_message_key,
                    inbound_payload_sha256,
                    command_tier_version,
                    consent_grant_epoch,
                    new_lease_token,
                    lease_seconds,
                )
                if inserted:
                    result = dict(inserted)
                    result.update(inserted=True, reclaimed=False, acquired=True)
                    return result

                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_photon_message_receipts
                    WHERE photon_binding_id = $1
                      AND inbound_provider_message_key = $2
                    FOR UPDATE
                    """,
                    binding_uuid,
                    inbound_provider_message_key,
                )
                if not existing:
                    raise RuntimePoolClaimError("photon_message_claim_lost")
                result = dict(existing)
                if str(result["inbound_payload_sha256"]) != inbound_payload_sha256:
                    raise RuntimePoolClaimError("photon_duplicate_payload_conflict")

                status = str(result.get("status") or "")
                if str(result.get("consent_grant_epoch") or "") != consent_grant_epoch:
                    if status in {"claimed", "running", "awaiting_delivery"}:
                        quarantined = await connection.fetchrow(
                            """
                            UPDATE ella_photon_message_receipts
                            SET status = 'uncertain',
                                reconciliation_status = 'manual_required',
                                error_code = 'managed_cloud_consent_grant_changed',
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = $1
                              AND status IN ('claimed', 'running', 'awaiting_delivery')
                            RETURNING *
                            """,
                            result["id"],
                        )
                        result = dict(quarantined)
                    result.update(inserted=False, reclaimed=False, acquired=False)
                    return result

                lease_expires_at = result.get("lease_expires_at")
                stale = status in {"claimed", "running"} and (
                    not isinstance(lease_expires_at, datetime) or lease_expires_at <= datetime.now(timezone.utc)
                )
                if stale:
                    safe_to_reclaim = not bool(result.get("provider_started"))
                    if not safe_to_reclaim:
                        safe_to_reclaim = bool(
                            await connection.fetchval(
                                """
                                SELECT 1
                                FROM ella_runtime_interactions i
                                JOIN ella_runtime_session_scopes s ON s.id = i.scope_id
                                JOIN ella_photon_channel_bindings p
                                  ON p.runtime_binding_id = s.binding_id
                                WHERE p.id = $1
                                  AND s.channel = 'photon'
                                  AND i.client_interaction_id = $2
                                  AND i.status = 'completed'
                                LIMIT 1
                                """,
                                binding_uuid,
                                f"photon:{result['id']}",
                            )
                        )
                    if safe_to_reclaim:
                        reclaimed = await connection.fetchrow(
                            """
                            UPDATE ella_photon_message_receipts
                            SET lease_token = $2,
                                lease_expires_at =
                                    CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'),
                                attempt_count = attempt_count + 1,
                                reconciliation_status = 'recovered',
                                error_code = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = $1
                              AND status IN ('claimed', 'running')
                            RETURNING *
                            """,
                            result["id"],
                            new_lease_token,
                            lease_seconds,
                        )
                        recovered = dict(reclaimed)
                        recovered.update(inserted=False, reclaimed=True, acquired=True)
                        return recovered

                    quarantined = await connection.fetchrow(
                        """
                        UPDATE ella_photon_message_receipts
                        SET status = 'uncertain',
                            reconciliation_status = 'manual_required',
                            error_code = 'photon_provider_outcome_unconfirmed',
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                          AND status IN ('claimed', 'running')
                        RETURNING *
                        """,
                        result["id"],
                    )
                    result = dict(quarantined)

                result.update(inserted=False, reclaimed=False, acquired=False)
                return result

    async def reserve_photon_quota(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        photon_binding_id: str,
        message_limit: int,
        initiation_limit: int,
        conversation_initiation: bool,
    ) -> dict[str, Any]:
        if not 2 <= message_limit < 5000 or not 0 < initiation_limit < 50:
            raise RuntimePoolClaimError("photon_quota_policy_invalid")
        receipt_uuid = uuid.UUID(str(receipt_id))
        binding_uuid = uuid.UUID(str(photon_binding_id))
        initiation_units = 1 if conversation_initiation else 0
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                receipt = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_photon_message_receipts
                    WHERE id = $1 AND photon_binding_id = $2
                    FOR UPDATE
                    """,
                    receipt_uuid,
                    binding_uuid,
                )
                if not receipt:
                    raise RuntimePoolClaimError("photon_message_receipt_missing")
                if (
                    str(receipt.get("lease_token") or "") != lease_token
                    or not isinstance(receipt.get("lease_expires_at"), datetime)
                    or receipt["lease_expires_at"] <= datetime.now(timezone.utc)
                ):
                    raise RuntimePoolClaimError("photon_message_claim_conflict")
                if receipt["quota_reserved"]:
                    if receipt["status"] != "running":
                        raise RuntimePoolClaimError("photon_message_claim_conflict")
                    return dict(receipt)
                bucket = await connection.fetchrow(
                    """
                    INSERT INTO ella_photon_quota_buckets (
                        photon_binding_id, bucket_date,
                        messages_reserved, initiations_reserved
                    )
                    VALUES ($1, CURRENT_DATE, 2, $2)
                    ON CONFLICT (photon_binding_id, bucket_date)
                    DO UPDATE SET
                        messages_reserved =
                            ella_photon_quota_buckets.messages_reserved + 2,
                        initiations_reserved =
                            ella_photon_quota_buckets.initiations_reserved + EXCLUDED.initiations_reserved,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE ella_photon_quota_buckets.messages_reserved + 2 <= $3
                      AND ella_photon_quota_buckets.initiations_reserved
                          + EXCLUDED.initiations_reserved <= $4
                    RETURNING *
                    """,
                    binding_uuid,
                    initiation_units,
                    message_limit,
                    initiation_limit,
                )
                if not bucket:
                    raise RuntimePoolClaimError("photon_quota_exhausted")
                updated = await connection.fetchrow(
                    """
                    UPDATE ella_photon_message_receipts
                    SET quota_reserved = true,
                        status = 'running',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                      AND status = 'claimed'
                      AND lease_token = $2
                      AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    receipt_uuid,
                    uuid.UUID(str(lease_token)),
                )
                if not updated:
                    raise RuntimePoolClaimError("photon_message_claim_conflict")
                return dict(updated)

    async def mark_photon_provider_started(
        self,
        *,
        receipt_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_photon_message_receipts
            SET provider_started = true,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND lease_token = $2
              AND lease_expires_at > CURRENT_TIMESTAMP
              AND status = 'running'
            RETURNING *
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(lease_token)),
        )
        if not row:
            raise RuntimePoolClaimError("photon_message_claim_conflict")
        return dict(row)

    async def complete_photon_message(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        runtime_interaction_id: Optional[str],
        canonical_inbound_event_id: str,
        canonical_outbound_event_id: str,
        runtime_revision: int,
        expected_model: str,
        policy_commit_sha: str,
        usage: dict[str, Any],
        preflight_receipt: dict[str, Any],
        writeback_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_photon_message_receipts
            SET status = 'awaiting_delivery',
                runtime_interaction_id = $2,
                canonical_inbound_event_id = $3,
                canonical_outbound_event_id = $4,
                runtime_revision = $5,
                expected_model = $6,
                policy_commit_sha = $7,
                usage = $8::jsonb,
                preflight_receipt = $9::jsonb,
                writeback_receipt = $10::jsonb,
                provider_started = true,
                error_code = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'running'
              AND lease_token = $11
              AND lease_expires_at > CURRENT_TIMESTAMP
            RETURNING *
            """,
            uuid.UUID(str(receipt_id)),
            uuid.UUID(str(runtime_interaction_id)) if runtime_interaction_id else None,
            canonical_inbound_event_id,
            canonical_outbound_event_id,
            int(runtime_revision),
            expected_model,
            policy_commit_sha,
            json.dumps(usage),
            json.dumps(preflight_receipt),
            json.dumps(writeback_receipt),
            uuid.UUID(str(lease_token)),
        )
        if not row:
            raise RuntimePoolClaimError("photon_message_completion_conflict")
        return dict(row)

    async def fail_photon_message(
        self,
        *,
        receipt_id: str,
        lease_token: str,
        error_code: str,
        uncertain: bool,
        provider_started: bool,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE ella_photon_message_receipts
            SET status = $2,
                error_code = $3,
                provider_started = provider_started OR $4,
                reconciliation_status = CASE
                    WHEN $2 = 'uncertain' THEN 'manual_required'
                    ELSE reconciliation_status
                END,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status IN ('claimed', 'running')
              AND lease_token = $5
            """,
            uuid.UUID(str(receipt_id)),
            "uncertain" if uncertain else "failed",
            error_code[:120],
            provider_started,
            uuid.UUID(str(lease_token)),
        )

    async def quarantine_photon_delivery_for_consent(
        self,
        *,
        receipt_id: str,
        error_code: str,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_photon_message_receipts
            SET status = 'uncertain',
                error_code = $2,
                reconciliation_status = 'manual_required',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'awaiting_delivery'
            RETURNING *
            """,
            uuid.UUID(str(receipt_id)),
            error_code[:120],
        )
        if not row:
            raise RuntimePoolClaimError("photon_consent_quarantine_conflict")
        return dict(row)

    async def acknowledge_photon_delivery(
        self,
        *,
        receipt_id: str,
        delivery_idempotency_key: str,
        outbound_provider_message_key: str,
        delivery_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            row = await self.pool.fetchrow(
                """
                UPDATE ella_photon_message_receipts
                SET status = 'delivered',
                    outbound_provider_message_key = $3,
                    delivery_receipt = $4::jsonb,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                  AND delivery_idempotency_key = $2
                  AND status IN ('awaiting_delivery', 'delivered')
                  AND (
                      outbound_provider_message_key IS NULL
                      OR outbound_provider_message_key = $3
                  )
                RETURNING *
                """,
                uuid.UUID(str(receipt_id)),
                uuid.UUID(str(delivery_idempotency_key)),
                outbound_provider_message_key,
                json.dumps(delivery_receipt),
            )
        except asyncpg.UniqueViolationError as exc:
            raise RuntimePoolClaimError("photon_outbound_message_conflict") from exc
        if not row:
            raise RuntimePoolClaimError("photon_delivery_ack_conflict")
        return dict(row)

    async def get_photon_message_receipt(
        self,
        *,
        receipt_id: str,
    ) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT
                m.*,
                p.status AS photon_status,
                p.sidecar_connection_key,
                p.sidecar_connected_at,
                p.oauth_expires_at,
                p.line_identity_key,
                p.contact_identity_key
            FROM ella_photon_message_receipts m
            JOIN ella_photon_channel_bindings p ON p.id = m.photon_binding_id
            WHERE m.id = $1
            """,
            uuid.UUID(str(receipt_id)),
        )
        return _row_dict(row)

    async def update_job(
        self,
        *,
        job_id: str,
        state: str,
        stage: str,
        retryable: bool,
        error_code: Optional[str] = None,
        error_detail: Optional[dict[str, Any]] = None,
        receipt: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            UPDATE ella_provisioning_jobs
            SET state = $2,
                stage = $3,
                retryable = $4,
                error_code = $5,
                error_detail = $6::jsonb,
                receipts = CASE
                    WHEN $7::jsonb = '{}'::jsonb THEN receipts
                    ELSE COALESCE(receipts, '[]'::jsonb) || jsonb_build_array($7::jsonb)
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND (
                    state NOT IN ('ready', 'blocked', 'rolling_back', 'manual_intervention')
                    OR state = $2
                  )
            RETURNING *
            """,
            uuid.UUID(str(job_id)),
            state,
            stage,
            retryable,
            error_code,
            json.dumps(error_detail or {}),
            json.dumps(receipt or {}),
        )
        if not row:
            existing = await self.pool.fetchrow(
                "SELECT * FROM ella_provisioning_jobs WHERE id = $1",
                uuid.UUID(str(job_id)),
            )
            if existing and str(existing["state"]) in {"ready", "blocked", "rolling_back", "manual_intervention"}:
                return dict(existing)
            raise LookupError("provisioning_job_not_found")
        return dict(row)

    async def prepare_runtime_binding_identity(self, *, uid: str, provider: str, role: str = "user") -> str:
        """Reserve the exact binding identity included in the provision challenge."""
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                existing = await connection.fetchval(
                    """
                    SELECT b.id
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1 AND b.provider = $2 AND b.role = $3
                    FOR SHARE OF b
                    """,
                    uid,
                    provider,
                    role,
                )
                return str(
                    existing
                    or deterministic_runtime_binding_id(
                        uid=uid,
                        provider=provider,
                        role=role,
                    )
                )

    async def _verify_self_hosted_honcho_attestation(
        self,
        connection,
        *,
        binding: dict[str, Any],
        uid: str,
        runtime_target_id: str,
        allowed_job_states: set[tuple[str, str]],
        require_current_freshness: bool,
    ) -> None:
        receipt = binding.get("health_receipt") or {}
        if isinstance(receipt, str):
            try:
                receipt = json.loads(receipt)
            except json.JSONDecodeError as exc:
                raise RuntimePoolClaimError("honcho_attestation_evidence_malformed") from exc
        evidence = receipt.get("honcho_isolation") if isinstance(receipt, dict) else None
        attestation = evidence.get("attestation") if isinstance(evidence, dict) else None
        if not isinstance(attestation, dict):
            raise RuntimePoolClaimError("honcho_attestation_evidence_malformed")

        profiles_root = os.getenv("ELLA_HERMES_PROFILES_ROOT", "/Users/ellaai/.hermes/profiles")
        profile_name = str(binding.get("profile_name") or "")
        expected_challenge = {
            "version": ATTESTATION_VERSION,
            "nonce": attestation.get("nonce"),
            "issued_at": attestation.get("issued_at"),
            "expires_at": attestation.get("expires_at"),
            "firebase_uid": uid,
            "account_owner_id": str(binding.get("account_user_id") or binding.get("user_id") or ""),
            "runtime_target_id": str(runtime_target_id),
            "binding_id": str(binding.get("id") or ""),
            "job_id": str(attestation.get("job_id") or ""),
        }
        expected_config = posixpath.normpath(f"{profiles_root.rstrip('/')}/{profile_name}/honcho.json")
        try:
            verified_evidence = verify_persisted_attestation(
                evidence,
                expected_challenge=expected_challenge,
                observed=observed_runtime_fields(
                    profile_name=profile_name,
                    config_path=expected_config,
                    workspace_root=str(binding.get("workspace_root") or ""),
                    honcho_workspace=str(binding.get("honcho_workspace") or ""),
                    observed_peer_id=str(binding.get("observed_peer") or ""),
                    observer_peer_id=str(binding.get("observer_peer") or ""),
                    gateway_port=int(binding.get("gateway_port") or 0),
                    gateway_target=str(binding.get("internal_gateway_url") or ""),
                    credential_ref=str(binding.get("credential_ref") or ""),
                    agent_id=str(binding.get("agent_id") or ""),
                    service_label=str(binding.get("service_label") or ""),
                ),
            )
        except (HonchoAttestationError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, HonchoAttestationError) else "honcho_attestation_readback_mismatch"
            raise RuntimePoolClaimError(code) from exc
        if require_current_freshness and int(time.time()) > int(verified_evidence["attestation"]["expires_at"]):
            raise RuntimePoolClaimError("honcho_attestation_stale")

        try:
            job_id = uuid.UUID(str(attestation["job_id"]))
        except (TypeError, ValueError) as exc:
            raise RuntimePoolClaimError("honcho_attestation_job_mismatch") from exc
        job = await connection.fetchrow(
            """
            SELECT state, stage
            FROM ella_provisioning_jobs
            WHERE id = $1 AND user_id = $2 AND target_schema_version = $3
            FOR UPDATE
            """,
            job_id,
            binding["user_id"],
            binding["template_version"],
        )
        if not job or (str(job["state"]), str(job["stage"])) not in allowed_job_states:
            raise RuntimePoolClaimError("honcho_attestation_job_mismatch")

    async def stage_runtime_binding(self, *, uid: str, binding: dict[str, Any]) -> dict[str, Any]:
        requested_binding_id = str(binding.get("binding_id") or uuid.uuid4())
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO ella_runtime_bindings (
                        id, user_id, account_user_id, profile_user_id,
                        role, provider, profile_name, agent_id, workspace_root,
                        internal_gateway_url, gateway_port, service_label, credential_ref,
                        honcho_workspace, observed_peer, observer_peer, template_version,
                        model_policy_version, voice_policy_version, health_state,
                        health_receipt, revision, active
                    )
                    SELECT
                        $1, u.id, u.id, u.id, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, $18, $19::jsonb, 1, false
                    FROM users u
                    WHERE u.omi_uid = $2
                    ON CONFLICT (user_id, role, provider)
                    WHERE provider <> 'hermes_cloud'
                    DO UPDATE
                    SET account_user_id = EXCLUDED.account_user_id,
                        profile_user_id = EXCLUDED.profile_user_id,
                        profile_name = EXCLUDED.profile_name,
                        agent_id = EXCLUDED.agent_id,
                        workspace_root = EXCLUDED.workspace_root,
                        internal_gateway_url = EXCLUDED.internal_gateway_url,
                        gateway_port = EXCLUDED.gateway_port,
                        service_label = EXCLUDED.service_label,
                        credential_ref = EXCLUDED.credential_ref,
                        honcho_workspace = EXCLUDED.honcho_workspace,
                        observed_peer = EXCLUDED.observed_peer,
                        observer_peer = EXCLUDED.observer_peer,
                        template_version = EXCLUDED.template_version,
                        model_policy_version = EXCLUDED.model_policy_version,
                        voice_policy_version = EXCLUDED.voice_policy_version,
                        health_state = EXCLUDED.health_state,
                        health_receipt = EXCLUDED.health_receipt,
                        revision = ella_runtime_bindings.revision + 1,
                        active = ella_runtime_bindings.active,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    uuid.UUID(requested_binding_id),
                    uid,
                    binding.get("role", "user"),
                    binding["provider"],
                    binding.get("profile_name"),
                    binding["agent_id"],
                    binding.get("workspace_root"),
                    binding.get("internal_gateway_url"),
                    binding.get("gateway_port"),
                    binding.get("service_label"),
                    binding.get("credential_ref"),
                    binding.get("honcho_workspace"),
                    binding.get("observed_peer"),
                    binding.get("observer_peer"),
                    binding["template_version"],
                    binding["model_policy_version"],
                    binding["voice_policy_version"],
                    binding.get("health_state", "pending"),
                    json.dumps(binding.get("health_receipt") or {}),
                )
        if not row:
            raise LookupError("user_not_found")
        if binding.get("binding_id") and str(row["id"]) != requested_binding_id:
            raise RuntimePoolClaimError("honcho_attestation_binding_mismatch")
        return dict(row)

    async def activate_runtime_binding(
        self,
        *,
        uid: str,
        provider: str,
        role: str = "user",
        require_invitation_target: bool = False,
        authority_lineage: Optional[RuntimeTargetLineage] = None,
        model: str = SELF_HOSTED_RUNTIME_MODEL,
    ) -> dict[str, Any]:
        lineage = None
        if require_invitation_target:
            if provider != SELF_HOSTED_RUNTIME_PROVIDER:
                raise ValueError("self_hosted_runtime_provider_required")
            if model != SELF_HOSTED_RUNTIME_MODEL:
                raise ValueError("invalid_self_hosted_runtime_policy")
            if authority_lineage is None:
                raise ValueError("self_hosted_runtime_lineage_required")
            lineage = authority_lineage.validate()
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                await voice_canary_db.lock_runtime_authority_on_connection(
                    connection,
                    uid=uid,
                    provider=provider,
                    mode="hermes-chat" if require_invitation_target else None,
                )
                selected = await connection.fetchrow(
                    """
                    SELECT b.*
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1 AND b.provider = $2 AND b.role = $3
                    FOR UPDATE
                    """,
                    uid,
                    provider,
                    role,
                )
                if not selected:
                    raise LookupError("runtime_binding_not_found")
                if selected["health_state"] != "healthy":
                    raise RuntimeError("runtime_binding_not_healthy")

                if require_invitation_target:
                    authority = await connection.fetchrow(
                        """
                        SELECT
                            target.id AS target_id,
                            target.invitation_target_id,
                            target.entitlement_revision,
                            target.status AS target_status,
                            target.runtime_binding_id,
                            entitlement.status AS entitlement_status,
                            entitlement.revision AS current_entitlement_revision,
                            entitlement.consent_authority_epoch,
                            invitation.id AS invitation_id
                        FROM ella_runtime_targets target
                        JOIN ella_runtime_targets voice_target
                          ON voice_target.invitation_target_id = target.invitation_target_id
                         AND voice_target.account_user_id = target.account_user_id
                         AND voice_target.profile_user_id = target.profile_user_id
                         AND voice_target.role = target.role
                         AND voice_target.provider = target.provider
                         AND voice_target.mode = 'hermes-voice'
                        JOIN ella_invitation_targets invitation_target
                          ON invitation_target.id = target.invitation_target_id
                        JOIN ella_invitation_redemptions redemption
                          ON redemption.invitation_target_id = invitation_target.id
                         AND redemption.user_id = $1
                         AND redemption.user_mapping_state = 'mapped'
                         AND redemption.consent_pending = FALSE
                        JOIN ella_invitations invitation
                          ON invitation.id = redemption.invitation_id
                         AND invitation.id = invitation_target.invitation_id
                        JOIN voice_entitlements entitlement
                          ON entitlement.uid = $2
                         AND entitlement.invitation_id = invitation.id
                        JOIN ella_managed_cloud_consent_authority consent
                          ON consent.user_id = $1
                        WHERE target.account_user_id = $1
                          AND target.profile_user_id = $1
                          AND target.role = $3
                          AND target.provider = 'hermes'
                          AND target.mode = 'hermes-chat'
                          AND target.status IN ('reserved', 'ready')
                          AND (target.runtime_binding_id IS NULL OR target.runtime_binding_id = $4)
                          AND target.entitlement_revision = entitlement.revision
                          AND target.policy_version = $5
                          AND target.processor_set_hash = $6
                          AND target.scope_version = $7
                          AND target.scope_hash = $8
                          AND voice_target.status = target.status
                          AND voice_target.runtime_binding_id IS NOT DISTINCT FROM target.runtime_binding_id
                          AND voice_target.entitlement_revision = target.entitlement_revision
                          AND voice_target.policy_version = target.policy_version
                          AND voice_target.processor_set_hash = target.processor_set_hash
                          AND voice_target.scope_version = target.scope_version
                          AND voice_target.scope_hash = target.scope_hash
                          AND invitation_target.required_profile_class = 'real'
                          AND invitation_target.consumed_at IS NOT NULL
                          AND invitation_target.revoked_at IS NULL
                          AND invitation.delivery_state = 'sent'
                          AND (
                                (invitation.kind = 'ordinary' AND invitation.state = 'redeemed')
                                OR (invitation.kind = 'app_review' AND invitation.state = 'sent')
                              )
                          AND invitation.required_consent_policy_version = target.policy_version
                          AND invitation.required_consent_processor_set_hash = target.processor_set_hash
                          AND invitation.required_consent_scope_version = target.scope_version
                          AND invitation.required_consent_scope_hash = target.scope_hash
                          AND entitlement.status IN ('invited', 'active')
                          AND entitlement.invitation_consent_pending = FALSE
                          AND entitlement.consent_authority_epoch = consent.authority_epoch
                          AND entitlement.consent_policy_version = target.policy_version
                          AND entitlement.consent_processor_set_hash = target.processor_set_hash
                          AND entitlement.consent_scope_version = target.scope_version
                          AND entitlement.consent_scope_hash = target.scope_hash
                          AND entitlement.provider_allowlist = ARRAY['hermes']::text[]
                          AND entitlement.model_allowlist = ARRAY[$9]::text[]
                          AND entitlement.mode_allowlist = $10::text[]
                          AND entitlement.fallback_policy = '{"enabled":false,"order":[]}'::jsonb
                          AND consent.decision = 'granted'
                          AND consent.consent_receipt_ref IS NOT NULL
                          AND consent.profile_binding_id IS NOT NULL
                          AND consent.policy_version = target.policy_version
                          AND consent.processor_set_hash = target.processor_set_hash
                          AND consent.scope_version = target.scope_version
                          AND consent.scope_hash = target.scope_hash
                        FOR UPDATE OF target, voice_target, invitation_target, redemption,
                            invitation, entitlement, consent
                        """,
                        selected["user_id"],
                        uid,
                        role,
                        selected["id"],
                        lineage.policy_version,
                        lineage.processor_set_hash,
                        lineage.scope_version,
                        lineage.scope_hash,
                        model,
                        list(SELF_HOSTED_RUNTIME_TARGET_MODES),
                    )
                    if not authority:
                        raise RuntimePoolClaimError("invitation_runtime_target_missing")
                    await self._verify_self_hosted_honcho_attestation(
                        connection,
                        binding=dict(selected),
                        uid=uid,
                        runtime_target_id=str(authority["invitation_target_id"]),
                        allowed_job_states={("provisioning", "smoke_passed")},
                        require_current_freshness=True,
                    )
                    decision = await voice_canary_db.revalidate_runtime_activation_on_connection(
                        connection,
                        uid=uid,
                        admitted_entitlement_revision=int(authority["entitlement_revision"]),
                        provider=provider,
                        model=model,
                        required_modes=SELF_HOSTED_RUNTIME_TARGET_MODES,
                        require_active=str(authority["entitlement_status"]) == "active",
                    )
                    if not decision.allowed:
                        raise RuntimePoolClaimError(f"runtime_admission_{decision.code}")
                    entitlement_revision = int(authority["current_entitlement_revision"])
                    if authority["entitlement_status"] == "invited":
                        activated_entitlement = await connection.fetchrow(
                            """
                            UPDATE voice_entitlements
                            SET status = 'active',
                                revision = revision + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE uid = $1
                              AND status = 'invited'
                              AND revision = $2
                              AND invitation_id = $3
                              AND consent_authority_epoch = $4
                            RETURNING revision
                            """,
                            uid,
                            entitlement_revision,
                            authority["invitation_id"],
                            authority["consent_authority_epoch"],
                        )
                        if not activated_entitlement:
                            raise RuntimePoolClaimError("invitation_entitlement_activation_stale")
                        entitlement_revision = int(activated_entitlement["revision"])
                    targets = await connection.fetch(
                        """
                        UPDATE ella_runtime_targets
                        SET runtime_binding_id = $2,
                            status = 'ready',
                            entitlement_revision = $3,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE invitation_target_id = $1
                          AND status IN ('reserved', 'ready')
                          AND (runtime_binding_id IS NULL OR runtime_binding_id = $2)
                          AND mode = ANY($4::text[])
                        RETURNING id, mode
                        """,
                        authority["invitation_target_id"],
                        selected["id"],
                        entitlement_revision,
                        list(SELF_HOSTED_RUNTIME_TARGET_MODES),
                    )
                    if {str(target["mode"]) for target in targets} != set(SELF_HOSTED_RUNTIME_TARGET_MODES):
                        raise RuntimePoolClaimError("invitation_runtime_target_stale")

                await connection.execute(
                    """
                    UPDATE ella_runtime_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND role = $2 AND active = true AND id <> $3
                    """,
                    selected["user_id"],
                    role,
                    selected["id"],
                )
                if bool(selected["active"]) and (not require_invitation_target or str(selected["status"]) == "active"):
                    activated = selected
                else:
                    activated = await connection.fetchrow(
                        """
                        UPDATE ella_runtime_bindings
                        SET active = true,
                            status = CASE WHEN provider = 'hermes' THEN 'active' ELSE status END,
                            revision = revision + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                        RETURNING *
                        """,
                        selected["id"],
                    )
                await connection.execute(
                    """
                    UPDATE users
                    SET status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    selected["user_id"],
                )
                return dict(activated)

    async def activate_user(self, uid: str) -> None:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                result = await connection.execute(
                    """
                    UPDATE users
                    SET status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP
                    WHERE omi_uid = $1
                    """,
                    uid,
                )
        if result == "UPDATE 0":
            raise LookupError("user_not_found")

    async def update_guardian_mode(self, uid: str, guardian_mode: Optional[str]) -> Optional[str]:
        """Update one active user's Guardian preference under owner authority."""
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                connection,
                uid=uid,
            )
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    connection,
                    owner=owner,
                )
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                row = await connection.fetchrow(
                    """
                    UPDATE users
                    SET guardian_mode = $2, updated_at = CURRENT_TIMESTAMP
                    WHERE omi_uid = $1
                      AND status = 'ACTIVE'
                    RETURNING guardian_mode
                    """,
                    uid,
                    guardian_mode,
                )
        if row is None:
            raise LookupError("active_user_not_found")
        return row["guardian_mode"]

    async def has_active_retained_runtime(self, uid: str) -> bool:
        """Return whether an authenticated legacy user still has usable routing."""
        row = await self.pool.fetchrow(
            """
            SELECT EXISTS (
                SELECT 1
                FROM users u
                JOIN agent_clusters ac ON ac.user_id = u.id
                WHERE u.omi_uid = $1
                  AND u.status = 'ACTIVE'
                  AND ac.status = 'ACTIVE'
                  AND jsonb_typeof(ac.agents) = 'object'
                  AND NULLIF(BTRIM(ac.agents->>'userAgentId'), '') IS NOT NULL
            ) AS eligible
            """,
            uid,
        )
        return bool(row and row["eligible"])

    async def resolve_active_runtime(
        self,
        uid: str,
        role: str = "user",
        template_version: Optional[str] = None,
        target_mode: Optional[str] = None,
        required_provider: Optional[str] = None,
        authority_lineage: Optional[RuntimeTargetLineage] = None,
        model: str = CLOUD_RUNTIME_MODEL,
    ) -> Optional[dict[str, Any]]:
        if required_provider == CLOUD_RUNTIME_PROVIDER:
            if target_mode not in CLOUD_RUNTIME_TARGET_MODES:
                raise ValueError("cloud_runtime_target_mode_required")
            if model != CLOUD_RUNTIME_MODEL:
                raise ValueError("invalid_cloud_runtime_policy")
            if authority_lineage is None:
                raise ValueError("cloud_runtime_lineage_required")
            lineage = authority_lineage.validate()
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await voice_canary_db.lock_runtime_authority_on_connection(
                        connection,
                        uid=uid,
                        provider=CLOUD_RUNTIME_PROVIDER,
                        mode=target_mode,
                    )
                    row = await connection.fetchrow(
                        """
                        SELECT
                            b.*, u.omi_uid, u.name, u.status AS user_status,
                            u.profile_class, t.id AS resolved_target_id,
                            t.mode AS resolved_target_mode,
                            t.endpoint_ref AS resolved_target_endpoint_ref,
                            t.credential_ref AS resolved_target_credential_ref,
                            t.policy_version AS target_policy_version,
                            t.processor_set_hash AS target_processor_set_hash,
                            t.scope_version AS target_scope_version,
                            t.scope_hash AS target_scope_hash,
                            t.entitlement_revision AS target_entitlement_revision,
                            t.updated_at AS resolved_target_updated_at
                        FROM ella_runtime_bindings b
                        JOIN users u ON u.id = b.user_id
                        JOIN ella_runtime_targets t ON t.runtime_binding_id = b.id
                        WHERE u.omi_uid = $1
                          AND b.role = $2
                          AND b.provider = 'hermes_cloud'
                          AND b.active = true
                          AND b.status IN ('internal_canary', 'active')
                          AND b.health_state = 'healthy'
                          AND b.account_user_id = u.id
                          AND b.profile_user_id = u.id
                          AND b.expected_model = $4
                          AND t.account_user_id = u.id
                          AND t.profile_user_id = u.id
                          AND t.role = b.role
                          AND t.provider = 'hermes_cloud'
                          AND t.status = 'ready'
                          AND t.mode = $5
                          AND t.candidate_runtime_instance_id = b.runtime_instance_id
                          AND t.endpoint_ref = b.api_base_url_ref
                          AND t.credential_ref = b.api_key_ref
                          AND t.policy_version = $6
                          AND t.processor_set_hash = $7
                          AND t.scope_version = $8
                          AND t.scope_hash = $9
                          AND t.entitlement_revision IS NOT NULL
                          AND ($3::text IS NULL OR b.template_version = $3)
                        FOR SHARE OF b, u, t
                        """,
                        uid,
                        role,
                        template_version,
                        model,
                        target_mode,
                        lineage.policy_version,
                        lineage.processor_set_hash,
                        lineage.scope_version,
                        lineage.scope_hash,
                    )
                    if not row:
                        return None
                    decision = await voice_canary_db.revalidate_runtime_resolution_on_connection(
                        connection,
                        uid=uid,
                        admitted_entitlement_revision=int(row["target_entitlement_revision"]),
                        provider=CLOUD_RUNTIME_PROVIDER,
                        model=model,
                        mode=target_mode,
                    )
                    if not decision.allowed:
                        raise RuntimePoolClaimError(f"runtime_admission_{decision.code}")
                    entitlement = decision.entitlement or {}
                    if (
                        str(entitlement.get("consent_policy_version") or "") != lineage.policy_version
                        or str(entitlement.get("consent_processor_set_hash") or "") != lineage.processor_set_hash
                        or str(entitlement.get("consent_scope_version") or "") != lineage.scope_version
                        or str(entitlement.get("consent_scope_hash") or "") != lineage.scope_hash
                    ):
                        raise RuntimePoolClaimError("runtime_cloud_entitlement_lineage_stale")
                    consent_authority_epoch = str(entitlement.get("consent_authority_epoch") or "").strip()
                    if not consent_authority_epoch:
                        raise RuntimePoolClaimError("runtime_cloud_consent_authority_epoch_missing")
                    result = dict(row)
                    result["runtime_target_id"] = str(row["resolved_target_id"])
                    result["runtime_target_mode"] = str(row["resolved_target_mode"])
                    result["target_endpoint_ref"] = str(row["resolved_target_endpoint_ref"])
                    result["target_credential_ref"] = str(row["resolved_target_credential_ref"])
                    result["target_entitlement_revision"] = int(row["target_entitlement_revision"])
                    result["runtime_target_updated_at"] = str(row["resolved_target_updated_at"])
                    result["consent_authority_epoch"] = consent_authority_epoch
                    return result
        if required_provider == SELF_HOSTED_RUNTIME_PROVIDER and authority_lineage is not None:
            if target_mode not in SELF_HOSTED_RUNTIME_TARGET_MODES:
                raise ValueError("self_hosted_runtime_target_mode_required")
            if model != SELF_HOSTED_RUNTIME_MODEL:
                raise ValueError("invalid_self_hosted_runtime_policy")
            lineage = authority_lineage.validate()
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await voice_canary_db.lock_runtime_authority_on_connection(
                        connection,
                        uid=uid,
                        provider=SELF_HOSTED_RUNTIME_PROVIDER,
                        mode=target_mode,
                    )
                    row = await connection.fetchrow(
                        """
                        SELECT
                            binding.*, app_user.omi_uid, app_user.name,
                            app_user.status AS user_status, app_user.profile_class,
                            target.id AS resolved_target_id,
                            target.invitation_target_id AS resolved_attestation_target_id,
                            target.mode AS resolved_target_mode,
                            target.endpoint_ref AS resolved_target_endpoint_ref,
                            target.credential_ref AS resolved_target_credential_ref,
                            target.policy_version AS target_policy_version,
                            target.processor_set_hash AS target_processor_set_hash,
                            target.scope_version AS target_scope_version,
                            target.scope_hash AS target_scope_hash,
                            target.entitlement_revision AS target_entitlement_revision,
                            target.updated_at AS resolved_target_updated_at,
                            authority.authority_epoch AS resolved_authority_epoch
                        FROM users app_user
                        JOIN voice_entitlements entitlement
                          ON entitlement.uid = app_user.omi_uid
                        JOIN ella_invitation_redemptions redemption
                          ON redemption.invitation_id = entitlement.invitation_id
                         AND redemption.user_id = app_user.id
                         AND redemption.user_mapping_state = 'mapped'
                         AND redemption.consent_pending = FALSE
                        JOIN ella_invitations invitation
                          ON invitation.id = redemption.invitation_id
                        JOIN ella_invitation_targets invitation_target
                          ON invitation_target.id = redemption.invitation_target_id
                         AND invitation_target.invitation_id = invitation.id
                        JOIN ella_runtime_targets target
                          ON target.invitation_target_id = invitation_target.id
                         AND target.account_user_id = app_user.id
                         AND target.profile_user_id = app_user.id
                        JOIN ella_runtime_bindings binding
                          ON binding.id = target.runtime_binding_id
                         AND binding.user_id = app_user.id
                        JOIN ella_managed_cloud_consent_authority authority
                          ON authority.user_id = app_user.id
                        WHERE app_user.omi_uid = $1
                          AND app_user.status = 'ACTIVE'
                          AND app_user.profile_class = 'real'
                          AND binding.role = $2
                          AND binding.provider = 'hermes'
                          AND binding.status = 'active'
                          AND binding.active = TRUE
                          AND binding.health_state = 'healthy'
                          AND binding.account_user_id = app_user.id
                          AND binding.profile_user_id = app_user.id
                          AND ($3::text IS NULL OR binding.template_version = $3)
                          AND target.role = binding.role
                          AND target.provider = 'hermes'
                          AND target.status = 'ready'
                          AND target.mode = $4
                          AND target.runtime_binding_id = binding.id
                          AND target.entitlement_revision = entitlement.revision
                          AND target.policy_version = $5
                          AND target.processor_set_hash = $6
                          AND target.scope_version = $7
                          AND target.scope_hash = $8
                          AND invitation_target.required_profile_class = 'real'
                          AND invitation_target.consumed_at IS NOT NULL
                          AND invitation_target.revoked_at IS NULL
                          AND invitation.delivery_state = 'sent'
                          AND (
                                (invitation.kind = 'ordinary' AND invitation.state = 'redeemed')
                                OR (invitation.kind = 'app_review' AND invitation.state = 'sent')
                              )
                          AND entitlement.status = 'active'
                          AND entitlement.invitation_consent_pending = FALSE
                          AND entitlement.consent_authority_epoch = authority.authority_epoch
                          AND authority.decision = 'granted'
                          AND authority.consent_receipt_ref IS NOT NULL
                          AND authority.profile_binding_id IS NOT NULL
                          AND authority.policy_version = target.policy_version
                          AND authority.processor_set_hash = target.processor_set_hash
                          AND authority.scope_version = target.scope_version
                          AND authority.scope_hash = target.scope_hash
                          AND invitation.required_consent_policy_version = target.policy_version
                          AND invitation.required_consent_processor_set_hash = target.processor_set_hash
                          AND invitation.required_consent_scope_version = target.scope_version
                          AND invitation.required_consent_scope_hash = target.scope_hash
                          AND entitlement.consent_policy_version = target.policy_version
                          AND entitlement.consent_processor_set_hash = target.processor_set_hash
                          AND entitlement.consent_scope_version = target.scope_version
                          AND entitlement.consent_scope_hash = target.scope_hash
                          AND entitlement.provider_allowlist = ARRAY['hermes']::text[]
                          AND entitlement.model_allowlist = ARRAY[$9]::text[]
                          AND entitlement.mode_allowlist = $10::text[]
                          AND entitlement.fallback_policy = '{"enabled":false,"order":[]}'::jsonb
                        FOR SHARE OF binding, app_user, target, invitation,
                            invitation_target, redemption, authority
                        """,
                        uid,
                        role,
                        template_version,
                        target_mode,
                        lineage.policy_version,
                        lineage.processor_set_hash,
                        lineage.scope_version,
                        lineage.scope_hash,
                        model,
                        list(SELF_HOSTED_RUNTIME_TARGET_MODES),
                    )
                    if not row:
                        return None
                    decision = await voice_canary_db.revalidate_runtime_resolution_on_connection(
                        connection,
                        uid=uid,
                        admitted_entitlement_revision=int(row["target_entitlement_revision"]),
                        provider=SELF_HOSTED_RUNTIME_PROVIDER,
                        model=model,
                        mode=target_mode,
                    )
                    if not decision.allowed:
                        raise RuntimePoolClaimError(f"runtime_admission_{decision.code}")
                    await self._verify_self_hosted_honcho_attestation(
                        connection,
                        binding=dict(row),
                        uid=uid,
                        runtime_target_id=str(row["resolved_attestation_target_id"]),
                        allowed_job_states={("provisioning", "smoke_passed"), ("ready", "active")},
                        require_current_freshness=False,
                    )
                    result = dict(row)
                    result["runtime_target_id"] = str(row["resolved_target_id"])
                    result["attestation_runtime_target_id"] = str(row["resolved_attestation_target_id"])
                    result["runtime_target_mode"] = str(row["resolved_target_mode"])
                    result["target_endpoint_ref"] = str(row["resolved_target_endpoint_ref"] or "")
                    result["target_credential_ref"] = str(row["resolved_target_credential_ref"] or "")
                    result["target_entitlement_revision"] = int(row["target_entitlement_revision"])
                    result["runtime_target_updated_at"] = str(row["resolved_target_updated_at"])
                    result["consent_authority_epoch"] = str(row["resolved_authority_epoch"])
                    return result
        if required_provider not in {None, "hermes"}:
            raise ValueError("invalid_runtime_provider")
        if authority_lineage is not None:
            raise ValueError("retained_runtime_lineage_forbidden")
        row = await self.pool.fetchrow(
            """
            SELECT b.*, u.omi_uid, u.name, u.status AS user_status, u.profile_class
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE u.omi_uid = $1
              AND b.role = $2
              AND b.active = true
              AND b.provider <> 'hermes_cloud'
              AND ($3::text IS NULL OR b.template_version = $3)
              AND ($4::text IS NULL OR b.provider = $4)
            """,
            uid,
            role,
            template_version,
            required_provider,
        )
        return _row_dict(row)
