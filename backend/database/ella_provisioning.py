"""Postgres access for isolated Ella provisioning and runtime bindings."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


class IdentityConflictError(RuntimeError):
    """The verified Firebase identity conflicts with an existing Ella user."""


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


class EllaProvisioningRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def create(cls) -> "EllaProvisioningRepository":
        return cls(await get_pool())

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
                                || jsonb_build_object('omi_uid', $1, 'email', email),
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
                                || jsonb_build_object('omi_uid', $2, 'email', email),
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
                        jsonb_build_object('omi_uid', $5, 'email', $2),
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
    ) -> bool:
        """Create the upstream OMI identity without granting data permissions."""

        def _ensure() -> bool:
            from database._client import db

            user_ref = db.collection("users").document(uid)
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
                        "private_cloud_sync_enabled": False,
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
                    "private_cloud_sync_enabled": False,
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
              AND state <> 'ready'
              AND state <> 'blocked'
              AND (
                    state <> 'provisioning'
                    OR updated_at < CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                  )
            RETURNING *
            """,
            uuid.UUID(str(job_id)),
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
            ON CONFLICT (user_id, role, provider) DO UPDATE
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
              AND ($3::text IS NULL OR b.template_version = $3)
            """,
            uid,
            role,
            template_version,
        )
        return _row_dict(row)
