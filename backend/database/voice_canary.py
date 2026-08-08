"""PostgreSQL authority for the Phase-1 realtime voice canary.

The billing/operations ledger is deliberately content-free. Transcript text and
audio never enter these tables.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from database import authority_advisory_lock
from database.honcho_attestation import authority_credential, retain_authority_credential

DEFAULT_DAILY_LIMIT_S = 45 * 60
DEFAULT_MONTHLY_LIMIT_S = 12 * 60 * 60
DEFAULT_MAX_SESSION_S = 20 * 60
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_SOFT_LIMIT_RATIO = 0.8
VOICE_SESSION_LEASE_SECONDS = int(os.getenv("ELLA_VOICE_SESSION_LEASE_SECONDS", "45"))
TOKEN_ISSUANCE_LIMIT_PER_MINUTE = int(os.getenv("ELLA_VOICE_TOKEN_ISSUANCE_LIMIT_PER_MINUTE", "5"))
SOCKET_ACCEPT_LIMIT_PER_MINUTE = int(os.getenv("ELLA_VOICE_SOCKET_ACCEPT_LIMIT_PER_MINUTE", "8"))
AUTH_FAILURE_ALERT_THRESHOLD = int(os.getenv("ELLA_VOICE_AUTH_FAILURE_ALERT_THRESHOLD", "10"))

_pool: Optional[asyncpg.Pool] = None


@dataclass(frozen=True)
class VoicePolicyDecision:
    allowed: bool
    code: str
    entitlement: Optional[dict[str, Any]]
    quota: dict[str, Any]
    resets_at: Optional[str] = None
    soft_warning: bool = False

    def detail(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "quota": self.quota,
        }
        if self.resets_at:
            result["resets_at"] = self.resets_at
        return result


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    value = now.astimezone(timezone.utc)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(now: datetime) -> datetime:
    start = _month_start(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _as_int(value: Any) -> int:
    return int(value or 0)


def _as_float(value: Any) -> float:
    return float(value or 0)


def _record_dict(record: Any) -> dict[str, Any]:
    return dict(record) if record else {}


def _authority_lock_key(scope_type: str, scope_value: str) -> int:
    payload = f"ella-voice-authority-v1\0{scope_type}\0{scope_value}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big", signed=True)


def _channel_for_mode(mode: Optional[str]) -> str:
    return "photon" if mode == "hermes-cloud-photon" else ""


async def lock_runtime_authority_on_connection(
    conn: asyncpg.Connection,
    *,
    uid: str,
    provider: str = "",
    mode: Optional[str] = None,
) -> None:
    """Serialize admission reads with every matching authority/quota writer."""
    scopes = [("global", "*")]
    if provider:
        scopes.append(("provider", provider))
    channel = _channel_for_mode(mode)
    if channel:
        scopes.append(("channel", channel))
    scopes.append(("user", uid))
    for scope_type, scope_value in scopes:
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1)",
            _authority_lock_key(scope_type, scope_value),
        )


async def set_kill_switch_on_connection(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_value: str,
    enabled: bool,
    reason: str,
    updated_by: str,
) -> dict[str, Any]:
    """Mutate one kill switch under the same lock read by admission."""
    if scope_type not in {"global", "user", "provider", "channel"}:
        raise ValueError("invalid_kill_switch_scope")
    normalized_value = "*" if scope_type == "global" else str(scope_value or "").strip()
    if not normalized_value:
        raise ValueError("kill_switch_scope_value_required")
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1)",
        _authority_lock_key(scope_type, normalized_value),
    )
    row = await conn.fetchrow(
        """
        INSERT INTO voice_kill_switches (
            scope_type, scope_value, enabled, reason, updated_by
        ) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (scope_type, scope_value) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            reason = EXCLUDED.reason,
            revision = voice_kill_switches.revision + 1,
            updated_by = EXCLUDED.updated_by,
            updated_at = NOW()
        RETURNING *
        """,
        scope_type,
        normalized_value,
        bool(enabled),
        reason,
        updated_by,
    )
    return _record_dict(row)


async def set_kill_switch(
    *,
    scope_type: str,
    scope_value: str,
    enabled: bool,
    reason: str,
    updated_by: str,
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await set_kill_switch_on_connection(
                conn,
                scope_type=scope_type,
                scope_value=scope_value,
                enabled=enabled,
                reason=reason,
                updated_by=updated_by,
            )


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _merge_provider_request_ids(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in _json_list(value) if not isinstance(value, list) else [str(item) for item in value]:
            normalized = item.strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
    return merged[-50:]


def quota_state(
    entitlement: dict[str, Any],
    *,
    daily_used_s: float,
    monthly_used_s: float,
    session_used_s: float = 0,
    audio_bytes: int = 0,
    daily_cost_used_microusd: int = 0,
    monthly_cost_used_microusd: int = 0,
) -> tuple[str, bool]:
    """Classify typed quota state without I/O so tests can cover boundary math."""
    daily_limit = _as_float(entitlement.get("daily_limit_s"))
    monthly_limit = _as_float(entitlement.get("monthly_limit_s"))
    max_session = _as_float(entitlement.get("max_session_s"))
    max_audio_bytes = _as_int(entitlement.get("max_audio_bytes_per_session"))
    hard_ratio = _as_float(entitlement.get("hard_limit_ratio")) or 1.0
    soft_ratio = _as_float(entitlement.get("soft_limit_ratio")) or DEFAULT_SOFT_LIMIT_RATIO
    daily_cost_limit = entitlement.get("daily_cost_limit_microusd")
    monthly_cost_limit = entitlement.get("monthly_cost_limit_microusd")

    if max_session and session_used_s >= max_session * hard_ratio:
        return "session_max", False
    if max_audio_bytes and audio_bytes >= max_audio_bytes:
        return "audio_limit", False
    if daily_limit and daily_used_s >= daily_limit * hard_ratio:
        return "quota_daily", False
    if monthly_limit and monthly_used_s >= monthly_limit * hard_ratio:
        return "quota_monthly", False
    if daily_cost_limit is not None and daily_cost_used_microusd >= _as_int(daily_cost_limit) * hard_ratio:
        return "cost_daily", False
    if monthly_cost_limit is not None and monthly_cost_used_microusd >= _as_int(monthly_cost_limit) * hard_ratio:
        return "cost_monthly", False

    soft_warning = any(
        (
            bool(max_session and session_used_s >= max_session * soft_ratio),
            bool(daily_limit and daily_used_s >= daily_limit * soft_ratio),
            bool(monthly_limit and monthly_used_s >= monthly_limit * soft_ratio),
            bool(max_audio_bytes and audio_bytes >= max_audio_bytes * soft_ratio),
        )
    )
    return ("soft_warning" if soft_warning else "ok"), soft_warning


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.getenv("ELLA_POSTGRES_DSN", "").strip()
        if dsn:
            retain_authority_credential(dsn)
            _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
        else:
            _pool = await asyncpg.create_pool(
                host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
                port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
                user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
                password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
                database=os.getenv("ELLA_POSTGRES_DATABASE", os.getenv("ELLA_POSTGRES_DB", "ella_ai")),
                min_size=1,
                max_size=10,
            )
    return _pool


async def _append_event(
    conn: asyncpg.Connection,
    *,
    event_type: str,
    uid: str,
    correlation_id: str,
    session_id: Optional[str] = None,
    entitlement_revision: Optional[int] = None,
    provider: str = "",
    model: str = "",
    mode: str = "",
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    input_audio_s: float = 0,
    output_audio_s: float = 0,
    connection_s: float = 0,
    input_audio_bytes: int = 0,
    output_audio_bytes: int = 0,
    tool_calls: int = 0,
    reconnects: int = 0,
    provider_request_ids: Optional[list[str]] = None,
    termination_reason: Optional[str] = None,
    normalized_error_code: Optional[str] = None,
    estimated_cost_microusd: int = 0,
    reconciled_cost_microusd: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    event_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO voice_usage_events (
            id, event_type, uid, session_id, correlation_id,
            entitlement_revision, provider, model, mode, started_at, ended_at,
            input_audio_s, output_audio_s, connection_s,
            input_audio_bytes, output_audio_bytes, tool_calls, reconnects,
            provider_request_ids, termination_reason, normalized_error_code,
            estimated_cost_microusd, reconciled_cost_microusd, metadata
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17, $18, $19::jsonb, $20, $21,
            $22, $23, $24::jsonb
        )
        """,
        event_id,
        event_type,
        uid,
        session_id,
        correlation_id,
        entitlement_revision,
        provider,
        model,
        mode,
        started_at,
        ended_at,
        Decimal(str(max(0, input_audio_s))),
        Decimal(str(max(0, output_audio_s))),
        Decimal(str(max(0, connection_s))),
        max(0, input_audio_bytes),
        max(0, output_audio_bytes),
        max(0, tool_calls),
        max(0, reconnects),
        json.dumps(provider_request_ids or []),
        termination_reason,
        normalized_error_code,
        max(0, estimated_cost_microusd),
        reconciled_cost_microusd,
        json.dumps(metadata or {}),
    )
    return event_id


async def _expire_stale_sessions(
    conn: asyncpg.Connection,
    now: datetime,
    *,
    uid: str,
) -> None:
    stale_before = now - timedelta(seconds=VOICE_SESSION_LEASE_SECONDS)
    stale = await conn.fetch(
        """
        DELETE FROM voice_active_sessions
        WHERE last_seen_at < $1
          AND uid = $2
        RETURNING *
        """,
        stale_before,
        uid,
    )
    for record in stale:
        row = _record_dict(record)
        connection_s = max(0.0, (now - row["accepted_at"]).total_seconds())
        await _append_event(
            conn,
            event_type="session_terminated",
            uid=row["uid"],
            session_id=row["session_id"],
            correlation_id=row["correlation_id"],
            entitlement_revision=row["entitlement_revision"],
            provider=row["provider"],
            model=row["model"],
            mode=row["mode"],
            started_at=row["accepted_at"],
            ended_at=now,
            input_audio_s=_as_float(row.get("input_audio_s")),
            output_audio_s=_as_float(row.get("output_audio_s")),
            connection_s=connection_s,
            input_audio_bytes=row["input_audio_bytes"],
            output_audio_bytes=row["output_audio_bytes"],
            tool_calls=row["tool_calls"],
            reconnects=row["reconnects"],
            provider_request_ids=_json_list(row["provider_request_ids"]),
            termination_reason="lease_expired",
            normalized_error_code="session_lease_expired",
            estimated_cost_microusd=_as_int(row.get("estimated_cost_microusd")),
        )


async def _usage_rollup(
    conn: asyncpg.Connection,
    uid: str,
    now: datetime,
) -> dict[str, Any]:
    day_start = _day_start(now)
    month_start = _month_start(now)
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(connection_s) FILTER (WHERE ended_at >= $2), 0) AS daily_used_s,
            COALESCE(SUM(connection_s) FILTER (WHERE ended_at >= $3), 0) AS monthly_used_s,
            COALESCE(SUM(estimated_cost_microusd) FILTER (WHERE ended_at >= $2), 0) AS daily_cost_microusd,
            COALESCE(SUM(estimated_cost_microusd) FILTER (WHERE ended_at >= $3), 0) AS monthly_cost_microusd
        FROM voice_usage_events
        WHERE uid = $1
          AND event_type IN ('session_completed', 'session_terminated')
        """,
        uid,
        day_start,
        month_start,
    )
    active = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(EXTRACT(EPOCH FROM ($2 - accepted_at))), 0) AS active_s,
            COALESCE(SUM(estimated_cost_microusd), 0) AS active_cost_microusd,
            COUNT(*) AS active_count
        FROM voice_active_sessions
        WHERE uid = $1
        """,
        uid,
        now,
    )
    active_s = _as_float(active["active_s"] if active else 0)
    active_cost_microusd = _as_int(active["active_cost_microusd"] if active else 0)
    return {
        "daily_used_s": _as_float(row["daily_used_s"] if row else 0) + active_s,
        "monthly_used_s": _as_float(row["monthly_used_s"] if row else 0) + active_s,
        "daily_cost_microusd": _as_int(row["daily_cost_microusd"] if row else 0) + active_cost_microusd,
        "monthly_cost_microusd": _as_int(row["monthly_cost_microusd"] if row else 0) + active_cost_microusd,
        "active_count": _as_int(active["active_count"] if active else 0),
        "daily_resets_at": (day_start + timedelta(days=1)).isoformat(),
        "monthly_resets_at": _next_month(now).isoformat(),
    }


async def _kill_switch_code(
    conn: asyncpg.Connection,
    uid: str,
    provider: str,
    mode: Optional[str] = None,
) -> Optional[str]:
    channel = _channel_for_mode(mode)
    rows = await conn.fetch(
        """
        SELECT scope_type
        FROM voice_kill_switches
        WHERE enabled = TRUE
          AND (
              (scope_type = 'global' AND scope_value = '*')
              OR (scope_type = 'user' AND scope_value = $1)
              OR (scope_type = 'provider' AND scope_value = $2)
              OR (scope_type = 'channel' AND scope_value = $3)
          )
        """,
        uid,
        provider,
        channel,
    )
    scopes = {row["scope_type"] for row in rows}
    if "global" in scopes:
        return "voice_disabled"
    if "user" in scopes:
        return "user_disabled"
    if "provider" in scopes:
        return "provider_disabled"
    if "channel" in scopes:
        return "channel_disabled"
    return None


def _quota_payload(entitlement: dict[str, Any], rollup: dict[str, Any]) -> dict[str, Any]:
    return {
        "daily_used_s": round(_as_float(rollup.get("daily_used_s")), 3),
        "daily_limit_s": _as_int(entitlement.get("daily_limit_s")),
        "monthly_used_s": round(_as_float(rollup.get("monthly_used_s")), 3),
        "monthly_limit_s": _as_int(entitlement.get("monthly_limit_s")),
        "max_session_s": _as_int(entitlement.get("max_session_s")),
        "max_concurrent": _as_int(entitlement.get("max_concurrent")),
        "soft_limit_ratio": _as_float(entitlement.get("soft_limit_ratio")),
        "resets_at": rollup.get("daily_resets_at"),
        "monthly_resets_at": rollup.get("monthly_resets_at"),
    }


async def get_entitlement(uid: str) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM voice_entitlements WHERE uid = $1", uid)
        return _record_dict(row) or None


async def upsert_entitlement(
    *,
    uid: str,
    plan: str,
    daily_limit_s: int,
    monthly_limit_s: int,
    max_session_s: int,
    max_concurrent: int,
    soft_limit_ratio: float,
    provider_allowlist: list[str],
    mode_allowlist: list[str],
    fallback_policy: dict[str, Any],
    operator_note: str,
) -> dict[str, Any]:
    """Create or reactivate an entitlement under the cross-service owner lock."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await authority_advisory_lock.resolve_self_owner_unlocked(conn, uid=uid)
        async with conn.transaction():
            owner_lock = await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
            await lock_runtime_authority_on_connection(conn, uid=uid)
            await authority_advisory_lock.verify_self_owner_after_lock(
                conn,
                uid=uid,
                owner=owner,
                proof=owner_lock,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO voice_entitlements (
                    uid, status, plan, daily_limit_s, monthly_limit_s,
                    max_session_s, max_concurrent, soft_limit_ratio,
                    provider_allowlist, mode_allowlist, fallback_policy, operator_note
                ) VALUES (
                    $1, 'active', $2, $3, $4, $5, $6, $7, $8::text[], $9::text[],
                    $10::jsonb, $11
                )
                ON CONFLICT (uid) DO UPDATE SET
                    status = 'active',
                    plan = EXCLUDED.plan,
                    revision = voice_entitlements.revision + 1,
                    daily_limit_s = EXCLUDED.daily_limit_s,
                    monthly_limit_s = EXCLUDED.monthly_limit_s,
                    max_session_s = EXCLUDED.max_session_s,
                    max_concurrent = EXCLUDED.max_concurrent,
                    soft_limit_ratio = EXCLUDED.soft_limit_ratio,
                    provider_allowlist = EXCLUDED.provider_allowlist,
                    mode_allowlist = EXCLUDED.mode_allowlist,
                    fallback_policy = EXCLUDED.fallback_policy,
                    operator_note = EXCLUDED.operator_note,
                    updated_at = NOW()
                RETURNING *
                """,
                uid,
                plan,
                daily_limit_s,
                monthly_limit_s,
                max_session_s,
                max_concurrent,
                soft_limit_ratio,
                provider_allowlist,
                mode_allowlist,
                json.dumps(fallback_policy),
                operator_note,
            )
            return _record_dict(row)


async def update_entitlement_status(
    *,
    uid: str,
    status: str,
    operator_note: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Suspend, revoke, or expire an entitlement under the shared owner lock."""
    if status not in {"suspended", "revoked", "expired"}:
        raise ValueError("invalid_entitlement_status")
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await authority_advisory_lock.resolve_self_owner_unlocked(conn, uid=uid)
        async with conn.transaction():
            owner_lock = await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
            await lock_runtime_authority_on_connection(conn, uid=uid)
            await authority_advisory_lock.verify_self_owner_after_lock(
                conn,
                uid=uid,
                owner=owner,
                proof=owner_lock,
            )
            row = await conn.fetchrow(
                """
                UPDATE voice_entitlements
                SET status = $2,
                    revision = revision + 1,
                    operator_note = COALESCE($3, operator_note),
                    updated_at = NOW()
                WHERE uid = $1
                RETURNING *
                """,
                uid,
                status,
                operator_note,
            )
            return _record_dict(row) or None


async def _runtime_activation_decision(
    conn: asyncpg.Connection,
    *,
    uid: str,
    provider: str,
    model: str,
    admitted_entitlement_revision: Optional[int] = None,
    mode: Optional[str] = None,
    required_modes: tuple[str, ...] = (),
    require_active: bool = False,
) -> VoicePolicyDecision:
    now = _utcnow()
    await lock_runtime_authority_on_connection(
        conn,
        uid=uid,
        provider=provider,
        mode=mode,
    )
    row = await conn.fetchrow(
        "SELECT * FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
        uid,
    )
    entitlement = _record_dict(row)
    if not entitlement:
        return VoicePolicyDecision(False, "no_entitlement", None, {})
    rollup = await _usage_rollup(conn, uid, now)
    quota = _quota_payload(entitlement, rollup)

    code: Optional[str] = None
    if admitted_entitlement_revision is not None and entitlement["revision"] != admitted_entitlement_revision:
        code = "entitlement_stale"
    elif require_active and entitlement["status"] != "active":
        code = str(entitlement["status"])
    elif not require_active and entitlement["status"] not in {"invited", "active"}:
        code = str(entitlement["status"])
    elif entitlement.get("trial_expires_at") and entitlement["trial_expires_at"] <= now:
        code = "expired"
    else:
        code = await _kill_switch_code(conn, uid, provider, mode)
    if not code and provider not in set(entitlement.get("provider_allowlist") or []):
        code = "provider_not_allowed"
    if not code and entitlement.get("model_allowlist") and model not in set(entitlement["model_allowlist"]):
        code = "model_not_allowed"
    allowed_modes = set(entitlement.get("mode_allowlist") or [])
    if not code and mode and mode not in allowed_modes:
        code = "mode_not_allowed"
    if not code and required_modes and not set(required_modes).issubset(allowed_modes):
        code = "mode_not_allowed"
    quota_code, soft_warning = quota_state(
        entitlement,
        daily_used_s=rollup["daily_used_s"],
        monthly_used_s=rollup["monthly_used_s"],
        daily_cost_used_microusd=rollup["daily_cost_microusd"],
        monthly_cost_used_microusd=rollup["monthly_cost_microusd"],
    )
    if not code and quota_code not in {"ok", "soft_warning"}:
        code = quota_code
    if code:
        return VoicePolicyDecision(False, code, entitlement, quota, quota.get("resets_at"))
    return VoicePolicyDecision(
        True,
        "soft_warning" if soft_warning else "ok",
        entitlement,
        quota,
        quota.get("resets_at"),
        soft_warning,
    )


async def revalidate_runtime_activation_on_connection(
    conn: asyncpg.Connection,
    *,
    uid: str,
    admitted_entitlement_revision: int,
    provider: str,
    model: str,
    required_modes: tuple[str, ...] = (),
    require_active: bool = False,
) -> VoicePolicyDecision:
    """Recheck admitted authority inside the caller's pool-claim transaction."""
    return await _runtime_activation_decision(
        conn,
        uid=uid,
        provider=provider,
        model=model,
        admitted_entitlement_revision=admitted_entitlement_revision,
        required_modes=required_modes,
        require_active=require_active,
    )


async def revalidate_runtime_resolution_on_connection(
    conn: asyncpg.Connection,
    *,
    uid: str,
    admitted_entitlement_revision: int,
    provider: str,
    model: str,
    mode: str,
) -> VoicePolicyDecision:
    """Recheck active per-mode authority before returning a direct Cloud target."""
    return await _runtime_activation_decision(
        conn,
        uid=uid,
        provider=provider,
        model=model,
        admitted_entitlement_revision=admitted_entitlement_revision,
        mode=mode,
        require_active=True,
    )


async def evaluate_runtime_activation(
    *,
    uid: str,
    provider: str,
    model: str,
) -> VoicePolicyDecision:
    """Apply ellaaicare/ella-ai#1113 entitlement and kill switches before binding a runtime.

    An invited entitlement may provision in the background, but only an active
    entitlement may later open a voice/provider session.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            decision = await _runtime_activation_decision(
                conn,
                uid=uid,
                provider=provider,
                model=model,
            )
            if not decision.allowed:
                await _append_event(
                    conn,
                    event_type="policy_denied",
                    uid=uid,
                    correlation_id=f"runtime-activation:{uuid.uuid4()}",
                    entitlement_revision=(decision.entitlement["revision"] if decision.entitlement else None),
                    provider=provider,
                    model=model,
                    mode="runtime_activation",
                    normalized_error_code=decision.code,
                )
            return decision


async def get_entitlement_contract_for_connection(
    conn: asyncpg.Connection,
    uid: str,
    *,
    now: Optional[datetime] = None,
    expire_stale_sessions: bool = True,
) -> dict[str, Any]:
    """Build the public entitlement contract inside an existing transaction."""
    current = now or _utcnow()
    await lock_runtime_authority_on_connection(conn, uid=uid)
    if expire_stale_sessions:
        await _expire_stale_sessions(conn, current, uid=uid)
    row = await conn.fetchrow("SELECT * FROM voice_entitlements WHERE uid = $1", uid)
    if not row:
        return {
            "status": "none",
            "quota": {
                "daily_used_s": 0,
                "daily_limit_s": 0,
                "monthly_used_s": 0,
                "monthly_limit_s": 0,
                "max_session_s": 0,
                "max_concurrent": 0,
                "soft_limit_ratio": DEFAULT_SOFT_LIMIT_RATIO,
                "resets_at": (_day_start(current) + timedelta(days=1)).isoformat(),
                "monthly_resets_at": _next_month(current).isoformat(),
            },
        }
    entitlement = _record_dict(row)
    rollup = await _usage_rollup(conn, uid, current)
    return {
        "status": entitlement["status"],
        "plan": entitlement["plan"],
        "revision": entitlement["revision"],
        "quota": _quota_payload(entitlement, rollup),
    }


async def get_entitlement_contract(uid: str) -> dict[str, Any]:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await get_entitlement_contract_for_connection(conn, uid, now=now)


async def evaluate_issuance(
    *,
    uid: str,
    provider: str,
    model: str,
    mode: str,
    correlation_id: str,
) -> VoicePolicyDecision:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await lock_runtime_authority_on_connection(
                conn,
                uid=uid,
                provider=provider,
                mode=mode,
            )
            await _expire_stale_sessions(conn, now, uid=uid)
            row = await conn.fetchrow(
                "SELECT * FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
                uid,
            )
            entitlement = _record_dict(row)
            if not entitlement:
                decision = VoicePolicyDecision(False, "no_entitlement", None, {})
                await _append_event(
                    conn,
                    event_type="policy_denied",
                    uid=uid,
                    correlation_id=correlation_id,
                    provider=provider,
                    model=model,
                    mode=mode,
                    normalized_error_code=decision.code,
                )
                return decision

            rollup = await _usage_rollup(conn, uid, now)
            quota = _quota_payload(entitlement, rollup)
            code: Optional[str] = None
            status = entitlement["status"]
            if status != "active":
                code = status
            elif entitlement.get("trial_expires_at") and entitlement["trial_expires_at"] <= now:
                code = "expired"
            else:
                code = await _kill_switch_code(conn, uid, provider, mode)

            if not code and provider not in set(entitlement.get("provider_allowlist") or []):
                code = "provider_not_allowed"
            if not code and entitlement.get("model_allowlist") and model not in set(entitlement["model_allowlist"]):
                code = "model_not_allowed"
            if not code and mode not in set(entitlement.get("mode_allowlist") or []):
                code = "mode_not_allowed"
            if not code and rollup["active_count"] >= _as_int(entitlement["max_concurrent"]):
                code = "concurrent"

            rate_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM voice_rate_limit_events
                WHERE uid = $1
                  AND event_type = 'token_issued'
                  AND occurred_at >= $2
                """,
                uid,
                now - timedelta(minutes=1),
            )
            if not code and _as_int(rate_count) >= TOKEN_ISSUANCE_LIMIT_PER_MINUTE:
                code = "rate_limited"

            quota_code, soft_warning = quota_state(
                entitlement,
                daily_used_s=rollup["daily_used_s"],
                monthly_used_s=rollup["monthly_used_s"],
                daily_cost_used_microusd=rollup["daily_cost_microusd"],
                monthly_cost_used_microusd=rollup["monthly_cost_microusd"],
            )
            if not code and quota_code not in {"ok", "soft_warning"}:
                code = quota_code

            if code:
                await _append_event(
                    conn,
                    event_type="policy_denied",
                    uid=uid,
                    correlation_id=correlation_id,
                    entitlement_revision=entitlement["revision"],
                    provider=provider,
                    model=model,
                    mode=mode,
                    normalized_error_code=code,
                )
                return VoicePolicyDecision(
                    False,
                    code,
                    entitlement,
                    quota,
                    quota.get("resets_at"),
                )

            await conn.execute(
                """
                INSERT INTO voice_rate_limit_events (uid, event_type, occurred_at)
                VALUES ($1, 'token_issued', $2)
                """,
                uid,
                now,
            )
            await _append_event(
                conn,
                event_type="token_issued",
                uid=uid,
                correlation_id=correlation_id,
                entitlement_revision=entitlement["revision"],
                provider=provider,
                model=model,
                mode=mode,
                started_at=now,
            )
            return VoicePolicyDecision(
                True,
                "soft_warning" if soft_warning else "ok",
                entitlement,
                quota,
                quota.get("resets_at"),
                soft_warning,
            )


async def accept_session(
    *,
    uid: str,
    session_id: str,
    correlation_id: str,
    entitlement_revision: int,
    provider: str,
    model: str,
    mode: str,
) -> VoicePolicyDecision:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await lock_runtime_authority_on_connection(
                conn,
                uid=uid,
                provider=provider,
                mode=mode,
            )
            await _expire_stale_sessions(conn, now, uid=uid)
            row = await conn.fetchrow(
                "SELECT * FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
                uid,
            )
            entitlement = _record_dict(row)
            if not entitlement:
                return VoicePolicyDecision(False, "no_entitlement", None, {})
            rollup = await _usage_rollup(conn, uid, now)
            quota = _quota_payload(entitlement, rollup)

            code: Optional[str] = None
            if entitlement["revision"] != entitlement_revision:
                code = "entitlement_stale"
            elif entitlement["status"] != "active":
                code = entitlement["status"]
            elif entitlement.get("trial_expires_at") and entitlement["trial_expires_at"] <= now:
                code = "expired"
            else:
                code = await _kill_switch_code(conn, uid, provider, mode)
            if not code and provider not in set(entitlement.get("provider_allowlist") or []):
                code = "provider_not_allowed"
            if not code and entitlement.get("model_allowlist") and model not in set(entitlement["model_allowlist"]):
                code = "model_not_allowed"
            if not code and mode not in set(entitlement.get("mode_allowlist") or []):
                code = "mode_not_allowed"
            if not code and rollup["active_count"] >= _as_int(entitlement["max_concurrent"]):
                code = "concurrent"

            existing = await conn.fetchval(
                "SELECT 1 FROM voice_active_sessions WHERE session_id = $1",
                session_id,
            )
            if not code and existing:
                code = "voice_session_replayed"

            accept_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM voice_rate_limit_events
                WHERE uid = $1
                  AND event_type = 'socket_accept'
                  AND occurred_at >= $2
                """,
                uid,
                now - timedelta(minutes=1),
            )
            if not code and _as_int(accept_count) >= SOCKET_ACCEPT_LIMIT_PER_MINUTE:
                code = "rate_limited"

            quota_code, soft_warning = quota_state(
                entitlement,
                daily_used_s=rollup["daily_used_s"],
                monthly_used_s=rollup["monthly_used_s"],
            )
            if not code and quota_code not in {"ok", "soft_warning"}:
                code = quota_code

            if code:
                await _append_event(
                    conn,
                    event_type="policy_denied",
                    uid=uid,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    entitlement_revision=entitlement["revision"],
                    provider=provider,
                    model=model,
                    mode=mode,
                    normalized_error_code=code,
                )
                return VoicePolicyDecision(False, code, entitlement, quota, quota.get("resets_at"))

            await conn.execute(
                """
                INSERT INTO voice_active_sessions (
                    session_id, uid, correlation_id, entitlement_revision,
                    provider, model, mode, accepted_at, last_seen_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
                """,
                session_id,
                uid,
                correlation_id,
                entitlement_revision,
                provider,
                model,
                mode,
                now,
            )
            await conn.execute(
                """
                INSERT INTO voice_rate_limit_events (uid, event_type, occurred_at)
                VALUES ($1, 'socket_accept', $2)
                """,
                uid,
                now,
            )
            await _append_event(
                conn,
                event_type="session_accepted",
                uid=uid,
                session_id=session_id,
                correlation_id=correlation_id,
                entitlement_revision=entitlement_revision,
                provider=provider,
                model=model,
                mode=mode,
                started_at=now,
            )
            return VoicePolicyDecision(
                True,
                "soft_warning" if soft_warning else "ok",
                entitlement,
                quota,
                quota.get("resets_at"),
                soft_warning,
            )


async def update_session(
    *,
    uid: str,
    session_id: str,
    input_audio_s: float,
    output_audio_s: float,
    input_audio_bytes: int,
    output_audio_bytes: int,
    tool_calls: int,
    reconnects: int,
    provider_request_ids: list[str],
    estimated_cost_microusd: int,
) -> VoicePolicyDecision:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            scope_row = await conn.fetchrow(
                """
                SELECT provider, mode
                FROM voice_active_sessions
                WHERE session_id = $1 AND uid = $2
                """,
                session_id,
                uid,
            )
            if not scope_row:
                return VoicePolicyDecision(False, "voice_session_not_active", None, {})
            await lock_runtime_authority_on_connection(
                conn,
                uid=uid,
                provider=str(scope_row["provider"]),
                mode=str(scope_row["mode"]),
            )
            active_row = await conn.fetchrow(
                """
                SELECT *
                FROM voice_active_sessions
                WHERE session_id = $1 AND uid = $2
                FOR UPDATE
                """,
                session_id,
                uid,
            )
            if not active_row:
                return VoicePolicyDecision(False, "voice_session_not_active", None, {})
            active = _record_dict(active_row)
            entitlement_row = await conn.fetchrow(
                "SELECT * FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
                uid,
            )
            entitlement = _record_dict(entitlement_row)
            if not entitlement:
                return VoicePolicyDecision(False, "no_entitlement", None, {})

            code: Optional[str] = None
            if entitlement["revision"] != active["entitlement_revision"]:
                code = "entitlement_stale"
            elif entitlement["status"] != "active":
                code = entitlement["status"]
            elif entitlement.get("trial_expires_at") and entitlement["trial_expires_at"] <= now:
                code = "expired"
            else:
                code = await _kill_switch_code(
                    conn,
                    uid,
                    active["provider"],
                    active["mode"],
                )

            provider_ids = _merge_provider_request_ids(
                active.get("provider_request_ids"),
                provider_request_ids,
            )
            updated_row = await conn.fetchrow(
                """
                UPDATE voice_active_sessions
                SET last_seen_at = $3,
                    input_audio_bytes = GREATEST(input_audio_bytes, $4),
                    output_audio_bytes = GREATEST(output_audio_bytes, $5),
                    input_audio_s = GREATEST(input_audio_s, $6),
                    output_audio_s = GREATEST(output_audio_s, $7),
                    tool_calls = GREATEST(tool_calls, $8),
                    reconnects = GREATEST(reconnects, $9),
                    provider_request_ids = $10::jsonb,
                    estimated_cost_microusd = GREATEST(estimated_cost_microusd, $11)
                WHERE session_id = $1 AND uid = $2
                RETURNING *
                """,
                session_id,
                uid,
                now,
                max(0, input_audio_bytes),
                max(0, output_audio_bytes),
                Decimal(str(max(0, input_audio_s))),
                Decimal(str(max(0, output_audio_s))),
                max(0, tool_calls),
                max(0, reconnects),
                json.dumps(provider_ids),
                max(0, estimated_cost_microusd),
            )
            updated = _record_dict(updated_row)
            rollup = await _usage_rollup(conn, uid, now)
            session_used_s = max(0.0, (now - active["accepted_at"]).total_seconds())
            quota = _quota_payload(entitlement, rollup)
            quota["session_used_s"] = round(session_used_s, 3)
            quota["audio_bytes"] = _as_int(updated.get("input_audio_bytes")) + _as_int(
                updated.get("output_audio_bytes")
            )
            quota_code, soft_warning = quota_state(
                entitlement,
                daily_used_s=rollup["daily_used_s"],
                monthly_used_s=rollup["monthly_used_s"],
                session_used_s=session_used_s,
                audio_bytes=quota["audio_bytes"],
                daily_cost_used_microusd=rollup["daily_cost_microusd"],
                monthly_cost_used_microusd=rollup["monthly_cost_microusd"],
            )
            if not code and quota_code not in {"ok", "soft_warning"}:
                code = quota_code
            if code:
                return VoicePolicyDecision(False, code, entitlement, quota, quota.get("resets_at"))
            return VoicePolicyDecision(
                True,
                "soft_warning" if soft_warning else "ok",
                entitlement,
                quota,
                quota.get("resets_at"),
                soft_warning,
            )


async def reserve_session_cost(
    *,
    uid: str,
    session_id: str,
    reservation_microusd: int,
) -> VoicePolicyDecision:
    """Atomically reserve worst-case provider cost before the provider call."""
    reservation = max(1, int(reservation_microusd))
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            scope_row = await conn.fetchrow(
                """
                SELECT provider, mode
                FROM voice_active_sessions
                WHERE session_id = $1 AND uid = $2
                """,
                session_id,
                uid,
            )
            if not scope_row:
                return VoicePolicyDecision(False, "voice_session_not_active", None, {})
            await lock_runtime_authority_on_connection(
                conn,
                uid=uid,
                provider=str(scope_row["provider"]),
                mode=str(scope_row["mode"]),
            )
            active_row = await conn.fetchrow(
                """
                SELECT *
                FROM voice_active_sessions
                WHERE session_id = $1 AND uid = $2
                FOR UPDATE
                """,
                session_id,
                uid,
            )
            if not active_row:
                return VoicePolicyDecision(False, "voice_session_not_active", None, {})
            active = _record_dict(active_row)
            entitlement_row = await conn.fetchrow(
                "SELECT * FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
                uid,
            )
            entitlement = _record_dict(entitlement_row)
            if not entitlement:
                return VoicePolicyDecision(False, "no_entitlement", None, {})
            rollup = await _usage_rollup(conn, uid, now)
            current = _as_int(active.get("estimated_cost_microusd"))
            daily_with_reservation = max(0, rollup["daily_cost_microusd"] - current) + reservation
            monthly_with_reservation = max(0, rollup["monthly_cost_microusd"] - current) + reservation
            code: Optional[str] = None
            if entitlement["revision"] != active["entitlement_revision"]:
                code = "entitlement_stale"
            elif entitlement["status"] != "active":
                code = entitlement["status"]
            elif entitlement.get("trial_expires_at") and entitlement["trial_expires_at"] <= now:
                code = "expired"
            else:
                code = await _kill_switch_code(
                    conn,
                    uid,
                    active["provider"],
                    active["mode"],
                )
            if not code and active["provider"] not in set(entitlement.get("provider_allowlist") or []):
                code = "provider_not_allowed"
            if (
                not code
                and entitlement.get("model_allowlist")
                and active["model"] not in set(entitlement["model_allowlist"])
            ):
                code = "model_not_allowed"
            if not code and active["mode"] not in set(entitlement.get("mode_allowlist") or []):
                code = "mode_not_allowed"
            quota_code, soft_warning = quota_state(
                entitlement,
                daily_used_s=rollup["daily_used_s"],
                monthly_used_s=rollup["monthly_used_s"],
                daily_cost_used_microusd=daily_with_reservation,
                monthly_cost_used_microusd=monthly_with_reservation,
            )
            if not code and quota_code not in {"ok", "soft_warning"}:
                code = quota_code
            quota = _quota_payload(
                entitlement,
                {
                    **rollup,
                    "daily_cost_microusd": daily_with_reservation,
                    "monthly_cost_microusd": monthly_with_reservation,
                },
            )
            quota["reserved_cost_microusd"] = reservation
            if code:
                return VoicePolicyDecision(False, code, entitlement, quota, quota.get("resets_at"))
            await conn.execute(
                """
                UPDATE voice_active_sessions
                SET estimated_cost_microusd = $3,
                    last_seen_at = $4
                WHERE session_id = $1 AND uid = $2
                """,
                session_id,
                uid,
                reservation,
                now,
            )
            return VoicePolicyDecision(
                True,
                "soft_warning" if soft_warning else "ok",
                entitlement,
                quota,
                quota.get("resets_at"),
                soft_warning,
            )


async def settle_session_cost(
    *,
    uid: str,
    session_id: str,
    actual_cost_microusd: int,
    tool_calls: int,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await lock_runtime_authority_on_connection(conn, uid=uid)
            result = await conn.execute(
                """
                UPDATE voice_active_sessions
                SET estimated_cost_microusd = $3,
                    tool_calls = GREATEST(tool_calls, $4),
                    last_seen_at = $5
                WHERE session_id = $1 AND uid = $2
                """,
                session_id,
                uid,
                max(0, int(actual_cost_microusd)),
                max(0, int(tool_calls)),
                _utcnow(),
            )
    if result == "UPDATE 0":
        raise LookupError("voice_session_not_active")


async def release_session_cost(*, uid: str, session_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await lock_runtime_authority_on_connection(conn, uid=uid)
            await conn.execute(
                """
                UPDATE voice_active_sessions
                SET estimated_cost_microusd = 0,
                    last_seen_at = $3
                WHERE session_id = $1 AND uid = $2
                """,
                session_id,
                uid,
                _utcnow(),
            )


async def _complete_session_on_connection(
    conn: asyncpg.Connection,
    *,
    uid: str,
    session_id: str,
    input_audio_s: float,
    output_audio_s: float,
    connection_s: float,
    input_audio_bytes: int,
    output_audio_bytes: int,
    tool_calls: int,
    reconnects: int,
    provider_request_ids: list[str],
    termination_reason: str,
    normalized_error_code: Optional[str],
    estimated_cost_microusd: int,
    now: datetime,
) -> Optional[str]:
    await lock_runtime_authority_on_connection(conn, uid=uid)
    active_row = await conn.fetchrow(
        """
        DELETE FROM voice_active_sessions
        WHERE session_id = $1 AND uid = $2
        RETURNING *
        """,
        session_id,
        uid,
    )
    if not active_row:
        return None
    active = _record_dict(active_row)
    provider_ids = _merge_provider_request_ids(
        active.get("provider_request_ids"),
        provider_request_ids,
    )
    server_connection_s = max(0.0, (now - active["accepted_at"]).total_seconds())
    event_type = (
        "session_completed"
        if termination_reason in {"client_disconnect", "signoff", "completed"}
        else "session_terminated"
    )
    return await _append_event(
        conn,
        event_type=event_type,
        uid=uid,
        session_id=session_id,
        correlation_id=active["correlation_id"],
        entitlement_revision=active["entitlement_revision"],
        provider=active["provider"],
        model=active["model"],
        mode=active["mode"],
        started_at=active["accepted_at"],
        ended_at=now,
        input_audio_s=max(_as_float(active.get("input_audio_s")), input_audio_s),
        output_audio_s=max(_as_float(active.get("output_audio_s")), output_audio_s),
        connection_s=server_connection_s,
        input_audio_bytes=max(_as_int(active.get("input_audio_bytes")), input_audio_bytes),
        output_audio_bytes=max(_as_int(active.get("output_audio_bytes")), output_audio_bytes),
        tool_calls=max(_as_int(active.get("tool_calls")), tool_calls),
        reconnects=max(_as_int(active.get("reconnects")), reconnects),
        provider_request_ids=provider_ids,
        termination_reason=termination_reason,
        normalized_error_code=normalized_error_code,
        estimated_cost_microusd=max(
            _as_int(active.get("estimated_cost_microusd")),
            estimated_cost_microusd,
        ),
    )


async def complete_session(
    *,
    uid: str,
    session_id: str,
    input_audio_s: float,
    output_audio_s: float,
    connection_s: float,
    input_audio_bytes: int,
    output_audio_bytes: int,
    tool_calls: int,
    reconnects: int,
    provider_request_ids: list[str],
    termination_reason: str,
    normalized_error_code: Optional[str],
    estimated_cost_microusd: int,
) -> Optional[str]:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _complete_session_on_connection(
                conn,
                uid=uid,
                session_id=session_id,
                input_audio_s=input_audio_s,
                output_audio_s=output_audio_s,
                connection_s=connection_s,
                input_audio_bytes=input_audio_bytes,
                output_audio_bytes=output_audio_bytes,
                tool_calls=tool_calls,
                reconnects=reconnects,
                provider_request_ids=provider_request_ids,
                termination_reason=termination_reason,
                normalized_error_code=normalized_error_code,
                estimated_cost_microusd=estimated_cost_microusd,
                now=now,
            )


async def record_auth_failure(
    *,
    uid: str,
    correlation_id: str,
    code: str,
) -> int:
    now = _utcnow()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO voice_rate_limit_events (uid, event_type, occurred_at)
                VALUES ($1, 'auth_failed', $2)
                """,
                uid,
                now,
            )
            await _append_event(
                conn,
                event_type="auth_failed",
                uid=uid,
                correlation_id=correlation_id,
                normalized_error_code=code,
            )
            return _as_int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM voice_rate_limit_events
                    WHERE event_type = 'auth_failed'
                      AND occurred_at >= $1
                    """,
                    now - timedelta(minutes=5),
                )
            )


async def delete_user_voice_data(uid: str) -> dict[str, int]:
    """Deletion exception to the append-only rule, used by the operator receipt."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await authority_advisory_lock.resolve_self_owner_unlocked(conn, uid=uid)
        async with conn.transaction():
            owner_lock = await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
            await lock_runtime_authority_on_connection(conn, uid=uid)
            await authority_advisory_lock.verify_self_owner_after_lock(
                conn,
                uid=uid,
                owner=owner,
                proof=owner_lock,
            )
            counts = {
                "active_sessions": _as_int(
                    await conn.fetchval("SELECT COUNT(*) FROM voice_active_sessions WHERE uid = $1", uid)
                ),
                "usage_events": _as_int(
                    await conn.fetchval("SELECT COUNT(*) FROM voice_usage_events WHERE uid = $1", uid)
                ),
                "rate_events": _as_int(
                    await conn.fetchval("SELECT COUNT(*) FROM voice_rate_limit_events WHERE uid = $1", uid)
                ),
                "entitlements": _as_int(
                    await conn.fetchval("SELECT COUNT(*) FROM voice_entitlements WHERE uid = $1", uid)
                ),
                "kill_switches": _as_int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM voice_kill_switches
                        WHERE scope_type = 'user' AND scope_value = $1
                        """,
                        uid,
                    )
                ),
            }
            await conn.execute("DELETE FROM voice_active_sessions WHERE uid = $1", uid)
            await conn.execute("DELETE FROM voice_usage_events WHERE uid = $1", uid)
            await conn.execute("DELETE FROM voice_rate_limit_events WHERE uid = $1", uid)
            await conn.execute(
                "DELETE FROM voice_kill_switches WHERE scope_type = 'user' AND scope_value = $1",
                uid,
            )
            await conn.execute("DELETE FROM voice_entitlements WHERE uid = $1", uid)
            return counts
