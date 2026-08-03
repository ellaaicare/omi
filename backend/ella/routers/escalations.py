"""Ella escalation policy API.

This endpoint evaluates classified safety/care events and returns a delivery
plan. It intentionally does not send messages itself; n8n/OpenClaw execute the
returned plan and report delivery status to trace tables.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from firebase_admin.auth import InvalidIdTokenError
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field
from utils.other import endpoints as auth_endpoints

from ella.services.escalation_policy import (
    CaregiverPolicyContext,
    EscalationEvent,
    UserPolicyContext,
    build_policy_markdown_view,
    build_plain_language_policy_view,
    evaluate_escalation_policy,
)

router = APIRouter(prefix="/v1/ella/escalations", tags=["Ella Escalations"])

ESCALATION_WEBHOOK_KEY = os.getenv(
    "ELLA_ESCALATION_WEBHOOK_KEY",
    os.getenv("GUARDIAN_WEBHOOK_KEY", ""),
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
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database="ella_ai",
            min_size=1,
            max_size=5,
        )
    return _pool


def _verify_key(x_guardian_key: Optional[str], x_escalation_key: Optional[str], key: Optional[str]) -> None:
    provided = x_escalation_key or x_guardian_key or key
    if provided != ESCALATION_WEBHOOK_KEY:
        raise HTTPException(status_code=403, detail="Invalid escalation key")


def _resolve_policy_view_uid(
    uid: Optional[str],
    authorization: Optional[str],
    x_guardian_key: Optional[str],
    x_escalation_key: Optional[str],
    key: Optional[str],
) -> str:
    """Resolve the policy-view UID for either app auth or internal callers."""
    provided_key = x_escalation_key or x_guardian_key or key
    if provided_key == ESCALATION_WEBHOOK_KEY:
        if not uid:
            raise HTTPException(status_code=400, detail="uid is required for internal policy lookup")
        return uid

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header not found")

    parts = str(authorization).split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    try:
        authenticated_uid = auth_endpoints.verify_token(parts[1])
    except InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid authorization token")
    except Exception as e:
        print(f"Error verifying Firebase ID token: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    if uid and uid != authenticated_uid:
        raise HTTPException(status_code=403, detail="Authenticated user cannot read another user's escalation policy")
    return authenticated_uid


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
        WHERE LOWER(omi_uid) = LOWER($1)
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
):
    """Evaluate a classified event and return a deterministic delivery plan."""
    _verify_key(x_guardian_key, x_escalation_key, key)
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
):
    """Return the read-only effective policy view for user/caregiver UI."""
    resolved_uid = _resolve_policy_view_uid(uid, authorization, x_guardian_key, x_escalation_key, key)
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
):
    """Return the effective policy as generated Markdown for agents/workspaces."""
    resolved_uid = _resolve_policy_view_uid(uid, authorization, x_guardian_key, x_escalation_key, key)
    user, caregivers = await _load_context(resolved_uid)
    markdown = build_policy_markdown_view(user, caregivers)
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")
