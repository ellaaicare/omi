"""
Guardian Mode Router - Audio delivery queue for iOS Guardian Mode.

Endpoints:
- POST /v1/ella/guardian/deliver              - evaluate policy and dispatch delivery plan
- GET  /v1/ella/guardian/next-audio?uid={uid} - iOS polls, pops from queue
- POST /v1/ella/guardian/enqueue               - n8n enqueues after TTS
- POST /v1/ella/guardian/upload                - n8n uploads MP3 binary
- GET  /v1/ella/guardian/queue?uid={uid}       - debug/dashboard view

Queue: ella-postgres guardian_queue table.
Audio files: /var/www/ella-ai-care.com/audio/{uid}/*.mp3
"""

import base64
import binascii
import json
import os
import time
import uuid
from email.mime.text import MIMEText
import smtplib
from typing import Any, Optional

import asyncpg
import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from ella.routers.resolve import resolve_user_routing
from database import app_settings as app_settings_db
from ella.services.app_settings import TTS_PROVIDERS, build_effective_voice_settings
from ella.services.escalation_policy import (
    CaregiverPolicyContext,
    EscalationEvent,
    MODE_EMERGENCY_ONLY,
    MODE_OFF,
    UserPolicyContext,
    _normalize_mode,
    evaluate_escalation_policy,
)
from utils.ella.canonical_context import (
    DEFAULT_CONTEXT_CHANNELS,
    canonical_events_to_chat_turns,
    fetch_canonical_timeline,
)

router = APIRouter(prefix="/v1/ella/guardian", tags=["Guardian Mode"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUDIO_BASE_DIR = "/var/www/ella-ai-care.com/audio"
AUDIO_PUBLIC_URL = "https://ella-ai-care.com/audio"
GUARDIAN_WEBHOOK_KEY = os.getenv("GUARDIAN_WEBHOOK_KEY", "4f13699d8462adf71e35d2098e6a791f")
SMTP_FROM = os.getenv("ELLA_SMTP_FROM", "guardian@ella-ai-care.com")
N8N_GUARDIAN_DELIVER_WEBHOOK = os.getenv(
    "N8N_GUARDIAN_DELIVER_WEBHOOK",
    "https://n8n.ella-ai-care.com/webhook/guardian-deliver",
)
ELLA_API_BASE = os.getenv("ELLA_API_BASE", "https://api.ella-ai-care.com")
ELLA_INTERNAL_VOICE_TTS_URL = os.getenv("ELLA_INTERNAL_VOICE_TTS_URL", "http://127.0.0.1:8000/v1/voice/tts")

# Consolidate queue when this many non-debug items are pending
CONSOLIDATION_THRESHOLD = int(os.getenv("CONSOLIDATION_THRESHOLD", "3"))

# Provision API for chat history lookups
_PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
_PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")
_GUARDIAN_CONTEXT_CHANNELS = [
    channel.strip()
    for channel in os.getenv("ELLA_GUARDIAN_CANONICAL_CHANNELS", ",".join(DEFAULT_CONTEXT_CHANNELS)).split(",")
    if channel.strip()
]

# LLM settings for consolidator — prefers OpenRouter, falls back to XAI direct
_OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
_LLM_API_KEY = _OPENROUTER_KEY or os.getenv("XAI_API_KEY", "")
_LLM_API_BASE = "https://openrouter.ai/api/v1" if _OPENROUTER_KEY else "https://api.x.ai/v1"
_LLM_MODEL = "x-ai/grok-4.1-fast" if _OPENROUTER_KEY else "grok-4-1-fast-non-reasoning"

# Database connection pool (lazy-initialized)
_pool: Optional[asyncpg.Pool] = None

# ---------------------------------------------------------------------------
# In-memory playback event store (echo risk tracking)
# ---------------------------------------------------------------------------

# uid -> last playback event (resets on restart — used only for echo risk)
_playback_events: dict[str, dict] = {}

# Echo risk by iOS AVAudioSession portType rawValue
_ECHO_RISK = {
    "Speaker": "high",  # builtInSpeaker
    "Receiver": "none",  # builtInReceiver
    "Headphones": "none",  # headphones
    "BluetoothHFP": "none",  # BT headset (hands-free)
    "BluetoothA2DP": "high",  # BT speaker/headphones
    "BluetoothLE": "medium",  # BT LE audio
    "AirPlay": "very_high",  # AirPlay / Apple TV
    "HDMI": "very_high",
    "CarAudio": "high",
    "USBAudio": "low",
}


async def _get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database="ella_ai",
            min_size=2,
            max_size=10,
        )
    return _pool


def _verify_key(
    x_guardian_key: Optional[str] = None,
    key: Optional[str] = None,
) -> None:
    """Verify webhook authentication key."""
    provided = x_guardian_key or key
    if provided != GUARDIAN_WEBHOOK_KEY:
        raise HTTPException(status_code=403, detail="Invalid guardian key")


def _verify_optional_trace_key(x_guardian_key: Optional[str] = None, key: Optional[str] = None) -> None:
    """Allow legacy scanner trace posts without a key, but reject bad keys when present."""
    provided = x_guardian_key or key
    if provided and provided != GUARDIAN_WEBHOOK_KEY:
        raise HTTPException(status_code=403, detail="Invalid guardian key")


def _dict_value(data: object, *keys: str) -> dict:
    if not isinstance(data, dict):
        return {}
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _identity_phone(identities: object, canonical_phone: Optional[str]) -> Optional[str]:
    if isinstance(identities, dict) and identities.get("phone"):
        return identities.get("phone")
    return canonical_phone


def _delivery_key(step: dict) -> tuple[str, str]:
    return str(step.get("channel") or "unknown"), str(step.get("target") or "unknown")


def _delivery_status_blocks_dispatch(status: Optional[str]) -> bool:
    return str(status or "").lower() in {"pending", "sending", "sent", "success", "delivered"}


def _caregiver_payload(caregiver: CaregiverPolicyContext) -> dict:
    return {
        "id": caregiver.caregiver_id,
        "caregiver_id": caregiver.caregiver_id,
        "name": caregiver.name,
        "email": caregiver.email,
        "phone": caregiver.phone,
        "is_emergency_contact": caregiver.is_emergency_contact,
        "relationship": caregiver.relationship,
    }


def _recipient_for_step(
    step: dict,
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> dict:
    if step.get("target") == "caregiver":
        caregiver = next((item for item in caregivers if item.caregiver_id == step.get("caregiver_id")), None)
        if caregiver is None:
            caregiver = next(
                (item for item in caregivers if item.is_emergency_contact), caregivers[0] if caregivers else None
            )
        if caregiver:
            return {
                "recipient_id": caregiver.caregiver_id,
                "recipient_name": caregiver.name,
                "recipient_phone": caregiver.phone,
                "recipient_email": caregiver.email,
            }
        return {"recipient_id": None, "recipient_name": None, "recipient_phone": None, "recipient_email": None}

    return {
        "recipient_id": user.user_id,
        "recipient_name": None,
        "recipient_phone": user.user_phone,
        "recipient_email": user.user_email,
    }


async def _load_delivery_context(uid: str) -> tuple[UserPolicyContext, list[CaregiverPolicyContext]]:
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
        caregiver_alert_preferences=_dict_value(identities, "caregiver_alert_preferences", "caregiver_alerts"),
        recap_preferences=_dict_value(identities, "recap_preferences", "daily_recap_preferences"),
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


async def _reserve_delivery_steps(
    trace_id: str,
    uid: str,
    steps: list[dict],
) -> tuple[list[dict], list[dict]]:
    if not steps:
        return [], []

    pool = await _get_pool()
    channels = [channel for channel, _target in [_delivery_key(step) for step in steps]]
    targets = [target for _channel, target in [_delivery_key(step) for step in steps]]
    existing_rows = await pool.fetch(
        """
        SELECT channel, target, status
        FROM guardian_delivery_log
        WHERE trace_id = $1
          AND channel = ANY($2::text[])
          AND target = ANY($3::text[])
        """,
        trace_id,
        channels,
        targets,
    )
    existing_status = {(str(row["channel"]), str(row["target"])): row["status"] for row in existing_rows}

    pending_steps: list[dict] = []
    skipped_steps: list[dict] = []
    for step in steps:
        channel, target = _delivery_key(step)
        status = existing_status.get((channel, target))
        if _delivery_status_blocks_dispatch(status):
            skipped_steps.append({**step, "skip_reason": f"already_{status}"})
            continue

        await pool.execute(
            """
            INSERT INTO guardian_delivery_log (
                trace_id, uid, channel, target, caregiver_id, recipient_phone,
                recipient_email, status, provider_response
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8::jsonb)
            ON CONFLICT (trace_id, channel, target) DO UPDATE SET
                uid = EXCLUDED.uid,
                caregiver_id = EXCLUDED.caregiver_id,
                recipient_phone = EXCLUDED.recipient_phone,
                recipient_email = EXCLUDED.recipient_email,
                status = 'pending',
                error_message = NULL,
                provider_response = EXCLUDED.provider_response,
                updated_at = NOW()
            WHERE guardian_delivery_log.status NOT IN ('pending', 'sending', 'sent', 'success', 'delivered')
            """,
            trace_id,
            uid,
            channel,
            target,
            step.get("caregiver_id"),
            step.get("recipient_phone"),
            step.get("recipient_email"),
            json.dumps({"reserved_by": "omi_backend", "step": step}),
        )
        pending_steps.append(step)

    return pending_steps, skipped_steps


async def _mark_reserved_steps_dispatch_failed(trace_id: str, steps: list[dict], error_message: str) -> None:
    if not steps:
        return
    pool = await _get_pool()
    for step in steps:
        channel, target = _delivery_key(step)
        await pool.execute(
            """
            UPDATE guardian_delivery_log
            SET status = 'dispatch_failed', error_message = $1, updated_at = NOW()
            WHERE trace_id = $2 AND channel = $3 AND target = $4 AND status = 'pending'
            """,
            error_message[:500],
            trace_id,
            channel,
            target,
        )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PlaybackEventRequest(BaseModel):
    """JSON body for /playback-event endpoint."""

    uid: str
    queue_item_id: Optional[str] = None
    trace_id: Optional[str] = None
    event_type: str = "started"  # started, completed, failed
    port_type: str  # AVAudioSession portType rawValue (e.g. "Speaker", "BluetoothA2DP")
    port_name: str = ""  # human-readable device name (e.g. "AirPods Pro")
    device_uid: str = ""  # unique device ID from AVAudioSessionPortDescription
    duration_ms: int = 0  # estimated audio duration in ms
    metadata: Optional[dict] = None


class PlaybackDebugEventRequest(BaseModel):
    """JSON body for /playback-debug endpoint.

    This is a wider debug/event sink for iOS Guardian playback diagnostics.
    It is intended for TestFlight/debug builds where we want the full local
    playback trace server-side without requiring manual copy/paste.
    """

    uid: str
    event_name: str
    trace_id: Optional[str] = None
    queue_item_id: Optional[str] = None
    stage: Optional[str] = None
    status: str = "success"
    port_type: str = ""
    port_name: str = ""
    device_uid: str = ""
    latency_ms: Optional[int] = None
    metadata: Optional[dict] = None


class DebugTriggerRequest(BaseModel):
    """Debug-only direct queue injection helper for iOS/device validation."""

    uid: str
    url: Optional[str] = None
    queue_item_id: Optional[str] = None
    trace_id: Optional[str] = None
    priority: str = "debug"
    message: str = "Guardian debug trigger"
    trigger_type: str = "manual_direct_test"
    metadata: Optional[dict] = None


class DeliverRequest(BaseModel):
    """JSON body for /deliver."""

    uid: str
    trace_id: Optional[str] = None
    source: str = "unknown"
    event_type: str = "unknown"
    severity: str = "low"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    evidence: dict = Field(default_factory=dict)
    requested_channels: list[str] = Field(default_factory=list)


class EmailSendRequest(BaseModel):
    """JSON body for /email/send."""

    to: str
    subject: str
    body: str
    trace_id: Optional[str] = None
    uid: Optional[str] = None
    channel: str = "email"
    target: str = "unknown"
    priority: str = "normal"


class TraceLogRequest(BaseModel):
    """JSON body for /trace/log."""

    trace_id: str
    uid: Optional[str] = None
    stage: str
    status: str = "success"
    latency_ms: Optional[int] = None
    error_detail: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class EnqueueRequest(BaseModel):
    """JSON body for enqueue endpoint."""

    uid: Optional[str] = None
    userID: Optional[str] = None  # alias accepted from n8n
    url: str
    id: Optional[str] = None
    priority: str = "normal"
    message: Optional[str] = None
    trigger: Optional[str] = None
    metadata: Optional[dict] = None


class UploadJsonRequest(BaseModel):
    """JSON body for trusted n8n audio uploads."""

    uid: str
    audio_base64: str
    filename: Optional[str] = None


class SynthesizeRequest(BaseModel):
    """JSON body for provider-aware Guardian synthesis."""

    uid: str
    text: str
    voice_id: Optional[str] = None
    provider: Optional[str] = None
    trace_id: Optional[str] = None


def _trace_id_from_metadata(metadata: Optional[dict], fallback: str) -> str:
    """Resolve the shared guardian pipeline trace id from queue metadata."""
    if isinstance(metadata, dict):
        for key in ("trace_id", "traceId", "conversation_id", "conversationId"):
            value = metadata.get(key)
            if value:
                return str(value)
    return fallback


def _coerce_metadata_dict(metadata: Optional[dict]) -> dict[str, Any]:
    """Return queue metadata as a plain dict."""
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _queue_priority_rank(priority: Optional[str]) -> int:
    return {
        "urgent": 0,
        "normal": 1,
        "scheduled": 2,
        "debug": 3,
    }.get(str(priority or "").lower(), 4)


def _queue_trace_id(row: dict[str, Any]) -> str:
    return _trace_id_from_metadata(_coerce_metadata_dict(row.get("metadata")), str(row.get("id") or ""))


def _is_wake_word_row(row: dict[str, Any]) -> bool:
    metadata = _coerce_metadata_dict(row.get("metadata"))
    trigger = str(row.get("trigger_type") or metadata.get("trigger_type") or metadata.get("category") or "").lower()
    return trigger == "wake_word"


def _select_same_trace_supersede_rows(pending_rows: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Pick one primary same-trace row to keep and return superseded siblings."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in pending_rows:
        trace_id = _queue_trace_id(row)
        if not trace_id:
            continue
        groups.setdefault(trace_id, []).append(row)

    candidate_groups = [rows for rows in groups.values() if len(rows) >= 2]
    if not candidate_groups:
        return None, []

    def _group_sort_key(rows: list[dict[str, Any]]) -> tuple[int, float]:
        best_rank = min(_queue_priority_rank(row.get("priority")) for row in rows)
        newest_ts = max(getattr(row.get("created_at"), "timestamp", lambda: 0.0)() for row in rows)
        return best_rank, -newest_ts

    group = sorted(candidate_groups, key=_group_sort_key)[0]
    ordered = sorted(
        group,
        key=lambda row: (
            _queue_priority_rank(row.get("priority")),
            1 if _is_wake_word_row(row) else 0,
            -getattr(row.get("created_at"), "timestamp", lambda: 0.0)(),
        ),
    )
    primary = ordered[0]
    superseded = [row for row in group if row.get("id") != primary.get("id")]
    return primary, superseded


def _enqueue_allows_guardian_audio(mode: str, req: "EnqueueRequest") -> tuple[bool, Optional[str]]:
    """Secondary queue-boundary gate for guardian audio."""
    normalized_mode = _normalize_mode(mode)
    if normalized_mode == MODE_OFF:
        return False, "guardian_mode_off"
    if normalized_mode != MODE_EMERGENCY_ONLY:
        return True, None

    metadata = _coerce_metadata_dict(req.metadata)
    trigger = str(
        req.trigger
        or metadata.get("trigger_type")
        or metadata.get("event_type")
        or metadata.get("category")
        or ""
    ).lower()
    severity = str(metadata.get("severity") or metadata.get("urgency") or req.priority or "").lower()
    message = str(req.message or metadata.get("message") or "").lower()
    emergency_keywords = (
        "fall",
        "fell",
        "help me",
        "cant breathe",
        "can't breathe",
        "call 911",
        "emergency",
        "stroke",
        "chest pain",
        "not breathing",
        "intruder",
        "fire",
        "gas leak",
    )

    if trigger == "wake_word" or trigger.startswith("wake_word_"):
        return True, None
    if trigger == "safety" and (severity in {"critical", "urgent", "high"} or req.priority == "urgent"):
        return True, None
    if any(keyword in message for keyword in emergency_keywords):
        return True, None
    return False, "guardian_mode_emergency_only"


async def _log_pipeline_event(
    trace_id: str,
    uid: str,
    stage: str,
    status: str = "success",
    latency_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort durable trace write for scanner/audio lifecycle events."""
    if not trace_id or not stage:
        return
    try:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO guardian_pipeline_events (trace_id, uid, stage, status, latency_ms, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            trace_id,
            uid or "",
            stage,
            status,
            latency_ms,
            json.dumps(metadata or {}),
        )
    except Exception as e:
        print(
            f"[FLOW:GUARDIAN-TRACE-LOG] trace={trace_id} stage={stage} failed={e}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Consolidation helpers
# ---------------------------------------------------------------------------


async def _get_recent_chat_turns(uid: str, limit: int = 5) -> list[dict]:
    """Fetch recent turns for Guardian consolidation from canonical timeline.

    Provision/OpenClaw history is retained only as a logged migration fallback.
    Returns list of {role, content} dicts, newest first. Returns [] on any error.
    """
    try:
        events = await fetch_canonical_timeline(
            uid,
            limit=max(limit, 10),
            channels=_GUARDIAN_CONTEXT_CHANNELS,
        )
        if events:
            turns = canonical_events_to_chat_turns(events, limit=limit)
            print(
                f"[FLOW:GUARDIAN-CONTEXT] uid={uid} source=canonical_timeline events={len(events)} turns={len(turns)}",
                flush=True,
            )
            return turns
        print(
            f"[FLOW:GUARDIAN-CONTEXT] uid={uid} source=canonical_timeline empty fallback=provision_openclaw_history_migration",
            flush=True,
        )
    except Exception as e:
        print(
            f"[FLOW:GUARDIAN-CONTEXT] uid={uid} canonical_error={e} fallback=provision_openclaw_history_migration",
            flush=True,
        )

    try:
        resolved = await resolve_user_routing(uid)
        if not resolved:
            return []

        routing = resolved.get("routing", {})
        if not routing:
            return []

        agent_id = routing.get("agentId", "")
        if not agent_id:
            return []

        openclaw_user_id = agent_id[5:] if agent_id.startswith("ella-") else agent_id

        headers = {}
        if _PROVISION_API_TOKEN:
            headers["Authorization"] = f"Bearer {_PROVISION_API_TOKEN}"

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_PROVISION_API_URL}/users/{openclaw_user_id}/history",
                params={"limit": limit},
                headers=headers,
            )

        if resp.status_code != 200:
            return []

        data = resp.json()
        messages = data.get("messages", [])
        turns = []
        for m in messages[:limit]:
            role = m.get("role", m.get("type", "user"))
            content = m.get("content", m.get("text", ""))
            if content:
                turns.append({"role": role, "content": str(content)[:500]})
        print(
            f"[FLOW:GUARDIAN-CONTEXT] uid={uid} source=provision_openclaw_history_migration turns={len(turns)}",
            flush=True,
        )
        return turns
    except Exception as e:
        print(f"[CONSOLIDATOR] chat history fetch error: {e}", flush=True)
        return []


async def _consolidate_queue(
    uid: str,
    pending: list[dict],
    recently_consumed: list[dict],
    chat_turns: list[dict],
    echo_risk: str = "unknown",
) -> Optional[str]:
    """Call LLM to consolidate a pile of pending guardian queue items.

    Returns:
        str  — consolidated spoken message to enqueue (plain text, no SSML)
        None — all items are resolved/irrelevant, nothing to say
    """
    pending_text = "\n".join(
        f"- [{i+1}] ({item.get('trigger_type', '?')} at {item.get('created_at', '?')}): {item.get('message', '')}"
        for i, item in enumerate(pending)
    )

    consumed_text = ""
    if recently_consumed:
        consumed_text = "\nMessages Ella just played aloud (last 60s):\n" + "\n".join(
            f"- {c.get('message', '')}" for c in recently_consumed
        )

    context_text = ""
    if chat_turns:
        context_text = "\nRecent conversation (newest first):\n" + "\n".join(
            f"- {t['role']}: {t['content']}" for t in chat_turns
        )

    echo_instruction = ""
    if echo_risk not in ("none", "unknown"):
        echo_instruction = (
            f"\n\nIMPORTANT: Audio is playing through a speaker (echo_risk={echo_risk}). "
            "Some pending alerts may be echoes — transcriptions of audio Ella just played. "
            "Compare pending items against recently played messages and discard obvious echoes."
        )

    system_prompt = """You are Ella's alert consolidator. You receive a list of pending unplayed alerts and recent conversation context.

Your job:
1. Discard alerts that are no longer relevant (user already resolved the situation, conversation moved on)
2. Discard duplicate or near-duplicate alerts
3. Discard echo alerts (transcriptions of audio Ella just played)
4. Merge what remains into ONE concise spoken message Ella will say aloud

Rules:
- URGENT alerts (fall, emergency, "can't breathe") are NEVER discarded, always included
- If EVERYTHING is resolved or irrelevant, output exactly: NULL
- Output ONLY the spoken message text, no preamble, no JSON, no quotes
- Keep it under 40 words — this will be spoken aloud
- Natural spoken language only — no markdown, no bullet points"""

    user_prompt = (
        f"Pending alerts ({len(pending)} items):\n{pending_text}"
        f"{consumed_text}"
        f"{context_text}"
        f"{echo_instruction}"
        "\n\nOutput the consolidated spoken message, or NULL if nothing needs to be said:"
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{_LLM_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {_LLM_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": _LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 100,
                    "temperature": 0.2,
                },
            )

        if resp.status_code != 200:
            print(f"[CONSOLIDATOR] LLM error {resp.status_code}: {resp.text[:200]}", flush=True)
            return pending[0].get("message", "")

        content = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[CONSOLIDATOR] uid={uid} pending={len(pending)} result={content[:80]!r}", flush=True)

        if content.upper() == "NULL" or not content:
            return None
        return content

    except Exception as e:
        print(f"[CONSOLIDATOR] error: {e}", flush=True)
        return pending[0].get("message", "")


# ---------------------------------------------------------------------------
# GET /v1/ella/guardian/next-audio?uid={uid}
# iOS polls this. Returns and consumes next queued audio clip.
# ---------------------------------------------------------------------------


@router.get("/next-audio")
async def next_audio(uid: str):
    """Pop next audio clip from queue. Consolidates if pile-up detected."""
    if not uid or uid == "unknown":
        return {"url": None}

    _start = time.time()
    pool = await _get_pool()

    # --- Check queue depth first (excluding debug items) ---
    depth_row = await pool.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE consumed_at IS NULL) AS pending
        FROM guardian_queue
        WHERE uid = $1 AND priority != 'debug'
        """,
        uid,
    )
    pending_count = depth_row["pending"] if depth_row else 0

    pending_rows: list[dict[str, Any]] = []
    if pending_count >= 2:
        pending_rows = await pool.fetch(
            """
            SELECT id, url, priority, message, trigger_type, metadata, created_at
            FROM guardian_queue
            WHERE uid = $1 AND consumed_at IS NULL AND priority != 'debug'
            ORDER BY
                CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                created_at ASC
            """,
            uid,
        )

        primary_row, superseded_rows = _select_same_trace_supersede_rows([dict(r) for r in pending_rows])
        if primary_row and superseded_rows:
            superseded_ids = [str(row["id"]) for row in superseded_rows]
            await pool.execute(
                "UPDATE guardian_queue SET consumed_at = NOW() WHERE id = ANY($1::text[])",
                superseded_ids,
            )
            _elapsed = int((time.time() - _start) * 1000)
            print(
                f"[FLOW:GUARDIAN-SUPERSEDE] uid={uid} trace={_queue_trace_id(primary_row)} "
                f"kept={primary_row['id']} dropped={len(superseded_ids)} latency={_elapsed}ms",
                flush=True,
            )
            await _log_pipeline_event(
                trace_id=_queue_trace_id(primary_row),
                uid=uid,
                stage="queue_superseded",
                status="success",
                latency_ms=_elapsed,
                metadata={
                    "queue_item_id": str(primary_row["id"]),
                    "priority": str(primary_row.get("priority") or ""),
                    "superseded_queue_item_ids": superseded_ids,
                },
            )
            pending_rows = [row for row in pending_rows if str(row["id"]) not in set(superseded_ids)]

    # --- Consolidation path ---
    if pending_count >= CONSOLIDATION_THRESHOLD:
        if not pending_rows:
            pending_rows = await pool.fetch(
                """
                SELECT id, url, priority, message, trigger_type, metadata, created_at
                FROM guardian_queue
                WHERE uid = $1 AND consumed_at IS NULL AND priority != 'debug'
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                    created_at ASC
                """,
                uid,
            )

        if len(pending_rows) >= CONSOLIDATION_THRESHOLD:
            print(f"[FLOW:CONSOLIDATOR] uid={uid} pending={len(pending_rows)} triggering consolidation", flush=True)
            # Fetch recently consumed items for echo detection
            consumed_rows = await pool.fetch(
                """
                SELECT message, trigger_type, consumed_at
                FROM guardian_queue
                WHERE uid = $1
                  AND consumed_at IS NOT NULL
                  AND consumed_at > NOW() - INTERVAL '60 seconds'
                  AND priority != 'debug'
                ORDER BY consumed_at DESC
                LIMIT 10
                """,
                uid,
            )

            playback = get_playback_event(uid)
            echo_risk = playback["echo_risk"] if playback else "unknown"

            chat_turns = await _get_recent_chat_turns(uid, limit=5)

            consolidated_msg = await _consolidate_queue(
                uid=uid,
                pending=[dict(r) for r in pending_rows],
                recently_consumed=[dict(r) for r in consumed_rows],
                chat_turns=chat_turns,
                echo_risk=echo_risk,
            )

            # Mark ALL pending items consumed
            pending_ids = [r["id"] for r in pending_rows]
            await pool.execute(
                "UPDATE guardian_queue SET consumed_at = NOW() WHERE id = ANY($1::text[])",
                pending_ids,
            )

            if consolidated_msg is None:
                _elapsed = int((time.time() - _start) * 1000)
                print(f"[FLOW:CONSOLIDATOR] uid={uid} result=null latency={_elapsed}ms", flush=True)
                return {"url": None}

            # Enqueue consolidated message so it plays next
            new_id = str(uuid.uuid4())
            source_trace_id = _trace_id_from_metadata(
                dict(pending_rows[0]).get("metadata") if pending_rows else None,
                new_id,
            )
            await pool.execute(
                """
                INSERT INTO guardian_queue (id, uid, url, priority, message, trigger_type, metadata)
                VALUES ($1, $2, '', 'urgent', $3, 'consolidated', '{}')
                """,
                new_id,
                uid,
                consolidated_msg,
            )
            await pool.execute(
                """
                UPDATE guardian_queue
                SET metadata = $2::jsonb
                WHERE id = $1
                """,
                new_id,
                json.dumps(
                    {
                        "trace_id": source_trace_id,
                        "queue_item_id": new_id,
                        "consolidated_from": pending_ids,
                    }
                ),
            )
            # Fall through to pop the newly-inserted consolidated item

    # --- Normal pop path ---
    row = await pool.fetchrow(
        """
        UPDATE guardian_queue
        SET consumed_at = NOW()
        WHERE id = (
            SELECT id FROM guardian_queue
            WHERE uid = $1 AND consumed_at IS NULL
            ORDER BY
                CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'normal' THEN 1
                    WHEN 'scheduled' THEN 2
                    WHEN 'debug' THEN 3
                    ELSE 4
                END,
                created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, url, priority, message, trigger_type, metadata, created_at
        """,
        uid,
    )

    _elapsed = int((time.time() - _start) * 1000)

    if row is None:
        # Don't log empty polls (too noisy — iOS polls every 3s)
        return {"url": None}

    print(
        f"[FLOW:GUARDIAN-POLL] uid={uid} popped id={row['id']} priority={row['priority']} latency={_elapsed}ms",
        flush=True,
    )

    meta = row["metadata"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    trace_id = _trace_id_from_metadata(meta, row["id"])
    meta.setdefault("trace_id", trace_id)
    await _log_pipeline_event(
        trace_id=trace_id,
        uid=uid,
        stage="audio_consumed",
        status="success",
        latency_ms=_elapsed,
        metadata={
            "queue_item_id": row["id"],
            "priority": row["priority"],
            "trigger_type": row["trigger_type"],
            "route": "ios_next_audio",
        },
    )

    return {
        "url": row["url"],
        "id": row["id"],
        "trace_id": trace_id,
        "priority": row["priority"],
        "message": row["message"],
        "trigger_type": row["trigger_type"],
        "metadata": meta,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/enqueue
# n8n calls this after TTS generation + audio upload.
# Accepts JSON body (EnqueueRequest).
# ---------------------------------------------------------------------------


@router.post("/enqueue")
async def enqueue(
    req: EnqueueRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Enqueue an audio clip for a user."""
    _start = time.time()
    _verify_key(x_guardian_key, key)

    uid = req.uid or req.userID
    if not uid:
        raise HTTPException(status_code=400, detail="uid (or userID) is required")

    item_id = req.id or f"guardian_{uuid.uuid4().hex[:12]}"
    trace_id = _trace_id_from_metadata(req.metadata, item_id)

    pool = await _get_pool()
    mode_row = await pool.fetchrow(
        "SELECT guardian_mode FROM users WHERE LOWER(omi_uid) = LOWER($1)",
        uid,
    )
    guardian_mode = mode_row["guardian_mode"] if mode_row else None

    normalized_mode = _normalize_mode(guardian_mode)

    # --- guardian_mode gate: reject inserts when guardian is OFF / suppressed ---
    if req.priority != "debug":
        allowed, reject_reason = _enqueue_allows_guardian_audio(guardian_mode, req)
        if not allowed:
            _elapsed = int((time.time() - _start) * 1000)
            print(
                f"[FLOW:GUARDIAN-ENQUEUE] uid={uid} REJECTED guardian_mode={normalized_mode or 'unknown'} "
                f"reason={reject_reason} latency={_elapsed}ms",
                flush=True,
            )
            await _log_pipeline_event(
                trace_id=trace_id,
                uid=uid,
                stage="queue_rejected",
                status="rejected",
                latency_ms=_elapsed,
                metadata={
                    "queue_item_id": item_id,
                    "priority": req.priority,
                    "trigger_type": req.trigger,
                    "reason": reject_reason,
                    "guardian_mode": normalized_mode,
                },
            )
            return {
                "ok": False,
                "rejected": True,
                "reason": reject_reason,
                "suggestion": "Route critical alerts to iMessage instead",
            }

    # Serialize metadata to JSON string for the JSONB column
    metadata = dict(req.metadata or {})
    metadata.setdefault("trace_id", trace_id)
    metadata.setdefault("queue_item_id", item_id)
    metadata_str = json.dumps(metadata)

    await pool.execute(
        """
        INSERT INTO guardian_queue (id, uid, url, priority, message, trigger_type, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (id) DO NOTHING
        """,
        item_id,
        uid,
        req.url,
        req.priority,
        req.message,
        req.trigger,
        metadata_str,
    )

    # Count pending items for this user
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM guardian_queue WHERE uid = $1 AND consumed_at IS NULL",
        uid,
    )

    _elapsed = int((time.time() - _start) * 1000)
    await _log_pipeline_event(
        trace_id=trace_id,
        uid=uid,
        stage="queue_inserted",
        status="success",
        latency_ms=_elapsed,
        metadata={
            "queue_item_id": item_id,
            "priority": req.priority,
            "trigger_type": req.trigger,
            "queued": count,
        },
    )
    print(
        f"[FLOW:GUARDIAN-ENQUEUE] uid={uid} id={item_id} priority={req.priority} "
        f"trigger={req.trigger} trace={trace_id} queued={count} latency={_elapsed}ms",
        flush=True,
    )

    return {"ok": True, "id": item_id, "trace_id": trace_id, "queued": count}


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/upload
# n8n uploads TTS audio binary (multipart form).
# ---------------------------------------------------------------------------


@router.post("/synthesize")
async def synthesize_audio(
    req: SynthesizeRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = None,
):
    """Resolve user voice settings and synthesize Guardian one-shot audio."""
    _verify_key(x_guardian_key, key)
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    voice_settings = app_settings_db.get_voice_settings(req.uid)
    effective = build_effective_voice_settings(req.uid, voice_settings)["effective_voice_settings"]
    requested_provider = (req.provider or "").strip().lower()
    provider_candidates = _guardian_tts_candidates(effective, requested_provider)
    if not provider_candidates:
        raise HTTPException(status_code=422, detail="No supported Guardian one-shot TTS providers available")

    payload = {"text": text}
    if req.voice_id:
        payload["voice_id"] = req.voice_id

    _start = time.time()
    attempts: list[dict[str, Any]] = []
    response: httpx.Response | None = None
    provider = provider_candidates[0]
    async with httpx.AsyncClient() as client:
        for candidate in provider_candidates:
            provider = candidate
            try:
                response = await client.post(
                    ELLA_INTERNAL_VOICE_TTS_URL,
                    json=payload,
                    headers={"X-TTS-Provider": candidate},
                    timeout=30.0,
                )
            except httpx.TimeoutException:
                attempts.append({"provider": candidate, "status": "timeout"})
                continue
            except Exception as exc:
                attempts.append({"provider": candidate, "status": "error", "detail": str(exc)[:160]})
                continue
            if response.status_code < 400:
                break
            attempts.append({"provider": candidate, "status": response.status_code})
        else:
            response = None

    elapsed_ms = int((time.time() - _start) * 1000)
    if response is None or response.status_code >= 400:
        raise HTTPException(status_code=502, detail={"error": "Guardian synthesis failed", "attempts": attempts})

    runtime_fallback_used = provider != provider_candidates[0]
    if runtime_fallback_used:
        attempts.append({"provider": provider, "status": response.status_code})

    print(
        f"[FLOW:GUARDIAN-SYNTHESIZE] uid={req.uid} trace={req.trace_id} "
        f"provider={provider} candidates={provider_candidates} voice_mode={effective['voice_mode']} "
        f"bytes={len(response.content)} latency={elapsed_ms}ms",
        flush=True,
    )
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "audio/mpeg"),
        headers={
            "X-Guardian-TTS-Provider": provider,
            "X-Guardian-Voice-Mode": effective["voice_mode"],
            "X-Guardian-Settings-Source": "server",
            "X-Guardian-TTS-Candidates": ",".join(provider_candidates),
            "X-Guardian-Fallback-Used": str(bool(effective.get("fallback_used") or runtime_fallback_used)).lower(),
            "X-Guardian-Synthesis-Latency-Ms": str(elapsed_ms),
        },
    )


def _guardian_tts_candidates(effective: dict[str, Any], requested_provider: str = "") -> list[str]:
    raw_candidates = []
    if requested_provider:
        raw_candidates.append(requested_provider)
    raw_candidates.extend(effective.get("one_shot_tts_candidates") or [])
    raw_candidates.append(effective.get("one_shot_tts_provider"))

    candidates: list[str] = []
    for provider in raw_candidates:
        normalized = str(provider or "").strip().lower()
        if normalized in TTS_PROVIDERS and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _store_audio_content(uid: str, content: bytes, filename: Optional[str] = None) -> dict:
    user_dir = os.path.join(AUDIO_BASE_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)

    ts = int(time.time())
    fname = filename or f"{ts}-{uuid.uuid4().hex[:12]}.mp3"
    filepath = os.path.join(user_dir, fname)

    with open(filepath, "wb") as f:
        f.write(content)

    public_url = f"{AUDIO_PUBLIC_URL}/{uid}/{fname}"
    return {
        "url": public_url,
        "path": filepath,
        "size_bytes": len(content),
        "filename": fname,
    }


@router.post("/upload")
async def upload_audio(
    file: UploadFile = File(...),
    uid: str = Form(...),
    filename: Optional[str] = Form(None),
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Upload an audio file and return its public URL."""
    _start = time.time()
    _verify_key(x_guardian_key, key)

    content = await file.read()
    result = _store_audio_content(uid, content, filename)

    _elapsed = int((time.time() - _start) * 1000)
    print(
        f"[FLOW:GUARDIAN-UPLOAD] uid={uid} file={result['filename']} size={result['size_bytes']}B "
        f"latency={_elapsed}ms url={result['url']}",
        flush=True,
    )

    return {k: v for k, v in result.items() if k != "filename"}


@router.post("/upload-json")
async def upload_audio_json(
    req: UploadJsonRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Upload a base64-encoded audio file and return its public URL."""
    _start = time.time()
    _verify_key(x_guardian_key, key)

    try:
        content = base64.b64decode(req.audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 audio") from exc

    result = _store_audio_content(req.uid, content, req.filename)

    _elapsed = int((time.time() - _start) * 1000)
    print(
        f"[FLOW:GUARDIAN-UPLOAD-JSON] uid={req.uid} file={result['filename']} size={result['size_bytes']}B "
        f"latency={_elapsed}ms url={result['url']}",
        flush=True,
    )

    return {k: v for k, v in result.items() if k != "filename"}


# ---------------------------------------------------------------------------
# GET /v1/ella/guardian/queue?uid={uid}
# Debug / dashboard endpoint. Returns full queue without consuming.
# ---------------------------------------------------------------------------


@router.get("/queue")
async def view_queue(uid: str):
    """View pending queue items for a user (non-consuming read)."""
    pool = await _get_pool()

    rows = await pool.fetch(
        """
        SELECT id, url, priority, message, trigger_type, created_at
        FROM guardian_queue
        WHERE uid = $1 AND consumed_at IS NULL
        ORDER BY
            CASE priority
                WHEN 'urgent' THEN 0
                WHEN 'normal' THEN 1
                WHEN 'scheduled' THEN 2
                WHEN 'debug' THEN 3
            END,
            created_at ASC
        """,
        uid,
    )

    items = [
        {
            "id": r["id"],
            "url": r["url"],
            "priority": r["priority"],
            "message": r["message"],
            "trigger_type": r["trigger_type"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]

    print(f"[FLOW:GUARDIAN-QUEUE] uid={uid} pending={len(items)}", flush=True)

    return {"uid": uid, "count": len(items), "items": items}


@router.get("/debug-events")
async def view_debug_events(
    uid: str,
    trace_id: Optional[str] = None,
    queue_item_id: Optional[str] = None,
    limit: int = 50,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Return recent Guardian pipeline/debug events for a user."""
    _verify_key(x_guardian_key, key)
    pool = await _get_pool()
    safe_limit = max(1, min(limit, 200))

    rows = await pool.fetch(
        """
        SELECT created_at, trace_id, stage, status, latency_ms, metadata
        FROM guardian_pipeline_events
        WHERE uid = $1
          AND ($2::text IS NULL OR trace_id = $2)
          AND ($3::text IS NULL OR metadata->>'queue_item_id' = $3)
        ORDER BY created_at DESC
        LIMIT $4
        """,
        uid,
        trace_id,
        queue_item_id,
        safe_limit,
    )

    items = [
        {
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "trace_id": row["trace_id"],
            "stage": row["stage"],
            "status": row["status"],
            "latency_ms": row["latency_ms"],
            "metadata": _coerce_metadata_dict(row["metadata"]),
        }
        for row in rows
    ]

    return {
        "uid": uid,
        "trace_id": trace_id,
        "queue_item_id": queue_item_id,
        "count": len(items),
        "items": items,
    }


@router.post("/debug-trigger")
async def debug_trigger(
    req: DebugTriggerRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Insert a direct debug Guardian queue item for device validation."""
    _verify_key(x_guardian_key, key)

    item_id = req.queue_item_id or f"guardian_{uuid.uuid4().hex[:12]}"
    trace_id = req.trace_id or item_id
    url = req.url or GUARDIAN_ACTIVE_AUDIO_URL
    metadata = dict(req.metadata or {})
    metadata.setdefault("trace_id", trace_id)
    metadata.setdefault("queue_item_id", item_id)
    metadata.setdefault("source", "guardian_debug_trigger")

    return await enqueue(
        EnqueueRequest(
            uid=req.uid,
            id=item_id,
            url=url,
            priority=req.priority,
            message=req.message,
            trigger=req.trigger_type,
            metadata=metadata,
        ),
        x_guardian_key=x_guardian_key,
        key=key,
    )


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/activate
# iOS calls when user enables Guardian Mode. Enqueues init audio.
# ---------------------------------------------------------------------------

GUARDIAN_ACTIVE_AUDIO_URL = f"{AUDIO_PUBLIC_URL}/system/guardian-active.mp3"


@router.post("/activate")
async def activate_guardian(request: Request):
    """
    Called by iOS when user enables Guardian Mode.
    Enqueues a "Guardian active" confirmation audio message.
    Also called on first successful queue poll to confirm connectivity.
    """
    body = await request.json()
    uid = body.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")

    print(f"[FLOW:GUARDIAN-ACTIVATE] uid={uid} starting activation", flush=True)

    pool = await _get_pool()

    # Check if we already sent an activation message in the last 5 minutes
    # (prevents duplicate init messages from rapid re-polls)
    recent = await pool.fetchval(
        """
        SELECT COUNT(*) FROM guardian_queue
        WHERE uid = $1 AND trigger_type = 'guardian-activate'
          AND created_at > NOW() - INTERVAL '5 minutes'
        """,
        uid,
    )
    if recent and recent > 0:
        print(f"[FLOW:GUARDIAN-ACTIVATE] uid={uid} already_active (dedup 5min)", flush=True)
        return {"ok": True, "status": "already_active", "uid": uid}

    # Enqueue the static guardian-active audio
    item_id = f"guardian_{uuid.uuid4().hex[:12]}"
    await pool.execute(
        """
        INSERT INTO guardian_queue (id, uid, url, priority, message, trigger_type, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (id) DO NOTHING
        """,
        item_id,
        uid,
        GUARDIAN_ACTIVE_AUDIO_URL,
        "urgent",
        "Guardian active. I am listening and will alert you if anything needs your attention.",
        "guardian-activate",
        json.dumps({"source": "ios-activate"}),
    )

    print(f"[FLOW:GUARDIAN-ACTIVATE] uid={uid} activated id={item_id}", flush=True)

    return {"ok": True, "status": "activated", "id": item_id, "uid": uid}


# ---------------------------------------------------------------------------
# GET /v1/ella/guardian/trace/{conversation_id}
# Full pipeline trace for a conversation (debug / dashboard).
# ---------------------------------------------------------------------------


@router.get("/trace/{conversation_id}")
async def get_pipeline_trace(conversation_id: str):
    """Get the full pipeline trace for a conversation."""
    try:
        pool = await _get_pool()
        rows = await pool.fetch(
            """
            SELECT stage, status, latency_ms, metadata, created_at, uid
            FROM guardian_pipeline_events
            WHERE trace_id = $1
            ORDER BY created_at ASC
            """,
            conversation_id,
        )

        if not rows:
            print(f"[FLOW:GUARDIAN-TRACE] conv={conversation_id} not_found", flush=True)
            return {"trace_id": conversation_id, "stages": [], "found": False}

        def _parse_meta(val):
            """Parse metadata: asyncpg may return JSONB as str or dict."""
            if val is None:
                return {}
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (ValueError, TypeError):
                    return {}
            return val

        stages = []
        for r in rows:
            meta = _parse_meta(r["metadata"])
            stages.append(
                {
                    "stage": r["stage"],
                    "status": r["status"],
                    "latency_ms": r["latency_ms"],
                    "metadata": meta,
                    "at": r["created_at"].isoformat() if r["created_at"] else None,
                }
            )

        # Calculate total latency
        first_ts = rows[0]["created_at"]
        last_ts = rows[-1]["created_at"]
        total_ms = None
        if first_ts and last_ts:
            total_ms = int((last_ts - first_ts).total_seconds() * 1000)

        escalated = any(_parse_meta(r["metadata"]).get("escalate") is True for r in rows)
        audio_delivered = any(
            r["stage"] in ("audio_consumed", "ios_playback_started", "ios_playback_completed") for r in rows
        )

        uid = rows[0]["uid"] if rows else ""

        print(
            f"[FLOW:GUARDIAN-TRACE] conv={conversation_id} uid={uid} stages={len(stages)} total_ms={total_ms} escalated={escalated}",
            flush=True,
        )

        return {
            "trace_id": conversation_id,
            "uid": uid,
            "stages": stages,
            "total_latency_ms": total_ms,
            "escalated": escalated,
            "audio_delivered": audio_delivered,
            "found": True,
        }
    except Exception as e:
        print(f"[FLOW:GUARDIAN-TRACE] conv={conversation_id} error={e}", flush=True)
        return {"error": str(e), "trace_id": conversation_id}


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/deliver
# Evaluate escalation policy and dispatch pending delivery steps to n8n.
# ---------------------------------------------------------------------------


@router.post("/deliver")
async def deliver(
    req: DeliverRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Evaluate escalation policy, reserve idempotency rows, then dispatch to n8n."""
    started_at = time.time()
    _verify_key(x_guardian_key, key)

    uid = req.uid.strip()
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")

    trace_id = req.trace_id or uid
    user, caregivers = await _load_delivery_context(uid)
    event = EscalationEvent(
        uid=uid,
        trace_id=trace_id,
        source=req.source,
        event_type=req.event_type,
        severity=req.severity,
        confidence=req.confidence,
        ambiguity=req.ambiguity,
        summary=req.summary,
        requested_channels=tuple(req.requested_channels),
        evidence=req.evidence,
    )
    decision = evaluate_escalation_policy(event, user, caregivers)
    decision_dict = decision.to_dict()

    await _log_pipeline_event(
        trace_id=trace_id,
        uid=uid,
        stage="escalation_decided",
        status="success",
        metadata={"decision": decision.decision, "steps": len(decision.delivery_plan)},
    )

    enriched_steps: list[dict] = []
    for step in decision_dict.get("delivery_plan", []):
        recipient = _recipient_for_step(step, user, caregivers)
        enriched_steps.append({**step, **recipient})

    pending_steps, skipped_steps = await _reserve_delivery_steps(trace_id, uid, enriched_steps)
    if not pending_steps:
        elapsed_ms = int((time.time() - started_at) * 1000)
        print(
            f"[FLOW:GUARDIAN-DELIVER] SKIP uid={uid} trace={trace_id} "
            f"reason=no_pending_steps skipped={len(skipped_steps)} latency={elapsed_ms}ms",
            flush=True,
        )
        return {
            "ok": True,
            "dispatched": False,
            "reason": "no_pending_delivery_steps",
            "decision": decision.decision,
            "trace_id": trace_id,
            "delivery_plan": enriched_steps,
            "skipped_delivery_plan": skipped_steps,
            "latency_ms": elapsed_ms,
        }

    dispatch_payload = {
        **decision_dict,
        "uid": uid,
        "trace_id": trace_id,
        "source": req.source,
        "event_type": req.event_type,
        "severity": req.severity,
        "summary": req.summary,
        "evidence": dict(req.evidence),
        "user_phone": user.user_phone,
        "user_email": user.user_email,
        "guardian_mode": str(user.guardian_mode or "off"),
        "caregivers": [_caregiver_payload(caregiver) for caregiver in caregivers],
        "delivery_plan": pending_steps,
        "selected_channels": pending_steps,
        "skipped_delivery_plan": skipped_steps,
    }

    dispatch_ok = False
    dispatch_error = ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                N8N_GUARDIAN_DELIVER_WEBHOOK,
                json=dispatch_payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Guardian-Key": GUARDIAN_WEBHOOK_KEY,
                },
            )
            dispatch_ok = response.status_code < 300
            if not dispatch_ok:
                dispatch_error = f"n8n returned {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        dispatch_error = f"n8n dispatch error: {exc}"

    elapsed_ms = int((time.time() - started_at) * 1000)
    if not dispatch_ok:
        await _mark_reserved_steps_dispatch_failed(trace_id, pending_steps, dispatch_error)

    await _log_pipeline_event(
        trace_id=trace_id,
        uid=uid,
        stage="delivery_dispatched" if dispatch_ok else "delivery_dispatch_failed",
        status="success" if dispatch_ok else "error",
        latency_ms=elapsed_ms,
        metadata={
            "channels": [step["channel"] for step in pending_steps],
            "skipped": len(skipped_steps),
            "n8n_status": "ok" if dispatch_ok else "failed",
            "n8n_error": dispatch_error,
        },
    )

    print(
        f"[FLOW:GUARDIAN-DELIVER] uid={uid} trace={trace_id} decision={decision.decision} "
        f"pending={len(pending_steps)} skipped={len(skipped_steps)} "
        f"dispatch={'ok' if dispatch_ok else 'FAILED'} latency={elapsed_ms}ms",
        flush=True,
    )
    return {
        "ok": dispatch_ok,
        "dispatched": dispatch_ok,
        "reason": "" if dispatch_ok else dispatch_error,
        "decision": decision.decision,
        "trace_id": trace_id,
        "delivery_plan": pending_steps,
        "skipped_delivery_plan": skipped_steps,
        "latency_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/email/send
# Optional SMTP fallback used by delivery workflows.
# ---------------------------------------------------------------------------


@router.post("/email/send")
async def email_send(
    req: EmailSendRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Send a guardian alert email via configured SMTP relay."""
    _verify_key(x_guardian_key, key)
    if not req.to:
        raise HTTPException(status_code=400, detail="email recipient is required")

    trace_id = req.trace_id or "unknown"
    uid = req.uid or "unknown"
    pool = await _get_pool()
    existing = await pool.fetchval(
        """
        SELECT status
        FROM guardian_delivery_log
        WHERE trace_id = $1 AND channel = 'email' AND target = $2
        """,
        trace_id,
        req.target,
    )
    if _delivery_status_blocks_dispatch(existing):
        return {"ok": True, "sent": False, "reason": f"already_{existing}", "trace_id": trace_id}

    await pool.execute(
        """
        INSERT INTO guardian_delivery_log (trace_id, uid, channel, target, recipient_email, status, provider_response)
        VALUES ($1, $2, 'email', $3, $4, 'sending', $5::jsonb)
        ON CONFLICT (trace_id, channel, target) DO UPDATE SET
            uid = EXCLUDED.uid,
            recipient_email = EXCLUDED.recipient_email,
            status = 'sending',
            error_message = NULL,
            provider_response = EXCLUDED.provider_response,
            updated_at = NOW()
        WHERE guardian_delivery_log.status NOT IN ('pending', 'sending', 'sent', 'success', 'delivered')
        """,
        trace_id,
        uid,
        req.target,
        req.to,
        json.dumps({"to": req.to, "subject": req.subject}),
    )

    message = MIMEText(req.body)
    message["Subject"] = req.subject
    message["To"] = req.to
    message["From"] = SMTP_FROM
    smtp_host = os.environ.get("ELLA_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("ELLA_SMTP_PORT", "587"))
    smtp_user = os.environ.get("ELLA_SMTP_USER", "")
    smtp_pass = os.environ.get("ELLA_SMTP_PASS", "")

    sent = False
    error = ""
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_user and smtp_pass:
                server.starttls()
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        sent = True
    except smtplib.SMTPRecipientsRefused as exc:
        error = f"recipient_refused: {str(exc)[:150]}"
    except smtplib.SMTPException as exc:
        error = f"smtp_error: {str(exc)[:150]}"
    except Exception as exc:
        error = f"send_error: {str(exc)[:150]}"

    await pool.execute(
        """
        UPDATE guardian_delivery_log
        SET status = $1, error_message = $2, updated_at = NOW()
        WHERE trace_id = $3 AND channel = 'email' AND target = $4
        """,
        "sent" if sent else "error",
        error,
        trace_id,
        req.target,
    )
    await _log_pipeline_event(
        trace_id=trace_id,
        uid=uid,
        stage="delivery_email_sent" if sent else "delivery_email_failed",
        status="success" if sent else "error",
        metadata={"to": req.to, "subject": req.subject[:80], "error": error if not sent else None},
    )

    if sent:
        return {"ok": True, "sent": True, "trace_id": trace_id, "to": req.to}
    return {"ok": False, "sent": False, "trace_id": trace_id, "error": error}


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/trace/log
# Log a pipeline event (called by n8n workflows).
# ---------------------------------------------------------------------------


@router.post("/trace/log")
async def log_pipeline_event(
    req: TraceLogRequest,
    x_guardian_key: Optional[str] = Header(None, alias="X-Guardian-Key"),
    key: Optional[str] = Header(None, alias="X-Key"),
):
    """Log a pipeline event (called by n8n workflows)."""
    _verify_optional_trace_key(x_guardian_key, key)
    metadata = dict(req.metadata or {})
    if req.error_detail:
        metadata["error_detail"] = req.error_detail

    await _log_pipeline_event(
        trace_id=req.trace_id,
        uid=req.uid or "",
        stage=req.stage,
        status=req.status,
        latency_ms=req.latency_ms,
        metadata=metadata,
    )

    channel = metadata.get("channel")
    if channel:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO guardian_delivery_log (trace_id, uid, channel, target, caregiver_id, status, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (trace_id, channel, target) DO UPDATE SET
                uid = EXCLUDED.uid,
                caregiver_id = EXCLUDED.caregiver_id,
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                updated_at = NOW()
            """,
            req.trace_id,
            req.uid or "",
            channel,
            metadata.get("target", "unknown"),
            metadata.get("caregiver_id"),
            req.status,
            req.error_detail,
        )

    print(
        f"[FLOW:GUARDIAN-TRACE-LOG] trace={req.trace_id} uid={req.uid or ''} "
        f"stage={req.stage} status={req.status} latency={req.latency_ms}ms",
        flush=True,
    )

    return {"ok": True, "logged": True, "trace_id": req.trace_id, "stage": req.stage, "status": req.status}


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/playback-event
# iOS calls this when guardian audio starts playing. Fire-and-forget.
# Records output route so the smart-queue consolidator knows echo risk.
# ---------------------------------------------------------------------------


@router.post("/playback-event")
async def record_playback_event(req: PlaybackEventRequest):
    """iOS calls this when guardian audio starts playing.
    Records output route so the consolidator knows echo risk."""
    echo_risk = _ECHO_RISK.get(req.port_type, "unknown")
    _playback_events[req.uid] = {
        "queue_item_id": req.queue_item_id,
        "trace_id": req.trace_id,
        "event_type": req.event_type,
        "port_type": req.port_type,
        "port_name": req.port_name,
        "device_uid": req.device_uid,
        "echo_risk": echo_risk,
        "duration_ms": req.duration_ms,
        "recorded_at": time.time(),
    }

    trace_id = req.trace_id or req.queue_item_id
    if trace_id:
        event_type = (req.event_type or "started").strip().lower().replace(" ", "_")
        status = "error" if event_type == "failed" else "success"
        await _log_pipeline_event(
            trace_id=trace_id,
            uid=req.uid,
            stage=f"ios_playback_{event_type}",
            status=status,
            latency_ms=req.duration_ms if event_type in ("completed", "failed") else None,
            metadata={
                "queue_item_id": req.queue_item_id,
                "port_type": req.port_type,
                "port_name": req.port_name,
                "device_uid": req.device_uid,
                "echo_risk": echo_risk,
                "duration_ms": req.duration_ms,
                **(req.metadata or {}),
            },
        )

    print(
        f"[FLOW:PLAYBACK-EVENT] uid={req.uid} trace={trace_id} item={req.queue_item_id} event={req.event_type} port={req.port_type} risk={echo_risk} device={req.port_name!r}",
        flush=True,
    )
    return {"ok": True, "trace_id": trace_id, "echo_risk": echo_risk}


@router.post("/playback-debug")
async def record_playback_debug_event(req: PlaybackDebugEventRequest):
    """Broader iOS Guardian debug-event sink.

    Unlike /playback-event, this accepts non-route lifecycle markers such as
    payload receipt, inject requests, queue state transitions, and early-return
    failures that happen before AVFoundation playback starts.
    """

    metadata = dict(req.metadata or {})
    event_name = (req.event_name or "unknown").strip().lower().replace(" ", "_")
    stage = req.stage or f"ios_debug_{event_name}"
    trace_id = req.trace_id or req.queue_item_id
    echo_risk = _ECHO_RISK.get(req.port_type, "unknown") if req.port_type else "unknown"

    if trace_id:
        await _log_pipeline_event(
            trace_id=trace_id,
            uid=req.uid,
            stage=stage,
            status=req.status,
            latency_ms=req.latency_ms,
            metadata={
                "event_name": event_name,
                "queue_item_id": req.queue_item_id,
                "port_type": req.port_type,
                "port_name": req.port_name,
                "device_uid": req.device_uid,
                "echo_risk": echo_risk,
                **metadata,
            },
        )

    print(
        f"[FLOW:PLAYBACK-DEBUG] uid={req.uid} trace={trace_id} item={req.queue_item_id} "
        f"event={event_name} status={req.status} port={req.port_type or 'n/a'}",
        flush=True,
    )
    return {"ok": True, "trace_id": trace_id, "stage": stage, "event_name": event_name}


def get_playback_event(uid: str) -> dict | None:
    """Return the most recent playback event for a UID, or None if >60s old."""
    event = _playback_events.get(uid)
    if not event:
        return None
    if time.time() - event["recorded_at"] > 60:
        return None  # stale — more than 60s old
    return event
