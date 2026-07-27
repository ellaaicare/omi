"""Postgres access for isolated Ella provisioning and runtime bindings."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None
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
)
CLOUD_RUNTIME_TABLES = (
    "ella_runtime_session_scopes",
    "ella_runtime_interactions",
    "ella_runtime_ingestion_receipts",
    "ella_runtime_pool_alerts",
    "ella_photon_channel_bindings",
    "ella_photon_message_receipts",
    "ella_photon_quota_buckets",
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
)
REQUIRED_CLOUD_PROVISIONING_JOB_COLUMNS = (
    "external_side_effects",
    "rollback_receipt",
    "manual_intervention_at",
)
REQUIRED_CLOUD_RUNTIME_CONSTRAINTS = (
    "ella_runtime_bindings_claim_job_id_fkey",
    "ella_runtime_bindings_status_check",
    "ella_runtime_bindings_cloud_pool_shape_check",
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


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
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
                        WHERE conname = required.constraint_name
                    )
                    ORDER BY required.constraint_name
                ) AS missing_constraints,
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
        )
        missing: list[str] = []
        if row:
            missing.extend(f"table:{name}" for name in (row["missing_tables"] or []))
            missing.extend(f"index:{name}" for name in (row["missing_indexes"] or []))
            missing.extend(f"column:ella_runtime_bindings.{name}" for name in (row["missing_columns"] or []))
            missing.extend(f"column:ella_provisioning_jobs.{name}" for name in (row["missing_job_columns"] or []))
            missing.extend(f"constraint:{name}" for name in (row["missing_constraints"] or []))
            if row["binding_user_nullable"] != "YES":
                missing.append("column:ella_runtime_bindings.user_id_nullable")
        else:
            missing.append("cloud_runtime_schema_probe")
        if missing:
            raise ProvisioningSchemaNotReadyError(missing)

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
            async with connection.transaction():
                by_uid = await connection.fetchrow(
                    """
                    SELECT id, omi_uid, email, name, timezone
                    FROM users
                    WHERE omi_uid = $1
                    FOR UPDATE
                    """,
                    uid,
                )
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

                by_email = await connection.fetchrow(
                    """
                    SELECT id, omi_uid, email
                    FROM users
                    WHERE lower(email) = lower($1)
                    FOR UPDATE
                    """,
                    email,
                )
                if by_email:
                    existing_uid = by_email["omi_uid"]
                    if existing_uid and existing_uid != uid:
                        raise IdentityConflictError("email_owned_by_different_uid")
                    updated = await connection.fetchrow(
                        """
                        UPDATE users
                        SET omi_uid = $2,
                            name = $3,
                            timezone = $4,
                            identities = COALESCE(identities, '{}'::jsonb)
                                || jsonb_build_object('omi_uid', $2::text, 'email', email),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $5
                        RETURNING id, omi_uid, email, name, timezone, status
                        """,
                        email,
                        uid,
                        name,
                        timezone_name,
                        by_email["id"],
                    )
                    return dict(updated)

                user_id = uuid.uuid4()
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
                    user_id,
                    email,
                    name,
                    timezone_name,
                    uid,
                )
                return dict(inserted)

    async def get_user_identity(self, uid: str) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT id, omi_uid, email, name, timezone, status
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
        rows = await self.pool.fetch("""
            SELECT DISTINCT expected_model, model_policy_version
            FROM ella_runtime_bindings
            WHERE provider = 'hermes_cloud'
              AND status = 'pool_available'
              AND health_state = 'healthy'
              AND active = false
              AND user_id IS NULL
            """)
        if not rows:
            return None
        policies = {(str(row["expected_model"] or ""), str(row["model_policy_version"] or "")) for row in rows}
        if len(policies) != 1 or not all(next(iter(policies))):
            raise RuntimePoolClaimError("runtime_pool_policy_ambiguous")
        model, policy = next(iter(policies))
        return {"provider": "hermes_cloud", "model": model, "model_policy_version": policy}

    async def register_cloud_pool_binding(
        self,
        *,
        runtime_instance_id: str,
        profile_name: str,
        agent_id: str,
        api_base_url_ref: str,
        api_key_ref: str,
        honcho_api_key_ref: str,
        template_version: str,
        prompt_pack_version: str,
        prompt_artifact_receipt: dict[str, Any],
        model_policy_version: str,
        voice_policy_version: str,
        expected_model: str,
        allowed_tools: list[str],
        required_capabilities: list[str],
        health_receipt: dict[str, Any],
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
            honcho_api_key_ref,
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
            return dict(row)
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
        return dict(existing)

    async def list_cloud_pool_bindings(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch("""
            SELECT id, runtime_instance_id, status, health_state, expected_model,
                   prompt_pack_version, revision, claimed_at, quarantined_at,
                   quarantine_reason, created_at, updated_at
            FROM ella_runtime_bindings
            WHERE provider = 'hermes_cloud'
            ORDER BY created_at ASC, id ASC
            """)
        return [dict(row) for row in rows]

    async def claim_cloud_pool_binding(
        self,
        *,
        uid: str,
        job_id: str,
        lease_seconds: int,
    ) -> Optional[dict[str, Any]]:
        """Atomically reserve one healthy unbound Hermes Cloud instance.

        A reconnect for the same provisioning receipt gets the same claim.
        Claims are never recycled automatically after external side effects.
        """
        claim_token = uuid.uuid4()
        lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        job_uuid = uuid.UUID(str(job_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    """
                    SELECT b.*, u.omi_uid
                    FROM ella_runtime_bindings b
                    JOIN users u ON u.id = b.user_id
                    WHERE u.omi_uid = $1
                      AND b.provider = 'hermes_cloud'
                      AND b.claim_job_id = $2
                      AND b.status IN ('claiming', 'shadow', 'internal_canary', 'active')
                    FOR UPDATE
                    """,
                    uid,
                    job_uuid,
                )
                if existing:
                    return dict(existing)

                user_row = await connection.fetchrow(
                    "SELECT id FROM users WHERE omi_uid = $1 FOR UPDATE",
                    uid,
                )
                if not user_row:
                    raise LookupError("user_not_found")

                candidate = await connection.fetchrow("""
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
                      AND honcho_api_key_ref IS NOT NULL
                      AND prompt_artifact_receipt <> '{}'::jsonb
                      AND expected_model IS NOT NULL
                      AND jsonb_typeof(allowed_tools) = 'array'
                      AND jsonb_typeof(required_capabilities) = 'array'
                    ORDER BY updated_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """)
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
                        health_receipt = '{}'::jsonb,
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
                return result

    async def finalize_cloud_pool_claim(
        self,
        *,
        uid: str,
        job_id: str,
        claim_token: str,
        honcho_workspace: str,
        observed_peer: str,
        observer_peer: str,
        health_receipt: dict[str, Any],
        status: str = "internal_canary",
    ) -> dict[str, Any]:
        if status not in {"shadow", "internal_canary", "active"}:
            raise ValueError("invalid_cloud_binding_status")
        job_uuid = uuid.UUID(str(job_id))
        token_uuid = uuid.UUID(str(claim_token))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                selected = await connection.fetchrow(
                    """
                    SELECT b.*, u.omi_uid
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
        row = await self.pool.fetchrow(
            """
            UPDATE ella_runtime_bindings b
            SET status = 'quarantined',
                active = false,
                health_state = 'unhealthy',
                health_receipt = $4::jsonb,
                quarantine_reason = $5,
                quarantined_at = CURRENT_TIMESTAMP,
                claim_lease_expires_at = NULL,
                revision = revision + 1,
                updated_at = CURRENT_TIMESTAMP
            FROM users u
            WHERE b.user_id = u.id
              AND u.omi_uid = $1
              AND b.provider = 'hermes_cloud'
              AND b.claim_job_id = $2
              AND b.claim_token = $3
              AND b.status = 'claiming'
            RETURNING b.*
            """,
            uid,
            uuid.UUID(str(job_id)),
            uuid.UUID(str(claim_token)),
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
            SELECT b.*, u.omi_uid, u.name, u.status AS user_status
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

    async def promote_cloud_binding(
        self,
        *,
        uid: str,
        binding_id: str,
        expected_revision: int,
        target_status: str,
    ) -> dict[str, Any]:
        """Promote a shadow binding through an explicit revision-checked CAS."""
        if target_status not in {"internal_canary", "active"}:
            raise ValueError("invalid_cloud_promotion_status")
        row = await self.pool.fetchrow(
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
            RETURNING b.*
            """,
            uuid.UUID(str(binding_id)),
            uid,
            int(expected_revision),
            target_status,
        )
        if not row:
            raise RuntimePoolClaimError("runtime_cloud_promotion_conflict")
        return dict(row)

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
                u.omi_uid
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

    async def claim_photon_message(
        self,
        *,
        photon_binding_id: str,
        inbound_provider_message_key: str,
        inbound_payload_sha256: str,
        command_tier_version: str,
    ) -> dict[str, Any]:
        inserted = await self.pool.fetchrow(
            """
            INSERT INTO ella_photon_message_receipts (
                id, photon_binding_id, inbound_provider_message_key,
                inbound_payload_sha256, status, command_tier_version
            )
            VALUES ($1, $2, $3, $4, 'claimed', $5)
            ON CONFLICT (
                photon_binding_id, inbound_provider_message_key
            ) DO NOTHING
            RETURNING *
            """,
            uuid.uuid4(),
            uuid.UUID(str(photon_binding_id)),
            inbound_provider_message_key,
            inbound_payload_sha256,
            command_tier_version,
        )
        if inserted:
            result = dict(inserted)
            result["inserted"] = True
            return result
        existing = await self.pool.fetchrow(
            """
            SELECT *
            FROM ella_photon_message_receipts
            WHERE photon_binding_id = $1
              AND inbound_provider_message_key = $2
            """,
            uuid.UUID(str(photon_binding_id)),
            inbound_provider_message_key,
        )
        if not existing:
            raise RuntimePoolClaimError("photon_message_claim_lost")
        result = dict(existing)
        if str(result["inbound_payload_sha256"]) != inbound_payload_sha256:
            raise RuntimePoolClaimError("photon_duplicate_payload_conflict")
        result["inserted"] = False
        return result

    async def reserve_photon_quota(
        self,
        *,
        receipt_id: str,
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
                if receipt["quota_reserved"]:
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
                    RETURNING *
                    """,
                    receipt_uuid,
                )
                if not updated:
                    raise RuntimePoolClaimError("photon_message_claim_conflict")
                return dict(updated)

    async def complete_photon_message(
        self,
        *,
        receipt_id: str,
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
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status = 'running'
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
        )
        if not row:
            raise RuntimePoolClaimError("photon_message_completion_conflict")
        return dict(row)

    async def fail_photon_message(
        self,
        *,
        receipt_id: str,
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
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
              AND status IN ('claimed', 'running')
            """,
            uuid.UUID(str(receipt_id)),
            "uncertain" if uncertain else "failed",
            error_code[:120],
            provider_started,
        )

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
            raise LookupError("provisioning_job_not_found")
        return dict(row)

    async def stage_runtime_binding(self, *, uid: str, binding: dict[str, Any]) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            INSERT INTO ella_runtime_bindings (
                id, user_id, role, provider, profile_name, agent_id, workspace_root,
                internal_gateway_url, gateway_port, service_label, credential_ref,
                honcho_workspace, observed_peer, observer_peer, template_version,
                model_policy_version, voice_policy_version, health_state,
                health_receipt, revision, active
            )
            SELECT
                $1, u.id, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19::jsonb, 1, false
            FROM users u
            WHERE u.omi_uid = $2
            ON CONFLICT (user_id, role, provider)
            WHERE provider <> 'hermes_cloud'
            DO UPDATE
            SET profile_name = EXCLUDED.profile_name,
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
            uuid.uuid4(),
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
        return dict(row)

    async def activate_runtime_binding(
        self,
        *,
        uid: str,
        provider: str,
        role: str = "user",
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
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

                await connection.execute(
                    """
                    UPDATE ella_runtime_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND role = $2 AND active = true
                    """,
                    selected["user_id"],
                    role,
                )
                activated = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings
                    SET active = true,
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
        result = await self.pool.execute(
            """
            UPDATE users
            SET status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP
            WHERE omi_uid = $1
            """,
            uid,
        )
        if result == "UPDATE 0":
            raise LookupError("user_not_found")

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
    ) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT b.*, u.omi_uid, u.name, u.status AS user_status
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE u.omi_uid = $1
              AND b.role = $2
              AND b.active = true
              AND (b.provider <> 'hermes_cloud' OR b.status IN ('internal_canary', 'active'))
              AND ($3::text IS NULL OR b.template_version = $3)
            """,
            uid,
            role,
            template_version,
        )
        return _row_dict(row)
