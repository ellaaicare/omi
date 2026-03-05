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

import os
import time
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/v1/ella/guardian", tags=["Guardian Mode"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUDIO_BASE_DIR = "/var/www/ella-ai-care.com/audio"
AUDIO_PUBLIC_URL = "https://ella-ai-care.com/audio"
GUARDIAN_WEBHOOK_KEY = os.getenv(
    "GUARDIAN_WEBHOOK_KEY", "4f13699d8462adf71e35d2098e6a791f"
)

# Database connection pool (lazy-initialized)
_pool: Optional[asyncpg.Pool] = None


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
    """Pop next audio clip from queue for the given user."""
    if not uid or uid == "unknown":
        return {"url": None}

    pool = await _get_pool()

    # Atomic pop: SELECT ... FOR UPDATE SKIP LOCKED + UPDATE in one query
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
                END,
                created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, url, priority, message, created_at
        """,
        uid,
    )

    if row is None:
        return {"url": None}

    return {
        "url": row["url"],
        "id": row["id"],
        "priority": row["priority"],
        "message": row["message"],
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
    _verify_key(x_guardian_key, key)

    uid = req.uid or req.userID
    if not uid:
        raise HTTPException(status_code=400, detail="uid (or userID) is required")

    item_id = req.id or f"guardian_{uuid.uuid4().hex[:12]}"

    # Serialize metadata to JSON string for the JSONB column
    import json as _json
    metadata_str = _json.dumps(req.metadata) if req.metadata else "{}"

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

    return {"uid": uid, "count": len(items), "items": items}
