"""Atomic, privacy-preserving Ella invitation redemption."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from database import voice_canary

INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_RE = re.compile(rf"^[{INVITE_ALPHABET}]{{8}}$")
SAFE_POLICY_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_APP_BUILD_RE = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")


@dataclass(frozen=True)
class InvitationConfig:
    hmac_pepper: bytes
    redemption_enabled: bool = False
    ordinary_enabled: bool = False
    app_review_enabled: bool = False
    uid_failure_limit: int = 5
    uid_window: timedelta = timedelta(minutes=15)
    source_failure_limit: int = 20
    source_window: timedelta = timedelta(hours=1)
    anomaly_limit: int = 100
    anomaly_window: timedelta = timedelta(minutes=15)
    progressive_backoff_enabled: bool = True

    @classmethod
    def from_env(cls) -> "InvitationConfig":
        redemption_enabled = _env_flag("ELLA_INVITE_REDEMPTION_ENABLED")
        pepper = os.getenv("ELLA_INVITE_HMAC_PEPPER", "").encode("utf-8")
        if redemption_enabled and len(pepper) < 32:
            raise InviteConfigurationError("invite_hmac_pepper_missing")
        return cls(
            hmac_pepper=pepper or b"disabled-not-used",
            redemption_enabled=redemption_enabled,
            ordinary_enabled=_env_flag("ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED"),
            app_review_enabled=_env_flag("ELLA_INVITE_APP_REVIEW_ENABLED"),
            uid_failure_limit=_positive_env_int("ELLA_INVITE_UID_FAILURE_LIMIT", 5),
            source_failure_limit=_positive_env_int("ELLA_INVITE_SOURCE_FAILURE_LIMIT", 20),
            anomaly_limit=_positive_env_int("ELLA_INVITE_ANOMALY_LIMIT", 100),
        )


class InviteConfigurationError(RuntimeError):
    pass


class InviteRedemptionFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        support_code: str,
        correlation_id: str,
        retry_after_s: Optional[int] = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.support_code = support_code
        self.correlation_id = correlation_id
        self.retry_after_s = retry_after_s

    def detail(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "support_code": self.support_code,
            "correlation_id": self.correlation_id,
        }
        if self.retry_after_s is not None:
            result["retry_after_s"] = self.retry_after_s
        return result


def _env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise InviteConfigurationError(f"{name.lower()}_invalid") from exc
    if value <= 0:
        raise InviteConfigurationError(f"{name.lower()}_invalid")
    return value


def normalize_invite_code(value: str) -> str:
    normalized = "".join(character for character in value.upper() if character not in {" ", "-"})
    return normalized if INVITE_CODE_RE.fullmatch(normalized) else ""


def generate_invite_code() -> str:
    compact = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))
    return f"{compact[:4]}-{compact[4:]}"


def _hmac_ref(config: InvitationConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.hmac_pepper,
        f"{domain}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def code_hmac(config: InvitationConfig, normalized_code: str) -> str:
    if not INVITE_CODE_RE.fullmatch(normalized_code):
        raise ValueError("normalized invite code required")
    return _hmac_ref(config, "code-v1", normalized_code)


def _support_code() -> str:
    return f"INV-{secrets.token_hex(4).upper()}"


def _correlation_id() -> str:
    return str(uuid.uuid4())


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _bounded_int(policy: dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    value = policy.get(key)
    if isinstance(value, bool):
        raise InviteConfigurationError("invite_policy_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise InviteConfigurationError("invite_policy_invalid") from exc
    if normalized < minimum or normalized > maximum:
        raise InviteConfigurationError("invite_policy_invalid")
    return normalized


def _safe_values(policy: dict[str, Any], key: str, *, required: bool) -> list[str]:
    value = policy.get(key, [])
    if not isinstance(value, list):
        raise InviteConfigurationError("invite_policy_invalid")
    normalized = [str(item).strip().lower() for item in value]
    if (required and not normalized) or len(normalized) > 16:
        raise InviteConfigurationError("invite_policy_invalid")
    if any(not SAFE_POLICY_VALUE_RE.fullmatch(item) for item in normalized):
        raise InviteConfigurationError("invite_policy_invalid")
    return normalized


def normalize_entitlement_policy(value: Any) -> dict[str, Any]:
    policy = _json_object(value)
    plan = str(policy.get("plan", "")).strip().lower()
    if not SAFE_POLICY_VALUE_RE.fullmatch(plan):
        raise InviteConfigurationError("invite_policy_invalid")
    fallback = _json_object(policy.get("fallback_policy", {}))
    if fallback.get("enabled") is not False or fallback.get("order") not in ([], None):
        raise InviteConfigurationError("invite_policy_invalid")

    try:
        soft_limit_ratio = float(policy.get("soft_limit_ratio", 0.8))
        hard_limit_ratio = float(policy.get("hard_limit_ratio", 1.0))
    except (TypeError, ValueError) as exc:
        raise InviteConfigurationError("invite_policy_invalid") from exc
    if (
        not math.isfinite(soft_limit_ratio)
        or not math.isfinite(hard_limit_ratio)
        or not 0 < soft_limit_ratio < 1
        or not 1 <= hard_limit_ratio <= 1.25
    ):
        raise InviteConfigurationError("invite_policy_invalid")

    return {
        "plan": plan,
        "daily_limit_s": _bounded_int(policy, "daily_limit_s", minimum=60, maximum=86400),
        "monthly_limit_s": _bounded_int(policy, "monthly_limit_s", minimum=60, maximum=2678400),
        "max_session_s": _bounded_int(policy, "max_session_s", minimum=60, maximum=14400),
        "max_concurrent": _bounded_int(policy, "max_concurrent", minimum=1, maximum=4),
        "max_audio_bytes_per_session": _bounded_int(
            policy,
            "max_audio_bytes_per_session",
            minimum=1_000_000,
            maximum=1_000_000_000,
        ),
        "max_audio_bytes_per_minute": _bounded_int(
            policy,
            "max_audio_bytes_per_minute",
            minimum=100_000,
            maximum=100_000_000,
        ),
        "soft_limit_ratio": soft_limit_ratio,
        "hard_limit_ratio": hard_limit_ratio,
        "provider_allowlist": _safe_values(policy, "provider_allowlist", required=True),
        "model_allowlist": _safe_values(policy, "model_allowlist", required=False),
        "mode_allowlist": _safe_values(policy, "mode_allowlist", required=True),
        "fallback_policy": {"enabled": False, "order": []},
    }


async def _lock_rate_keys(
    conn: asyncpg.Connection,
    uid_ref_hmac: str,
    source_ref_hmac: str,
) -> None:
    for value in sorted((f"uid:{uid_ref_hmac}", f"source:{source_ref_hmac}")):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", value)


async def _rate_limit_retry_after(
    conn: asyncpg.Connection,
    *,
    uid_ref_hmac: str,
    source_ref_hmac: str,
    now: datetime,
    config: InvitationConfig,
) -> Optional[int]:
    uid_row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS count, MIN(occurred_at) AS oldest, MAX(occurred_at) AS newest
        FROM ella_invitation_rate_limit_events
        WHERE uid_ref_hmac = $1 AND occurred_at >= $2
        """,
        uid_ref_hmac,
        now - config.uid_window,
    )
    source_row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS count, MIN(occurred_at) AS oldest
        FROM ella_invitation_rate_limit_events
        WHERE source_ref_hmac = $1 AND occurred_at >= $2
        """,
        source_ref_hmac,
        now - config.source_window,
    )
    deadlines: list[datetime] = []
    uid_count = int(uid_row["count"] or 0)
    source_count = int(source_row["count"] or 0)
    if uid_count >= config.uid_failure_limit and uid_row["oldest"]:
        deadlines.append(uid_row["oldest"] + config.uid_window)
    if source_count >= config.source_failure_limit and source_row["oldest"]:
        deadlines.append(source_row["oldest"] + config.source_window)
    if config.progressive_backoff_enabled and uid_count >= 2 and uid_row["newest"]:
        backoff = min(60, 2 ** min(uid_count - 2, 6))
        deadlines.append(uid_row["newest"] + timedelta(seconds=backoff))
    active = [deadline for deadline in deadlines if deadline > now]
    if not active:
        return None
    return max(1, math.ceil((max(active) - now).total_seconds()))


async def _record_audit(
    conn: asyncpg.Connection,
    *,
    invitation_id: Optional[uuid.UUID],
    uid_ref_hmac: str,
    source_ref_hmac: str,
    event_type: str,
    support_code: str,
    correlation_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO ella_invitation_audit_receipts (
            invitation_id, uid_ref_hmac, source_ref_hmac, event_type,
            support_code, correlation_id, metadata
        ) VALUES ($1, $2, $3, $4, $5, $6::uuid, $7::jsonb)
        """,
        invitation_id,
        uid_ref_hmac,
        source_ref_hmac,
        event_type,
        support_code,
        correlation_id,
        json.dumps(metadata or {}),
    )


async def _record_failure(
    conn: asyncpg.Connection,
    *,
    invitation_id: Optional[uuid.UUID],
    uid_ref_hmac: str,
    source_ref_hmac: str,
    failure_code: str,
    support_code: str,
    correlation_id: str,
    now: datetime,
    config: InvitationConfig,
) -> None:
    await conn.execute(
        """
        INSERT INTO ella_invitation_rate_limit_events (
            uid_ref_hmac, source_ref_hmac, failure_code, occurred_at
        ) VALUES ($1, $2, $3, $4)
        """,
        uid_ref_hmac,
        source_ref_hmac,
        failure_code,
        now,
    )
    await _record_audit(
        conn,
        invitation_id=invitation_id,
        uid_ref_hmac=uid_ref_hmac,
        source_ref_hmac=source_ref_hmac,
        event_type=failure_code,
        support_code=support_code,
        correlation_id=correlation_id,
    )
    global_count = int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM ella_invitation_rate_limit_events
            WHERE occurred_at >= $1
            """,
            now - config.anomaly_window,
        )
        or 0
    )
    if global_count >= config.anomaly_limit:
        await conn.execute(
            """
            INSERT INTO ella_invitation_security_alerts (
                alert_type, window_started_at, failure_count
            ) VALUES ('redemption_anomaly', $1, $2)
            ON CONFLICT DO NOTHING
            """,
            now - config.anomaly_window,
            global_count,
        )


def _failure(
    code: str,
    *,
    support_code: str,
    correlation_id: str,
    retry_after_s: Optional[int] = None,
) -> InviteRedemptionFailure:
    statuses = {
        "invalid": 400,
        "capacity": 409,
        "expired": 410,
        "rate_limited": 429,
        "service_unavailable": 503,
    }
    return InviteRedemptionFailure(
        code,
        status_code=statuses[code],
        support_code=support_code,
        correlation_id=correlation_id,
        retry_after_s=retry_after_s,
    )


async def redeem_invitation(
    *,
    uid: str,
    code: str,
    source_address: str,
    app_build: str = "",
    config: Optional[InvitationConfig] = None,
) -> dict[str, Any]:
    settings = config or InvitationConfig.from_env()
    support_code = _support_code()
    correlation_id = _correlation_id()
    if not settings.redemption_enabled:
        raise _failure(
            "invalid",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    normalized_code = normalize_invite_code(code)
    uid_ref_hmac = _hmac_ref(settings, "uid-v1", uid)
    source_ref_hmac = _hmac_ref(settings, "source-v1", source_address)
    invitation_code_hmac = code_hmac(settings, normalized_code) if normalized_code else None
    now = datetime.now(timezone.utc)
    result: dict[str, Any] | InviteRedemptionFailure

    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await voice_canary.lock_runtime_authority_on_connection(
                conn,
                uid=uid,
            )
            await _lock_rate_keys(conn, uid_ref_hmac, source_ref_hmac)
            await conn.execute(
                "DELETE FROM ella_invitation_rate_limit_events WHERE occurred_at < $1",
                now - timedelta(hours=25),
            )
            retry_after_s = await _rate_limit_retry_after(
                conn,
                uid_ref_hmac=uid_ref_hmac,
                source_ref_hmac=source_ref_hmac,
                now=now,
                config=settings,
            )
            if retry_after_s is not None:
                await _record_audit(
                    conn,
                    invitation_id=None,
                    uid_ref_hmac=uid_ref_hmac,
                    source_ref_hmac=source_ref_hmac,
                    event_type="rate_limited",
                    support_code=support_code,
                    correlation_id=correlation_id,
                    metadata={"retry_after_s": retry_after_s},
                )
                result = _failure(
                    "rate_limited",
                    support_code=support_code,
                    correlation_id=correlation_id,
                    retry_after_s=retry_after_s,
                )
            elif not invitation_code_hmac:
                await _record_failure(
                    conn,
                    invitation_id=None,
                    uid_ref_hmac=uid_ref_hmac,
                    source_ref_hmac=source_ref_hmac,
                    failure_code="invalid",
                    support_code=support_code,
                    correlation_id=correlation_id,
                    now=now,
                    config=settings,
                )
                result = _failure(
                    "invalid",
                    support_code=support_code,
                    correlation_id=correlation_id,
                )
            else:
                invitation = await conn.fetchrow(
                    "SELECT * FROM ella_invitations WHERE code_hmac = $1 FOR UPDATE",
                    invitation_code_hmac,
                )
                if not invitation or not hmac.compare_digest(
                    str(invitation["code_hmac"]).strip(),
                    invitation_code_hmac,
                ):
                    await _record_failure(
                        conn,
                        invitation_id=None,
                        uid_ref_hmac=uid_ref_hmac,
                        source_ref_hmac=source_ref_hmac,
                        failure_code="invalid",
                        support_code=support_code,
                        correlation_id=correlation_id,
                        now=now,
                        config=settings,
                    )
                    result = _failure(
                        "invalid",
                        support_code=support_code,
                        correlation_id=correlation_id,
                    )
                else:
                    result = await _redeem_locked_invitation(
                        conn,
                        invitation=dict(invitation),
                        uid=uid,
                        uid_ref_hmac=uid_ref_hmac,
                        source_ref_hmac=source_ref_hmac,
                        support_code=support_code,
                        correlation_id=correlation_id,
                        app_build=app_build,
                        now=now,
                        config=settings,
                    )

    if isinstance(result, InviteRedemptionFailure):
        raise result
    return result


async def _redeem_locked_invitation(
    conn: asyncpg.Connection,
    *,
    invitation: dict[str, Any],
    uid: str,
    uid_ref_hmac: str,
    source_ref_hmac: str,
    support_code: str,
    correlation_id: str,
    app_build: str,
    now: datetime,
    config: InvitationConfig,
) -> dict[str, Any] | InviteRedemptionFailure:
    invitation_id = invitation["id"]
    existing = await conn.fetchrow(
        """
        SELECT entitlement_revision
        FROM ella_invitation_redemptions
        WHERE invitation_id = $1 AND uid_ref_hmac = $2
        """,
        invitation_id,
        uid_ref_hmac,
    )
    if existing:
        entitlement = await conn.fetchrow(
            """
            SELECT uid FROM voice_entitlements
            WHERE uid = $1 AND invitation_id = $2
            """,
            uid,
            invitation_id,
        )
        if not entitlement:
            await _record_audit(
                conn,
                invitation_id=invitation_id,
                uid_ref_hmac=uid_ref_hmac,
                source_ref_hmac=source_ref_hmac,
                event_type="policy_invalid",
                support_code=support_code,
                correlation_id=correlation_id,
            )
            return _failure(
                "service_unavailable",
                support_code=support_code,
                correlation_id=correlation_id,
            )
        await _record_audit(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            event_type="idempotent_retry",
            support_code=support_code,
            correlation_id=correlation_id,
        )
        contract = await voice_canary.get_entitlement_contract_for_connection(
            conn,
            uid,
            now=now,
            expire_stale_sessions=False,
        )
        return {
            **contract,
            "support_code": support_code,
            "correlation_id": correlation_id,
        }

    kind = str(invitation["kind"])
    kind_enabled = config.ordinary_enabled if kind == "ordinary" else config.app_review_enabled
    if not kind_enabled:
        await _record_audit(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            event_type="redemption_disabled",
            support_code=support_code,
            correlation_id=correlation_id,
        )
        return _failure(
            "invalid",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    if (
        invitation["state"] != "sent"
        or invitation["delivery_state"] != "sent"
        or int(invitation["redemption_count"]) >= int(invitation["max_redemptions"])
    ):
        await _record_failure(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            failure_code="invalid",
            support_code=support_code,
            correlation_id=correlation_id,
            now=now,
            config=config,
        )
        return _failure(
            "invalid",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    expires_at = invitation.get("expires_at")
    if expires_at and expires_at <= now:
        await _record_failure(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            failure_code="expired",
            support_code=support_code,
            correlation_id=correlation_id,
            now=now,
            config=config,
        )
        return _failure(
            "expired",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    reservation = await conn.fetchrow(
        """
        SELECT *
        FROM ella_invitation_capacity_reservations
        WHERE id = $1
        FOR UPDATE
        """,
        invitation["capacity_reservation_id"],
    )
    capacity_available = bool(
        reservation
        and reservation["state"] == "reserved"
        and (not reservation["expires_at"] or reservation["expires_at"] > now)
        and (kind == "app_review" or int(reservation["consumed_slots"]) < int(reservation["reserved_slots"]))
    )
    if not capacity_available:
        await _record_failure(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            failure_code="capacity",
            support_code=support_code,
            correlation_id=correlation_id,
            now=now,
            config=config,
        )
        return _failure(
            "capacity",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    try:
        policy = normalize_entitlement_policy(invitation["entitlement_policy"])
    except InviteConfigurationError:
        await _record_audit(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            event_type="policy_invalid",
            support_code=support_code,
            correlation_id=correlation_id,
        )
        return _failure(
            "service_unavailable",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    existing_entitlement = await conn.fetchrow(
        "SELECT uid FROM voice_entitlements WHERE uid = $1 FOR UPDATE",
        uid,
    )
    if existing_entitlement:
        await _record_failure(
            conn,
            invitation_id=invitation_id,
            uid_ref_hmac=uid_ref_hmac,
            source_ref_hmac=source_ref_hmac,
            failure_code="invalid",
            support_code=support_code,
            correlation_id=correlation_id,
            now=now,
            config=config,
        )
        return _failure(
            "invalid",
            support_code=support_code,
            correlation_id=correlation_id,
        )

    entitlement_revision = int(
        await conn.fetchval(
            """
            INSERT INTO voice_entitlements (
                uid, status, plan, daily_limit_s, monthly_limit_s,
                soft_limit_ratio, hard_limit_ratio, max_session_s, max_concurrent,
                max_audio_bytes_per_session, max_audio_bytes_per_minute,
                provider_allowlist, model_allowlist, mode_allowlist, fallback_policy,
                invitation_id, entitlement_policy_revision, cohort,
                exclude_from_product_analytics
            ) VALUES (
                $1, 'invited', $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14::jsonb, $15, $16, $17, $18
            )
            RETURNING revision
            """,
            uid,
            policy["plan"],
            policy["daily_limit_s"],
            policy["monthly_limit_s"],
            policy["soft_limit_ratio"],
            policy["hard_limit_ratio"],
            policy["max_session_s"],
            policy["max_concurrent"],
            policy["max_audio_bytes_per_session"],
            policy["max_audio_bytes_per_minute"],
            policy["provider_allowlist"],
            policy["model_allowlist"],
            policy["mode_allowlist"],
            json.dumps(policy["fallback_policy"]),
            invitation_id,
            invitation["entitlement_policy_revision"],
            invitation["cohort"],
            bool(invitation["exclude_from_product_analytics"]),
        )
    )
    if kind == "ordinary":
        await conn.execute(
            """
            UPDATE ella_invitation_capacity_reservations
            SET consumed_slots = consumed_slots + 1,
                state = CASE
                    WHEN consumed_slots + 1 >= reserved_slots THEN 'consumed'
                    ELSE state
                END,
                consumed_at = CASE
                    WHEN consumed_slots + 1 >= reserved_slots THEN $2
                    ELSE consumed_at
                END,
                version = version + 1,
                updated_at = $2
            WHERE id = $1
            """,
            reservation["id"],
            now,
        )
    await conn.execute(
        """
        UPDATE ella_invitations
        SET redemption_count = redemption_count + 1,
            state = CASE WHEN kind = 'ordinary' THEN 'redeemed' ELSE state END,
            version = version + 1,
            updated_at = $2
        WHERE id = $1
        """,
        invitation_id,
        now,
    )
    await conn.execute(
        """
        INSERT INTO ella_invitation_redemptions (
            invitation_id, uid_ref_hmac, entitlement_revision,
            support_code, correlation_id, app_build
        ) VALUES ($1, $2, $3, $4, $5::uuid, $6)
        """,
        invitation_id,
        uid_ref_hmac,
        entitlement_revision,
        support_code,
        correlation_id,
        app_build if SAFE_APP_BUILD_RE.fullmatch(app_build) else None,
    )
    await _record_audit(
        conn,
        invitation_id=invitation_id,
        uid_ref_hmac=uid_ref_hmac,
        source_ref_hmac=source_ref_hmac,
        event_type="redeemed",
        support_code=support_code,
        correlation_id=correlation_id,
        metadata={
            "kind": kind,
            "cohort": invitation["cohort"],
            "entitlement_revision": entitlement_revision,
        },
    )
    contract = await voice_canary.get_entitlement_contract_for_connection(
        conn,
        uid,
        now=now,
        expire_stale_sessions=False,
    )
    return {
        **contract,
        "support_code": support_code,
        "correlation_id": correlation_id,
    }
