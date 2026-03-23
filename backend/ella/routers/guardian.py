"""
Guardian Mode Router - Audio delivery queue for iOS Guardian Mode.

Endpoints:
- GET  /v1/ella/guardian/next-audio?uid={uid}  - iOS polls, pops from queue
- POST /v1/ella/guardian/enqueue               - n8n enqueues after TTS
- POST /v1/ella/guardian/upload                - n8n uploads MP3 binary
- GET  /v1/ella/guardian/queue?uid={uid}       - debug/dashboard view

Queue: ella-postgres guardian_queue table.
Audio files: /var/www/ella-ai-care.com/audio/{uid}/*.mp3
"""

import json
import os
import time
import uuid
from typing import Optional

import asyncpg
import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ella.routers.resolve import resolve_user_routing

router = APIRouter(prefix="/v1/ella/guardian", tags=["Guardian Mode"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUDIO_BASE_DIR = "/var/www/ella-ai-care.com/audio"
AUDIO_PUBLIC_URL = "https://ella-ai-care.com/audio"
GUARDIAN_WEBHOOK_KEY = os.getenv(
    "GUARDIAN_WEBHOOK_KEY", "4f13699d8462adf71e35d2098e6a791f"
)

# Consolidate queue when this many non-debug items are pending
CONSOLIDATION_THRESHOLD = int(os.getenv("CONSOLIDATION_THRESHOLD", "3"))

# Provision API for chat history lookups
_PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
_PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")

# LLM settings for consolidator (uses XAI Grok fast by default)
_LLM_API_KEY = os.getenv("XAI_API_KEY", "")
_LLM_API_BASE = "https://api.x.ai/v1"
_LLM_MODEL = "grok-3-mini-fast"

# Database connection pool (lazy-initialized)
_pool: Optional[asyncpg.Pool] = None

# ---------------------------------------------------------------------------
# In-memory playback event store (echo risk tracking)
# ---------------------------------------------------------------------------

# uid -> last playback event (resets on restart — used only for echo risk)
_playback_events: dict[str, dict] = {}

# Echo risk by iOS AVAudioSession portType rawValue
_ECHO_RISK = {
    "Speaker": "high",        # builtInSpeaker
    "Receiver": "none",       # builtInReceiver
    "Headphones": "none",     # headphones
    "BluetoothHFP": "none",   # BT headset (hands-free)
    "BluetoothA2DP": "high",  # BT speaker/headphones
    "BluetoothLE": "medium",  # BT LE audio
    "AirPlay": "very_high",   # AirPlay / Apple TV
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


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PlaybackEventRequest(BaseModel):
    """JSON body for /playback-event endpoint."""
    uid: str
    port_type: str       # AVAudioSession portType rawValue (e.g. "Speaker", "BluetoothA2DP")
    port_name: str = ""  # human-readable device name (e.g. "AirPods Pro")
    device_uid: str = "" # unique device ID from AVAudioSessionPortDescription
    duration_ms: int = 0 # estimated audio duration in ms


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

    # --- Consolidation path ---
    if pending_count >= CONSOLIDATION_THRESHOLD:
        print(f"[FLOW:CONSOLIDATOR] uid={uid} pending={pending_count} triggering consolidation", flush=True)

        # Fetch all pending non-debug items
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

        # Urgent items bypass consolidation — fall through to normal pop
        urgent = [r for r in pending_rows if r["priority"] == "urgent"]
        if not urgent:
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
                "UPDATE guardian_queue SET consumed_at = NOW() WHERE id = ANY($1::uuid[])",
                pending_ids,
            )

            if consolidated_msg is None:
                _elapsed = int((time.time() - _start) * 1000)
                print(f"[FLOW:CONSOLIDATOR] uid={uid} result=null latency={_elapsed}ms", flush=True)
                return {"url": None}

            # Enqueue consolidated message so it plays next
            new_id = str(uuid.uuid4())
            await pool.execute(
                """
                INSERT INTO guardian_queue (id, uid, url, priority, message, trigger_type, metadata)
                VALUES ($1, $2, '', 'urgent', $3, 'consolidated', '{}')
                """,
                new_id,
                uid,
                consolidated_msg,
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

    print(f"[FLOW:GUARDIAN-POLL] uid={uid} popped id={row['id']} priority={row['priority']} latency={_elapsed}ms", flush=True)

    return {
        "url": row["url"],
        "id": row["id"],
        "priority": row["priority"],
        "message": row["message"],
        "trigger_type": row["trigger_type"],
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
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

    # Serialize metadata to JSON string for the JSONB column
    metadata_str = json.dumps(req.metadata) if req.metadata else "{}"

    pool = await _get_pool()
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
    print(f"[FLOW:GUARDIAN-ENQUEUE] uid={uid} id={item_id} priority={req.priority} trigger={req.trigger} queued={count} latency={_elapsed}ms", flush=True)

    return {"ok": True, "id": item_id, "queued": count}


# ---------------------------------------------------------------------------
# POST /v1/ella/guardian/upload
# n8n uploads TTS audio binary (multipart form).
# ---------------------------------------------------------------------------


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

    # Create user directory
    user_dir = os.path.join(AUDIO_BASE_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)

    # Generate filename
    ts = int(time.time())
    fname = filename or f"{ts}-{uuid.uuid4().hex[:12]}.mp3"
    filepath = os.path.join(user_dir, fname)

    # Write file
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    public_url = f"{AUDIO_PUBLIC_URL}/{uid}/{fname}"

    _elapsed = int((time.time() - _start) * 1000)
    print(f"[FLOW:GUARDIAN-UPLOAD] uid={uid} file={fname} size={len(content)}B latency={_elapsed}ms url={public_url}", flush=True)

    return {
        "url": public_url,
        "path": filepath,
        "size_bytes": len(content),
    }


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
            stages.append({
                "stage": r["stage"],
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "metadata": meta,
                "at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        # Calculate total latency
        first_ts = rows[0]["created_at"]
        last_ts = rows[-1]["created_at"]
        total_ms = None
        if first_ts and last_ts:
            total_ms = int((last_ts - first_ts).total_seconds() * 1000)

        escalated = any(
            _parse_meta(r["metadata"]).get("escalate") is True
            for r in rows
        )
        audio_delivered = any(
            r["stage"] == "audio_consumed"
            for r in rows
        )

        uid = rows[0]["uid"] if rows else ""

        print(f"[FLOW:GUARDIAN-TRACE] conv={conversation_id} uid={uid} stages={len(stages)} total_ms={total_ms} escalated={escalated}", flush=True)

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
# POST /v1/ella/guardian/trace/log
# Log a pipeline event (called by n8n workflows).
# ---------------------------------------------------------------------------


@router.post("/trace/log")
async def log_pipeline_event(request: Request):
    """Log a pipeline event (called by n8n workflows)."""
    request_body = await request.json()

    trace_id = request_body.get("trace_id")
    uid = request_body.get("uid", "")
    stage = request_body.get("stage")
    status = request_body.get("status", "success")
    latency_ms = request_body.get("latency_ms")
    metadata = request_body.get("metadata", {})

    if not trace_id or not stage:
        raise HTTPException(status_code=400, detail="trace_id and stage required")

    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO guardian_pipeline_events (trace_id, uid, stage, status, latency_ms, metadata)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        trace_id,
        uid,
        stage,
        status,
        latency_ms,
        json.dumps(metadata),
    )

    print(f"[FLOW:GUARDIAN-TRACE-LOG] trace={trace_id} uid={uid} stage={stage} status={status} latency={latency_ms}ms", flush=True)

    return {"logged": True, "trace_id": trace_id, "stage": stage}


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
        "port_type": req.port_type,
        "port_name": req.port_name,
        "device_uid": req.device_uid,
        "echo_risk": echo_risk,
        "recorded_at": time.time(),
    }
    print(
        f"[FLOW:PLAYBACK-EVENT] uid={req.uid} port={req.port_type} risk={echo_risk} device={req.port_name!r}",
        flush=True,
    )
    return {"echo_risk": echo_risk}


def get_playback_event(uid: str) -> dict | None:
    """Return the most recent playback event for a UID, or None if >60s old."""
    event = _playback_events.get(uid)
    if not event:
        return None
    if time.time() - event["recorded_at"] > 60:
        return None  # stale — more than 60s old
    return event
