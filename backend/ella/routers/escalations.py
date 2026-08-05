"""Ella escalation policy API.

This endpoint evaluates classified safety/care events and returns a delivery
plan. It intentionally does not send messages itself; n8n/OpenClaw execute the
returned plan and report delivery status to trace tables.
"""

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from database.honcho_attestation import authority_credential
from ella.services.escalation_policy import (
    CaregiverPolicyContext,
    EscalationEvent,
    UserPolicyContext,
    build_policy_markdown_view,
    build_plain_language_policy_view,
    evaluate_escalation_policy,
)
from utils.ella.exact_firebase_auth import (
    ELLA_SUBJECT_UID_HEADER,
    EllaRequestAuthority,
    get_exact_firebase_uid,
    get_exact_service_authority,
)

router = APIRouter(prefix="/v1/ella/escalations", tags=["Ella Escalations"])

ESCALATION_WEBHOOK_KEY = (
    authority_credential("ELLA_ESCALATION_WEBHOOK_KEY", strip=False)
    if "ELLA_ESCALATION_WEBHOOK_KEY" in os.environ
    else authority_credential("GUARDIAN_WEBHOOK_KEY", strip=False)
)

_pool: Optional[asyncpg.Pool] = None


class EscalationEvaluateRequest(BaseModel):
    uid: str
    trace_id: Optional[str] = None
    source: str = "unknown"
    event_type: str = "unknown"
    severity: str = "low"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    requested_channels: list[str] = Field(default_factory=list)


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False),
            database="ella_ai",
            min_size=1,
            max_size=5,
        )
    return _pool


def _has_valid_service_key(x_guardian_key: Optional[str], x_escalation_key: Optional[str], key: Optional[str]) -> bool:
    provided = x_escalation_key or x_guardian_key or key
    configured = ESCALATION_WEBHOOK_KEY
    return bool(
        configured
        and configured.strip()
        and provided
        and provided.strip()
        and secrets.compare_digest(provided, configured)
    )


def _verify_key(x_guardian_key: Optional[str], x_escalation_key: Optional[str], key: Optional[str]) -> None:
    if not _has_valid_service_key(x_guardian_key, x_escalation_key, key):
        raise HTTPException(status_code=403, detail="Invalid escalation key")


def _service_authority(
    x_guardian_key: Optional[str],
    x_escalation_key: Optional[str],
    key: Optional[str],
    subject_uid: Optional[str],
) -> EllaRequestAuthority:
    return get_exact_service_authority(
        provided_service_key=x_escalation_key or x_guardian_key or key,
        configured_service_key=ESCALATION_WEBHOOK_KEY,
        service_subject_uid=subject_uid,
        service="ella_escalation",
    )


def _resolve_policy_view_uid(
    uid: Optional[str],
    authorization: Optional[str],
    x_guardian_key: Optional[str],
    x_escalation_key: Optional[str],
    key: Optional[str],
    subject_uid: Optional[str],
    x_app_version: Optional[str] = None,
    x_ella_app_build: Optional[str] = None,
    x_ella_client_version: Optional[str] = None,
) -> str:
    """Resolve the policy-view UID for either app auth or internal callers."""
    if _has_valid_service_key(x_guardian_key, x_escalation_key, key):
        return _service_authority(
            x_guardian_key,
            x_escalation_key,
            key,
            subject_uid,
        ).require_uid(uid, feature="Escalation policy")
    authenticated_uid = get_exact_firebase_uid(
        authorization,
        x_app_version,
        x_ella_app_build,
        x_ella_client_version,
    )
    return EllaRequestAuthority(firebase_uid=authenticated_uid).require_uid(uid, feature="Escalation policy")


def _dict_value(data: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _identity_phone(identities: Any, canonical_phone: Optional[str]) -> Optional[str]:
    if isinstance(identities, dict) and identities.get("phone"):
        return identities.get("phone")
    return canonical_phone


async def _load_context(uid: str) -> tuple[UserPolicyContext, list[CaregiverPolicyContext]]:
    pool = await _get_pool()
    user_row = await pool.fetchrow(
        """
        SELECT id, omi_uid, guardian_mode, email, phone_number, identities
        FROM users
        WHERE omi_uid = $1
        """,
        uid,
    )
    if not user_row:
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "uid": uid})

    identities = user_row["identities"] or {}
    if isinstance(identities, str):
        identities = json.loads(identities)
    user = UserPolicyContext(
        uid=user_row["omi_uid"],
        user_id=str(user_row["id"]),
        guardian_mode=user_row["guardian_mode"],
        user_email=user_row["email"],
        user_phone=_identity_phone(identities, user_row["phone_number"]),
        guardian_audio_enabled=identities.get("guardian_audio_enabled") if isinstance(identities, dict) else None,
        channel_preferences=_dict_value(
            identities,
            "escalation_channel_preferences",
            "channel_preferences",
            "notification_channel_preferences",
        ),
        caregiver_alert_preferences=_dict_value(
            identities,
            "caregiver_alert_preferences",
            "caregiver_alerts",
        ),
        recap_preferences=_dict_value(
            identities,
            "recap_preferences",
            "daily_recap_preferences",
        ),
        provider_health=_dict_value(identities, "provider_health", "channel_provider_health"),
        quiet_hours_active=bool(identities.get("quiet_hours_active", False)) if isinstance(identities, dict) else False,
    )

    caregiver_rows = await pool.fetch(
        """
        SELECT id, status::text AS status, is_emergency_contact, name, relationship, email, phone, permissions
        FROM caregivers
        WHERE user_id = $1
        ORDER BY is_emergency_contact DESC, created_at ASC
        """,
        user_row["id"],
    )
    caregivers: list[CaregiverPolicyContext] = []
    for row in caregiver_rows:
        permissions = row["permissions"] or {}
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        caregivers.append(
            CaregiverPolicyContext(
                caregiver_id=str(row["id"]),
                status=row["status"],
                is_emergency_contact=bool(row["is_emergency_contact"]),
                name=row["name"],
                relationship=row["relationship"],
                email=row["email"],
                phone=row["phone"],
                permissions=permissions if isinstance(permissions, dict) else {},
            )
        )
    return user, caregivers


async def _log_decision(uid: str, decision: dict[str, Any]) -> None:
    try:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO guardian_pipeline_events (trace_id, uid, stage, status, metadata)
            VALUES ($1, $2, 'escalation_policy_decided', 'success', $3::jsonb)
            """,
            decision["trace_id"],
            uid,
            json.dumps(decision),
        )
    except Exception:
        # Trace writes should not block the policy response.
        return


@router.post("/evaluate")
async def evaluate_escalation(
    req: EscalationEvaluateRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    x_escalation_key: Optional[str] = Header(None, alias="X-Escalation-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
    subject_uid: Optional[str] = Header(None, alias=ELLA_SUBJECT_UID_HEADER),
):
    """Evaluate a classified event and return a deterministic delivery plan."""
    authority = _service_authority(x_guardian_key, x_escalation_key, key, subject_uid)
    req.uid = authority.require_uid(req.uid, feature="Escalation evaluation")
    user, caregivers = await _load_context(req.uid)
    event = EscalationEvent(
        uid=req.uid,
        trace_id=req.trace_id or req.uid,
        source=req.source,
        event_type=req.event_type,
        severity=req.severity,
        confidence=req.confidence,
        ambiguity=req.ambiguity,
        summary=req.summary,
        evidence=req.evidence,
        requested_channels=tuple(req.requested_channels),
    )
    decision = evaluate_escalation_policy(event, user, caregivers).to_dict()
    await _log_decision(req.uid, decision)
    return {"ok": True, **decision}


@router.get("/policy")
async def get_escalation_policy(
    uid: Optional[str] = None,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    x_escalation_key: Optional[str] = Header(None, alias="X-Escalation-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
    subject_uid: Optional[str] = Header(None, alias=ELLA_SUBJECT_UID_HEADER),
    x_app_version: Optional[str] = Header(default=None, alias="X-App-Version"),
    x_ella_app_build: Optional[str] = Header(default=None, alias="X-Ella-App-Build"),
    x_ella_client_version: Optional[str] = Header(default=None, alias="X-Ella-Client-Version"),
):
    """Return the read-only effective policy view for user/caregiver UI."""
    resolved_uid = _resolve_policy_view_uid(
        uid,
        authorization,
        x_guardian_key,
        x_escalation_key,
        key,
        subject_uid,
        x_app_version,
        x_ella_app_build,
        x_ella_client_version,
    )
    user, caregivers = await _load_context(resolved_uid)
    policy = build_plain_language_policy_view(user, caregivers)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **policy,
    }


@router.get("/policy.md")
async def get_escalation_policy_markdown(
    uid: Optional[str] = None,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    x_escalation_key: Optional[str] = Header(None, alias="X-Escalation-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
    subject_uid: Optional[str] = Header(None, alias=ELLA_SUBJECT_UID_HEADER),
    x_app_version: Optional[str] = Header(default=None, alias="X-App-Version"),
    x_ella_app_build: Optional[str] = Header(default=None, alias="X-Ella-App-Build"),
    x_ella_client_version: Optional[str] = Header(default=None, alias="X-Ella-Client-Version"),
):
    """Return the effective policy as generated Markdown for agents/workspaces."""
    resolved_uid = _resolve_policy_view_uid(
        uid,
        authorization,
        x_guardian_key,
        x_escalation_key,
        key,
        subject_uid,
        x_app_version,
        x_ella_app_build,
        x_ella_client_version,
    )
    user, caregivers = await _load_context(resolved_uid)
    markdown = build_policy_markdown_view(user, caregivers)
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")
