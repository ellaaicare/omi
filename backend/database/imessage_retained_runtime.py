"""Root-operator lifecycle for the one retained iMessage runtime binding."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional

import asyncpg

from database import authority_advisory_lock
from database.ella_provisioning import get_pool

CONTRACT_ID = "ella.imessage.retained_runtime.v1"


class RetainedImessageRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RetainedImessageRuntimeSpec:
    profile_name: str
    agent_id: str
    workspace_root: str
    internal_gateway_url: str
    gateway_port: int
    service_label: str
    credential_ref: str
    honcho_workspace: str
    observed_peer: str
    observer_peer: str
    template_version: str
    model_policy_version: str
    voice_policy_version: str


class RetainedImessageRuntimeRepository:
    def __init__(self, pool: asyncpg.Pool, *, owner_uid: str) -> None:
        if not owner_uid or owner_uid != owner_uid.strip():
            raise RetainedImessageRuntimeError("imessage_retained_owner_not_configured")
        self.pool = pool
        self.owner_uid = owner_uid

    @classmethod
    async def create(cls, *, owner_uid: str) -> "RetainedImessageRuntimeRepository":
        return cls(await get_pool(), owner_uid=owner_uid)

    async def stage(
        self,
        *,
        uid: str,
        spec: RetainedImessageRuntimeSpec,
        manifest_sha256: str,
    ) -> tuple[dict[str, Any], bool]:
        self._require_owner(uid)
        self._require_sha256(manifest_sha256, code="imessage_retained_manifest_invalid")
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                active = await connection.fetchval("SELECT status = 'ACTIVE' FROM users WHERE id = $1", user_id)
                if active is not True:
                    raise RetainedImessageRuntimeError("imessage_retained_owner_not_active")
                graph_present = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM ella_imessage_registration_attempts WHERE user_id = $1
                        UNION ALL
                        SELECT 1 FROM ella_imessage_channel_bindings WHERE user_id = $1
                        UNION ALL
                        SELECT 1 FROM ella_imessage_message_receipts WHERE user_id = $1
                    )
                    """,
                    user_id,
                )
                if graph_present is True:
                    raise RetainedImessageRuntimeError("imessage_retained_graph_not_empty")
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_runtime_bindings
                    WHERE user_id = $1 AND role = 'imessage'
                    FOR UPDATE
                    """,
                    user_id,
                )
                if existing:
                    self._require_exact(existing, spec=spec, manifest_sha256=manifest_sha256)
                    return dict(existing), False
                receipt = {
                    "contract": CONTRACT_ID,
                    "manifest_sha256": manifest_sha256,
                    "health_receipt_sha256": None,
                }
                row = await connection.fetchrow(
                    """
                    INSERT INTO ella_runtime_bindings (
                        id, user_id, account_user_id, profile_user_id,
                        role, provider, profile_name, agent_id, workspace_root,
                        internal_gateway_url, gateway_port, service_label,
                        credential_ref, honcho_workspace, observed_peer,
                        observer_peer, template_version, model_policy_version,
                        voice_policy_version, health_state, health_receipt,
                        revision, active, status
                    ) VALUES (
                        $1, $2, $2, $2, 'imessage', 'hermes', $3, $4, $5,
                        $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                        'pending', $16::jsonb, 1, FALSE, 'disabled'
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING *
                    """,
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{CONTRACT_ID}:{user_id}:{manifest_sha256}"),
                    user_id,
                    spec.profile_name,
                    spec.agent_id,
                    spec.workspace_root,
                    spec.internal_gateway_url,
                    spec.gateway_port,
                    spec.service_label,
                    spec.credential_ref,
                    spec.honcho_workspace,
                    spec.observed_peer,
                    spec.observer_peer,
                    spec.template_version,
                    spec.model_policy_version,
                    spec.voice_policy_version,
                    json.dumps(receipt, sort_keys=True),
                )
                if not row:
                    raise RetainedImessageRuntimeError("imessage_retained_physical_identity_conflict")
                return dict(row), True

    async def activate(
        self,
        *,
        uid: str,
        spec: RetainedImessageRuntimeSpec,
        manifest_sha256: str,
        health_receipt_sha256: str,
    ) -> tuple[dict[str, Any], bool]:
        self._require_owner(uid)
        self._require_sha256(manifest_sha256, code="imessage_retained_manifest_invalid")
        self._require_sha256(health_receipt_sha256, code="imessage_retained_health_receipt_invalid")
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                row = await connection.fetchrow(
                    """
                    SELECT binding.*,
                           EXISTS (
                               SELECT 1 FROM ella_runtime_targets target
                               WHERE target.runtime_binding_id = binding.id
                           ) AS has_target
                    FROM ella_runtime_bindings binding
                    JOIN users owner ON owner.id = binding.user_id
                    WHERE binding.user_id = $1
                      AND binding.role = 'imessage'
                      AND binding.provider = 'hermes'
                      AND owner.status = 'ACTIVE'
                    FOR UPDATE OF binding, owner
                    """,
                    user_id,
                )
                if not row:
                    raise RetainedImessageRuntimeError("imessage_retained_binding_missing")
                self._require_exact(row, spec=spec, manifest_sha256=manifest_sha256)
                receipt = self._receipt(row)
                if row["has_target"] is True:
                    raise RetainedImessageRuntimeError("imessage_retained_authority_changed")
                if row["active"] is True and str(row["status"]) == "active":
                    if receipt.get("health_receipt_sha256") != health_receipt_sha256:
                        raise RetainedImessageRuntimeError("imessage_retained_health_receipt_conflict")
                    return dict(row), False
                updated_receipt = {
                    "contract": CONTRACT_ID,
                    "manifest_sha256": manifest_sha256,
                    "health_receipt_sha256": health_receipt_sha256,
                }
                activated = await connection.fetchrow(
                    """
                    UPDATE ella_runtime_bindings
                    SET health_state = 'healthy',
                        health_receipt = $2::jsonb,
                        active = TRUE,
                        status = 'active',
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1 AND active = FALSE AND status = 'disabled'
                    RETURNING *
                    """,
                    row["id"],
                    json.dumps(updated_receipt, sort_keys=True),
                )
                if not activated:
                    raise RetainedImessageRuntimeError("imessage_retained_activation_conflict")
                return dict(activated), True

    async def rollback(self, *, uid: str, manifest_sha256: str) -> bool:
        self._require_owner(uid)
        self._require_sha256(manifest_sha256, code="imessage_retained_manifest_invalid")
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM ella_runtime_bindings
                    WHERE user_id = $1 AND role = 'imessage' AND provider = 'hermes'
                    FOR UPDATE
                    """,
                    user_id,
                )
                if not row:
                    return False
                if self._receipt(row).get("manifest_sha256") != manifest_sha256:
                    raise RetainedImessageRuntimeError("imessage_retained_manifest_conflict")
                in_use = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM ella_imessage_registration_attempts
                        WHERE runtime_binding_id = $1
                        UNION ALL
                        SELECT 1 FROM ella_imessage_channel_bindings
                        WHERE runtime_binding_id = $1
                        UNION ALL
                        SELECT 1 FROM ella_imessage_message_receipts
                        WHERE runtime_binding_id = $1
                    )
                    """,
                    row["id"],
                )
                if in_use is True:
                    raise RetainedImessageRuntimeError("imessage_retained_binding_in_use")
                result = await connection.execute("DELETE FROM ella_runtime_bindings WHERE id = $1", row["id"])
                return result == "DELETE 1"

    def _require_owner(self, uid: str) -> None:
        if uid != self.owner_uid:
            raise RetainedImessageRuntimeError("imessage_retained_owner_forbidden")

    @staticmethod
    def _require_sha256(value: str, *, code: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise RetainedImessageRuntimeError(code)

    @staticmethod
    def _receipt(row: Any) -> dict[str, Any]:
        receipt = row["health_receipt"] or {}
        if isinstance(receipt, str):
            try:
                receipt = json.loads(receipt)
            except json.JSONDecodeError as exc:
                raise RetainedImessageRuntimeError("imessage_retained_receipt_invalid") from exc
        if not isinstance(receipt, dict) or receipt.get("contract") != CONTRACT_ID:
            raise RetainedImessageRuntimeError("imessage_retained_receipt_invalid")
        return receipt

    @classmethod
    def _require_exact(
        cls,
        row: Any,
        *,
        spec: RetainedImessageRuntimeSpec,
        manifest_sha256: str,
    ) -> None:
        expected = asdict(spec)
        if (
            str(row["role"]) != "imessage"
            or str(row["provider"]) != "hermes"
            or any(row[field] != value for field, value in expected.items())
            or cls._receipt(row).get("manifest_sha256") != manifest_sha256
        ):
            raise RetainedImessageRuntimeError("imessage_retained_binding_conflict")
