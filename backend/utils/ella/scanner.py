# Ella Scanner Integration
#
# Sends real-time transcript segments to n8n scanner for urgency detection.
# Fire-and-forget with short timeout - doesn't block transcription flow.

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests

from database.honcho_attestation import authority_credential

from .config import ELLA_CONFIG

GUARDIAN_TRACE_LOG_URL = os.getenv(
    "ELLA_GUARDIAN_TRACE_LOG_URL",
    "http://127.0.0.1:8000/v1/ella/guardian/trace/log",
)
ELLA_POSTGRES_HOST = os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1")
ELLA_POSTGRES_PORT = int(os.getenv("ELLA_POSTGRES_PORT", "5433"))
ELLA_POSTGRES_USER = os.getenv("ELLA_POSTGRES_USER", "postgres")
ELLA_POSTGRES_PASSWORD = authority_credential("ELLA_POSTGRES_PASSWORD", default="postgres", strip=False)
ELLA_POSTGRES_DATABASE = os.getenv("ELLA_POSTGRES_DATABASE", "ella_ai")
GUARDIAN_ENQUEUE_URL = os.getenv("ELLA_GUARDIAN_ENQUEUE_URL", "http://127.0.0.1:8000/v1/ella/guardian/enqueue")
GUARDIAN_WEBHOOK_KEY = authority_credential("GUARDIAN_WEBHOOK_KEY", strip=False)
GUARDIAN_WAKE_ACK_AUDIO_URL = os.getenv(
    "ELLA_GUARDIAN_WAKE_ACK_AUDIO_URL",
    "https://ella-ai-care.com/audio/system/wake_ack_pulse.mp3",
)
GUARDIAN_WAKE_ACK_TIMEOUT_S = float(os.getenv("ELLA_GUARDIAN_WAKE_ACK_TIMEOUT_S", "2.0"))
GUARDIAN_WAKE_ACK_DIRECT_DB = os.getenv("ELLA_GUARDIAN_WAKE_ACK_DIRECT_DB", "true").lower() == "true"
WAKE_WORD_PREFIX_MAX_WORDS = 4
WAKE_WORD_PENDING_WINDOW_S = float(os.getenv("ELLA_WAKE_WORD_PENDING_WINDOW_S", "12.0"))
SCANNER_CONTEXT_WINDOW_S = float(os.getenv("ELLA_SCANNER_CONTEXT_WINDOW_S", "12.0"))
GUARDIAN_ECHO_SUPPRESSION_SECONDS = int(os.getenv("ELLA_GUARDIAN_ECHO_SUPPRESSION_SECONDS", "45"))
SCANNER_AMBIENT_BATCHING_ENABLED = os.getenv("ELLA_SCANNER_AMBIENT_BATCHING_ENABLED", "true").lower() == "true"
SCANNER_AMBIENT_BATCH_SECONDS = float(os.getenv("ELLA_SCANNER_AMBIENT_BATCH_SECONDS", "10.0"))
SCANNER_AMBIENT_BATCH_WORDS = int(os.getenv("ELLA_SCANNER_AMBIENT_BATCH_WORDS", "70"))
SCANNER_AMBIENT_BATCH_MAX_WORDS = int(os.getenv("ELLA_SCANNER_AMBIENT_BATCH_MAX_WORDS", "180"))
SCANNER_RATE_LIMIT_DEFAULT_BACKOFF_S = float(os.getenv("ELLA_SCANNER_RATE_LIMIT_DEFAULT_BACKOFF_S", "15.0"))
_ECHO_RISKY_OUTPUTS = {"medium", "high", "very_high"}
_GUARDIAN_ECHO_MARKERS = (
    "hi greg",
    "heard my name",
    "i heard my name",
    "i'm here with you",
    "im here with you",
    "here with you",
    "tell me what you need",
    "just talking about names",
    "just talking about me",
    "i heard you i am checking that now",
)
WAKE_WORD_ALIASES = (
    "hey ella",
    "ella",
    "ela",
    "ellah",
    "ellaa",
    "hey ela",
    "hey el",
    "hey ell",
    "hey elle",
    "hey eleve",
    "hey eleven",
    "ask ella",
    "ask ela",
    "tell ella",
    "tell ela",
)

WAKE_WORD_QUESTION_STARTERS = (
    "what",
    "can",
    "could",
    "would",
    "do",
    "did",
    "does",
    "tell",
    "check",
    "find",
    "search",
    "remind",
    "help",
    "how",
    "why",
    "when",
    "where",
    "who",
    "is",
    "are",
    "was",
    "were",
)

_EMERGENCY_PATTERN = re.compile(
    r"\b("
    r"911|emergency|urgent|ambulance|paramedic|"
    r"help me|need help|call for help|"
    r"can(?:not|'t|t) breathe|chest pain|heart attack|stroke|seizure|"
    r"fell|fallen|falling|bleeding|choking|overdose|"
    r"suicidal|kill myself|hurt myself|fire|break in|burglar"
    r")\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)")
_SCANNER_BATCHES: dict[tuple[str, str, str], dict] = {}
_SCANNER_RATE_LIMIT_UNTIL = {
    "global": 0.0,
    "users": {},
}
_SCANNER_STATE_LOCK = threading.Lock()


def _trace_id_for(conversation_id: str) -> str:
    """Use the conversation id as the cross-service guardian trace id."""
    trace_id = str(conversation_id or "").strip()
    if trace_id and trace_id.lower() != "unknown":
        return trace_id
    return f"scanner-{int(time.time() * 1000)}"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()


def _word_count(text: str) -> int:
    return len(_normalize_text(text).split())


def _normalize_for_echo_match(text: str) -> str:
    normalized = text.lower().replace("’", "'")
    normalized = re.sub(r"[^a-z0-9' ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _looks_like_guardian_echo_text(text: str) -> bool:
    normalized = _normalize_for_echo_match(text)
    if not normalized:
        return False

    marker_hits = sum(1 for marker in _GUARDIAN_ECHO_MARKERS if marker in normalized)
    if marker_hits >= 2:
        return True
    if "hi greg" in normalized and "heard my name" in normalized:
        return True
    if "tell me what you need" in normalized and ("just talking about" in normalized or "here with you" in normalized):
        return True
    return False


def _recent_risky_playback_event(uid: str) -> dict | None:
    try:
        from ella.routers.guardian import get_playback_event
    except Exception:
        return None

    event = get_playback_event(uid)
    if not event:
        return None
    if event.get("echo_risk") not in _ECHO_RISKY_OUTPUTS:
        return None
    recorded_at = event.get("recorded_at")
    if isinstance(recorded_at, (int, float)) and time.time() - recorded_at > GUARDIAN_ECHO_SUPPRESSION_SECONDS:
        return None
    return event


def should_suppress_guardian_echo(
    uid: str, scanner_segments: List[dict], playback_event: Optional[dict] = None
) -> bool:
    """Return True when the scanner input is likely Guardian audio re-captured by the mic."""
    text = _combined_segment_text(scanner_segments)
    if not _looks_like_guardian_echo_text(text):
        return False

    event = playback_event if playback_event is not None else _recent_risky_playback_event(uid)
    if not event:
        return False
    return event.get("echo_risk") in _ECHO_RISKY_OUTPUTS


def _combined_segment_text(segments: List[dict]) -> str:
    return " ".join((segment.get("text") or "").strip() for segment in segments if segment.get("text")).strip()


def contains_wake_phrase(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if any(alias in normalized for alias in WAKE_WORD_ALIASES):
        return True

    # Deepgram/Soniox sometimes renders "Hey Ella" as "A l" or "AL" at
    # utterance start. Keep this narrow so ambient speech is not promoted into
    # wake traffic: it must be followed by a question/command starter.
    tokens = normalized.split()
    if len(tokens) >= 3 and tokens[0] == "a" and tokens[1] == "l":
        return tokens[2] in WAKE_WORD_QUESTION_STARTERS
    if len(tokens) >= 2 and tokens[0] == "al":
        return tokens[1] in WAKE_WORD_QUESTION_STARTERS
    return False


def contains_emergency_phrase(text: str) -> bool:
    return bool(_EMERGENCY_PATTERN.search(text or ""))


def scanner_immediate_reason(text: str, *, wake_prefix_recent: Optional[bool] = None) -> Optional[str]:
    if contains_wake_phrase(text) or wake_prefix_recent:
        return "wake"
    if contains_emergency_phrase(text):
        return "emergency"
    return None


def canonicalize_wake_phrase(text: str) -> str:
    """Canonicalize likely wake-word STT variants for scanner dispatch only."""
    if not text:
        return text

    original = text
    text = re.sub(
        r"\bhey[\s,.-]*(?:ella|ela|ellah|ellaa|el|ell|elle|eleve|eleven)\b",
        "Hey Ella",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^\s*a[\s,.-]*l\b(?=\s+(?:" + "|".join(re.escape(word) for word in WAKE_WORD_QUESTION_STARTERS) + r")\b)",
        "Hey Ella",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^\s*al\b(?=\s+(?:" + "|".join(re.escape(word) for word in WAKE_WORD_QUESTION_STARTERS) + r")\b)",
        "Hey Ella",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if text != original:
        print(
            f"[SCANNER-WAKE] canonicalized wake phrase for scanner dispatch: {original[:80]!r} -> {text[:80]!r}",
            flush=True,
        )
    return text


def is_short_wake_prefix_only(segments: List[dict]) -> bool:
    combined = _combined_segment_text(segments)
    if not combined:
        return False
    candidate = combined.strip()
    if re.fullmatch(r".*[.?!]+", candidate):
        candidate = re.sub(r"[.?!]+\s*$", "", candidate).strip()
    normalized = _normalize_text(candidate)
    if not contains_wake_phrase(normalized):
        return False
    # Only hold very short prefix-like utterances such as "Hey Ella".
    return len(normalized.split()) <= WAKE_WORD_PREFIX_MAX_WORDS


def prepare_scanner_segments_for_dispatch(
    current_segments: List[dict],
    pending_wake_prefix_segments: Optional[List[dict]] = None,
    pending_wake_prefix_since: Optional[float] = None,
    *,
    now: Optional[float] = None,
    max_pending_window_s: float = WAKE_WORD_PENDING_WINDOW_S,
):
    """
    Prepare transcript batches for scanner dispatch.

    Short wake-word-only batches are dispatched immediately so a reconnect between
    "Hey Ella" and the follow-up question cannot lose the wake event. Older
    pending-prefix state is still honored for workers that already carry one.
    """
    now = now if now is not None else time.time()
    pending_wake_prefix_segments = list(pending_wake_prefix_segments or [])

    if pending_wake_prefix_segments and pending_wake_prefix_since is not None:
        if now - pending_wake_prefix_since > max_pending_window_s:
            pending_wake_prefix_segments = []
            pending_wake_prefix_since = None

    if not current_segments:
        return (
            [],
            pending_wake_prefix_segments,
            pending_wake_prefix_since,
            {
                "action": "empty",
                "prepended_count": 0,
            },
        )

    if is_short_wake_prefix_only(current_segments):
        return (
            list(current_segments),
            [],
            None,
            {
                "action": "direct_wake_prefix_dispatch",
                "prepended_count": 0,
            },
        )

    dispatch_segments = list(current_segments)
    prepended_count = 0
    if pending_wake_prefix_segments and not contains_wake_phrase(_combined_segment_text(current_segments)):
        dispatch_segments = pending_wake_prefix_segments + dispatch_segments
        prepended_count = len(pending_wake_prefix_segments)
        pending_wake_prefix_segments = []
        pending_wake_prefix_since = None

    return (
        dispatch_segments,
        pending_wake_prefix_segments,
        pending_wake_prefix_since,
        {
            "action": "prepend_pending_wake_prefix" if prepended_count else "direct_dispatch",
            "prepended_count": prepended_count,
        },
    )


def scanner_payload_preview(segments: List[dict], *, limit: int = 4) -> List[dict]:
    preview = []
    for segment in segments[:limit]:
        preview.append(
            {
                "speaker": segment.get("speaker") or f"SPEAKER_{segment.get('speaker_id', 0)}",
                "text": (segment.get("text") or "")[:120],
            }
        )
    return preview


def scanner_model_name() -> str:
    return (
        os.getenv("ELLA_SCANNER_GROQ_MODEL")
        or os.getenv("GROQ_MODEL")
        or os.getenv("OMI_GROQ_MODEL")
        or os.getenv("SCANNER_MODEL")
        or "unknown"
    )


def _scanner_batch_key(uid: str, conversation_id: str, device_type: str) -> tuple[str, str, str]:
    return (str(uid), str(conversation_id), str(device_type or "omi"))


def _new_ambient_batch(now: float) -> dict:
    return {
        "segments": [],
        "started_at": now,
        "last_at": now,
        "word_count": 0,
    }


def _append_to_ambient_batch(batch: dict, segments: List[dict], now: float) -> None:
    batch["segments"].extend(segments)
    batch["last_at"] = now
    batch["word_count"] += _word_count(_combined_segment_text(segments))


def _ambient_flush_reason(batch: dict, now: float) -> Optional[str]:
    elapsed_s = max(0.0, now - float(batch.get("started_at") or now))
    if int(batch.get("word_count") or 0) >= SCANNER_AMBIENT_BATCH_WORDS:
        return "word_threshold"
    if elapsed_s >= SCANNER_AMBIENT_BATCH_SECONDS:
        return "time_threshold"
    return None


def _duration_to_seconds(value: str) -> Optional[float]:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    # Groq reset headers are commonly duration strings such as "1m2.5s".
    total = 0.0
    matched = False
    for match in _DURATION_RE.finditer(raw):
        matched = True
        amount = float(match.group("value"))
        unit = match.group("unit")
        if unit == "ms":
            total += amount / 1000.0
        elif unit == "s":
            total += amount
        elif unit == "m":
            total += amount * 60.0
        elif unit == "h":
            total += amount * 3600.0
    if matched:
        return max(0.0, total)

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def rate_limit_status_from_response(resp) -> dict:
    headers = {str(k).lower(): str(v) for k, v in getattr(resp, "headers", {}).items()}
    retry_after_s = _duration_to_seconds(headers.get("retry-after", ""))
    reset_requests_s = _duration_to_seconds(headers.get("x-ratelimit-reset-requests", ""))
    reset_tokens_s = _duration_to_seconds(headers.get("x-ratelimit-reset-tokens", ""))
    status_code = int(getattr(resp, "status_code", 0) or 0)
    remaining_requests = headers.get("x-ratelimit-remaining-requests")
    limited = status_code == 429 or headers.get("retry-after") is not None or remaining_requests == "0"
    return {
        "limited": limited,
        "retry_after_s": retry_after_s,
        "reset_requests_s": reset_requests_s,
        "reset_tokens_s": reset_tokens_s,
        "remaining_requests": remaining_requests,
        "limit_requests": headers.get("x-ratelimit-limit-requests"),
        "remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
        "limit_tokens": headers.get("x-ratelimit-limit-tokens"),
    }


def _record_scanner_backpressure(uid: str, status: dict, now: float) -> None:
    if not status.get("limited"):
        return
    retry_after_s = (
        status.get("retry_after_s")
        or status.get("reset_requests_s")
        or status.get("reset_tokens_s")
        or SCANNER_RATE_LIMIT_DEFAULT_BACKOFF_S
    )
    until = now + max(0.0, float(retry_after_s))
    with _SCANNER_STATE_LOCK:
        _SCANNER_RATE_LIMIT_UNTIL["global"] = max(float(_SCANNER_RATE_LIMIT_UNTIL.get("global") or 0.0), until)
        users = _SCANNER_RATE_LIMIT_UNTIL.setdefault("users", {})
        users[str(uid)] = max(float(users.get(str(uid)) or 0.0), until)


def _scanner_backpressure_remaining_s(uid: str, now: float) -> float:
    with _SCANNER_STATE_LOCK:
        global_until = float(_SCANNER_RATE_LIMIT_UNTIL.get("global") or 0.0)
        user_until = float(_SCANNER_RATE_LIMIT_UNTIL.get("users", {}).get(str(uid)) or 0.0)
    return max(0.0, max(global_until, user_until) - now)


def reset_scanner_batch_state() -> None:
    """Test helper for clearing in-memory ambient batching and backpressure state."""
    with _SCANNER_STATE_LOCK:
        _SCANNER_BATCHES.clear()
        _SCANNER_RATE_LIMIT_UNTIL["global"] = 0.0
        _SCANNER_RATE_LIMIT_UNTIL["users"] = {}


def _apply_ambient_batching(
    uid: str,
    conversation_id: str,
    scanner_segments: List[dict],
    device_type: str,
    trace_id: str,
    *,
    wake_prefix_recent: Optional[bool] = None,
    now: Optional[float] = None,
) -> tuple[Optional[List[dict]], dict]:
    now = now if now is not None else time.time()
    combined_text = _combined_segment_text(scanner_segments)
    immediate_reason = scanner_immediate_reason(combined_text, wake_prefix_recent=wake_prefix_recent)
    base_metadata = {
        "batching_enabled": SCANNER_AMBIENT_BATCHING_ENABLED,
        "model": scanner_model_name(),
        "batch_target_seconds": SCANNER_AMBIENT_BATCH_SECONDS,
        "batch_target_words": SCANNER_AMBIENT_BATCH_WORDS,
        "immediate_reason": immediate_reason,
    }

    if not SCANNER_AMBIENT_BATCHING_ENABLED:
        return scanner_segments, {
            **base_metadata,
            "batch_size": len(scanner_segments),
            "batch_word_count": _word_count(combined_text),
            "flush_reason": "disabled",
            "rate_limit_status": "not_checked",
        }

    if immediate_reason:
        return scanner_segments, {
            **base_metadata,
            "batch_size": len(scanner_segments),
            "batch_word_count": _word_count(combined_text),
            "flush_reason": f"immediate_{immediate_reason}",
            "rate_limit_status": "bypassed_for_immediate",
        }

    key = _scanner_batch_key(uid, conversation_id, device_type)
    with _SCANNER_STATE_LOCK:
        batch = _SCANNER_BATCHES.get(key)
        if not batch:
            batch = _new_ambient_batch(now)
            _SCANNER_BATCHES[key] = batch
        _append_to_ambient_batch(batch, scanner_segments, now)
        flush_reason = _ambient_flush_reason(batch, now)
        batch_word_count = int(batch.get("word_count") or 0)
        batch_size = len(batch.get("segments") or [])
        batch_started_at = float(batch.get("started_at") or now)

        global_until = float(_SCANNER_RATE_LIMIT_UNTIL.get("global") or 0.0)
        user_until = float(_SCANNER_RATE_LIMIT_UNTIL.get("users", {}).get(str(uid)) or 0.0)
        backpressure_remaining_s = max(0.0, max(global_until, user_until) - now)
        if backpressure_remaining_s > 0:
            if batch_word_count >= SCANNER_AMBIENT_BATCH_MAX_WORDS:
                _SCANNER_BATCHES.pop(key, None)
                return None, {
                    **base_metadata,
                    "batch_size": batch_size,
                    "batch_word_count": batch_word_count,
                    "flush_reason": "rate_limited_drop",
                    "rate_limit_status": "active",
                    "backpressure_remaining_s": round(backpressure_remaining_s, 3),
                    "batch_age_s": round(now - batch_started_at, 3),
                }
            return None, {
                **base_metadata,
                "batch_size": batch_size,
                "batch_word_count": batch_word_count,
                "flush_reason": "rate_limited_defer",
                "rate_limit_status": "active",
                "backpressure_remaining_s": round(backpressure_remaining_s, 3),
                "batch_age_s": round(now - batch_started_at, 3),
            }

        if not flush_reason:
            return None, {
                **base_metadata,
                "batch_size": batch_size,
                "batch_word_count": batch_word_count,
                "flush_reason": "pending",
                "rate_limit_status": "not_limited",
                "batch_age_s": round(now - batch_started_at, 3),
            }

        dispatch_segments = list(batch.get("segments") or [])
        _SCANNER_BATCHES.pop(key, None)

    return dispatch_segments, {
        **base_metadata,
        "batch_size": len(dispatch_segments),
        "batch_word_count": _word_count(_combined_segment_text(dispatch_segments)),
        "flush_reason": flush_reason,
        "rate_limit_status": "not_limited",
        "batch_age_s": round(now - batch_started_at, 3),
    }


def build_scanner_context_window(
    current_segments: List[dict],
    recent_segments: Optional[List[dict]] = None,
    *,
    now: Optional[float] = None,
    window_s: float = SCANNER_CONTEXT_WINDOW_S,
):
    """
    Build a short recent context window for the scanner payload.

    recent_segments items are expected to be dicts like:
      {"observed_at": <float>, "segment": <segment-dict>}
    """
    now = now if now is not None else time.time()
    cleaned_recent = []
    recent_context_segments: List[dict] = []
    for item in recent_segments or []:
        observed_at = item.get("observed_at")
        segment = item.get("segment")
        if observed_at is None or not segment:
            continue
        if now - float(observed_at) <= window_s:
            cleaned_recent.append({"observed_at": float(observed_at), "segment": segment})
            recent_context_segments.append(segment)

    scanner_window_segments = recent_context_segments + list(current_segments or [])
    scanner_window_text = _combined_segment_text(scanner_window_segments)
    wake_prefix_recent = contains_wake_phrase(_combined_segment_text(recent_context_segments))
    updated_recent_cache = cleaned_recent + [
        {"observed_at": now, "segment": segment} for segment in current_segments or [] if segment.get("text")
    ]

    return {
        "recent_segments": recent_context_segments,
        "scanner_window_segments": scanner_window_segments,
        "scanner_window_text": scanner_window_text,
        "wake_prefix_recent": wake_prefix_recent,
        "updated_recent_cache": updated_recent_cache,
    }


def _log_trace_event(
    trace_id: str,
    uid: str,
    stage: str,
    status: str,
    metadata: Optional[dict] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """Best-effort trace write; scanner must never block transcription."""
    if not GUARDIAN_WEBHOOK_KEY:
        return
    try:
        requests.post(
            GUARDIAN_TRACE_LOG_URL,
            json={
                "trace_id": trace_id,
                "uid": uid,
                "stage": stage,
                "status": status,
                "latency_ms": latency_ms,
                "metadata": metadata or {},
            },
            headers={"X-Guardian-Key": GUARDIAN_WEBHOOK_KEY, "X-Ella-Subject-Uid": uid},
            timeout=0.25,
        )
    except Exception:
        pass


def _wake_turn_id(trace_id: str, text: str) -> str:
    digest = hashlib.sha1(f"{trace_id}:{_normalize_text(text)}".encode("utf-8")).hexdigest()[:12]
    return f"wake_{digest}"


def _build_wake_ack_payload(uid: str, conversation_id: str, trace_id: str, scanner_segments: List[dict]) -> dict | None:
    text = _combined_segment_text(scanner_segments)
    if not contains_wake_phrase(text):
        return None

    wake_turn_id = _wake_turn_id(trace_id, text)
    return {
        "uid": uid,
        "userID": uid,
        "id": f"wake_ack_{trace_id}_{wake_turn_id}",
        "url": GUARDIAN_WAKE_ACK_AUDIO_URL,
        "priority": "normal",
        "message": "wake_ack",
        "trigger": "wake_word_ack",
        "metadata": {
            "trace_id": trace_id,
            "parent_conversation_id": str(conversation_id),
            "wake_turn_id": wake_turn_id,
            "matched_pattern": "wake_phrase",
            "ack_only": True,
            "source": "omi_backend_fast_wake_ack",
            "detected_at": time.time(),
            "segments_preview": scanner_payload_preview(scanner_segments, limit=2),
        },
    }


def _enqueue_wake_ack(uid: str, conversation_id: str, trace_id: str, scanner_segments: List[dict]) -> None:
    """Fire-and-forget audible wake acknowledgement before slower scanner work."""
    payload = _build_wake_ack_payload(uid, conversation_id, trace_id, scanner_segments)
    if payload is None:
        return

    wake_turn_id = str(payload["metadata"]["wake_turn_id"])

    _log_trace_event(
        trace_id=trace_id,
        uid=uid,
        stage="wake_detected",
        status="success",
        metadata={
            "conversation_id": str(conversation_id),
            "wake_turn_id": wake_turn_id,
            "segments_preview": scanner_payload_preview(scanner_segments, limit=2),
        },
    )

    def _post() -> None:
        start = time.time()
        try:
            if not GUARDIAN_WEBHOOK_KEY:
                return
            if GUARDIAN_WAKE_ACK_DIRECT_DB:
                result = _insert_wake_ack_direct(uid, trace_id, payload)
                _log_trace_event(
                    trace_id=trace_id,
                    uid=uid,
                    stage="ack_enqueued",
                    status=result.get("status", "success"),
                    latency_ms=int((time.time() - start) * 1000),
                    metadata={
                        "wake_turn_id": wake_turn_id,
                        "queue_item_id": payload["id"],
                        **result,
                    },
                )
            else:
                response = requests.post(
                    GUARDIAN_ENQUEUE_URL,
                    json=payload,
                    headers={"X-Guardian-Key": GUARDIAN_WEBHOOK_KEY, "X-Ella-Subject-Uid": uid},
                    timeout=GUARDIAN_WAKE_ACK_TIMEOUT_S,
                )
                _log_trace_event(
                    trace_id=trace_id,
                    uid=uid,
                    stage="ack_enqueued",
                    status="success" if 200 <= response.status_code < 300 else "error",
                    latency_ms=int((time.time() - start) * 1000),
                    metadata={
                        "wake_turn_id": wake_turn_id,
                        "queue_item_id": payload["id"],
                        "method": "loopback_http",
                        "status_code": response.status_code,
                        "response": response.text[:200],
                    },
                )
        except Exception as e:
            _log_trace_event(
                trace_id=trace_id,
                uid=uid,
                stage="ack_enqueued",
                status="error",
                latency_ms=int((time.time() - start) * 1000),
                metadata={
                    "wake_turn_id": wake_turn_id,
                    "queue_item_id": payload["id"],
                    "error": str(e)[:200],
                },
            )

    threading.Thread(target=_post, name="guardian-wake-ack", daemon=True).start()


def _insert_wake_ack_direct(uid: str, trace_id: str, payload: dict) -> dict:
    """Insert wake ack without loopback HTTP so scanner timeouts cannot block the ring."""
    try:
        import psycopg2
    except Exception as exc:
        response = requests.post(
            GUARDIAN_ENQUEUE_URL,
            json=payload,
            headers={"X-Guardian-Key": GUARDIAN_WEBHOOK_KEY, "X-Ella-Subject-Uid": uid},
            timeout=max(GUARDIAN_WAKE_ACK_TIMEOUT_S, 12.0),
        )
        return {
            "method": "loopback_http_fallback",
            "status_code": response.status_code,
            "fallback_reason": f"psycopg2_unavailable:{exc}",
            "status": "success" if 200 <= response.status_code < 300 else "error",
        }

    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("trace_id", trace_id)
    metadata.setdefault("queue_item_id", payload["id"])

    conn = psycopg2.connect(
        host=ELLA_POSTGRES_HOST,
        port=ELLA_POSTGRES_PORT,
        user=ELLA_POSTGRES_USER,
        password=ELLA_POSTGRES_PASSWORD,
        dbname=ELLA_POSTGRES_DATABASE,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT guardian_mode FROM users WHERE omi_uid = %s",
                    (uid,),
                )
                row = cur.fetchone()
                mode = str(row[0] if row and row[0] is not None else "").strip().lower()
                if mode == "off":
                    return {"method": "direct_db", "status": "skipped", "reason": "guardian_mode_off"}

                cur.execute(
                    """
                    SELECT id
                    FROM guardian_queue
                    WHERE uid = %s
                      AND trigger_type = 'wake_word_ack'
                      AND metadata->>'trace_id' = %s
                      AND created_at > NOW() - INTERVAL '15 seconds'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (uid, trace_id),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    return {
                        "method": "direct_db",
                        "status": "skipped",
                        "deduped": True,
                        "duplicate_queue_item_id": duplicate[0],
                    }

                cur.execute(
                    """
                    INSERT INTO guardian_queue (id, uid, url, priority, message, trigger_type, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        payload["id"],
                        uid,
                        payload["url"],
                        payload.get("priority", "normal"),
                        payload.get("message"),
                        payload.get("trigger"),
                        json.dumps(metadata),
                    ),
                )
                return {"method": "direct_db", "status": "success", "inserted": cur.rowcount}
    finally:
        conn.close()


def send_to_scanner(
    uid: str,
    conversation_id: str,
    segments: List[dict],
    device_type: str = "omi",
    timeout: Optional[float] = None,
    recent_segments: Optional[List[dict]] = None,
    scanner_window_text: Optional[str] = None,
    wake_prefix_recent: Optional[bool] = None,
    latency_metadata: Optional[dict] = None,
) -> Optional[int]:
    """
    Send transcript segments to Ella scanner agent.

    Args:
        uid: User ID
        conversation_id: Current conversation ID for tracking
        segments: List of transcript segment dicts with text, speaker, stt_source
        device_type: Source device type (omi, ios, android)
        timeout: Request timeout in seconds (default from config)

    Returns:
        HTTP status code if successful, None if failed

    Note:
        This is fire-and-forget. Failures are logged but don't affect
        the main transcription flow.
    """
    if not ELLA_CONFIG.scanner_enabled:
        return None

    if not segments:
        return None

    timeout = timeout or ELLA_CONFIG.scanner_timeout
    trace_id = _trace_id_for(str(conversation_id))

    # Format segments for scanner
    scanner_segments = [
        {
            "speaker": s.get("speaker") or f"SPEAKER_{s.get('speaker_id', 0)}",
            "text": canonicalize_wake_phrase(s.get("text", "")),
            "stt_source": s.get("stt_provider") or s.get("stt_source") or s.get("source"),  # edge_asr, deepgram, soniox
            "is_user": s.get("is_user"),
            "person_id": s.get("person_id"),
            "speaker_id": s.get("speaker_id"),
            "speech_profile_processed": s.get("speech_profile_processed"),
        }
        for s in segments
        if s.get("text")
    ]

    if not scanner_segments:
        return None

    if should_suppress_guardian_echo(uid, scanner_segments):
        _log_trace_event(
            trace_id=trace_id,
            uid=uid,
            stage="scanner_dispatch_suppressed",
            status="skipped",
            metadata={
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                "segment_count": len(scanner_segments),
                "reason": "guardian_playback_echo",
                "segments_preview": scanner_payload_preview(scanner_segments),
            },
        )
        print(
            f"📡 Scanner suppressed guardian playback echo trace={trace_id} "
            f"preview={scanner_payload_preview(scanner_segments)}",
            flush=True,
        )
        return None

    scanner_segments, batch_metadata = _apply_ambient_batching(
        uid,
        str(conversation_id),
        scanner_segments,
        device_type,
        trace_id,
        wake_prefix_recent=wake_prefix_recent,
    )
    if not scanner_segments:
        _log_trace_event(
            trace_id=trace_id,
            uid=uid,
            stage="scanner_ambient_batch",
            status="deferred" if batch_metadata.get("flush_reason") != "rate_limited_drop" else "dropped",
            metadata={
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                **batch_metadata,
            },
        )
        print(
            f"📡 Scanner ambient batch trace={trace_id} "
            f"status={batch_metadata.get('flush_reason')} "
            f"size={batch_metadata.get('batch_size')} words={batch_metadata.get('batch_word_count')} "
            f"rate_limit={batch_metadata.get('rate_limit_status')}",
            flush=True,
        )
        return None

    _enqueue_wake_ack(uid, str(conversation_id), trace_id, scanner_segments)

    payload = {
        "uid": uid,
        "conversation_id": str(conversation_id),
        "trace_id": trace_id,
        "device_type": device_type,
        "segments": scanner_segments,
        "trace": {
            "id": trace_id,
            "schema_version": "guardian-pipeline-v1",
            "source": "omi-backend",
            "contract": "ella-ai#600",
        },
        "scanner_batch": batch_metadata,
    }
    if latency_metadata:
        payload["latency"] = latency_metadata
    if recent_segments is not None:
        payload["recent_segments"] = [
            {
                "speaker": s.get("speaker") or f"SPEAKER_{s.get('speaker_id', 0)}",
                "text": canonicalize_wake_phrase(s.get("text", "")),
                "stt_source": s.get("stt_provider") or s.get("stt_source") or s.get("source"),
                "is_user": s.get("is_user"),
                "person_id": s.get("person_id"),
                "speaker_id": s.get("speaker_id"),
                "speech_profile_processed": s.get("speech_profile_processed"),
            }
            for s in recent_segments
            if s.get("text")
        ]
    if scanner_window_text is not None:
        payload["scanner_window_text"] = canonicalize_wake_phrase(scanner_window_text)
    if wake_prefix_recent is not None:
        payload["wake_prefix_recent"] = wake_prefix_recent

    try:
        start = time.time()
        resp = requests.post(ELLA_CONFIG.scanner_url, json=payload, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        rate_limit_status = rate_limit_status_from_response(resp)
        _record_scanner_backpressure(uid, rate_limit_status, time.time())
        _log_trace_event(
            trace_id=trace_id,
            uid=uid,
            stage="scanner_dispatched",
            status="success" if 200 <= resp.status_code < 300 else "error",
            latency_ms=latency_ms,
            metadata={
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                "segment_count": len(scanner_segments),
                "scanner_status_code": resp.status_code,
                "scanner_batch": batch_metadata,
                "latency": latency_metadata or {},
                "rate_limit": rate_limit_status,
                "segments_preview": scanner_payload_preview(scanner_segments),
                "recent_segments_preview": scanner_payload_preview(payload.get("recent_segments", [])),
                "wake_prefix_recent": payload.get("wake_prefix_recent"),
            },
        )
        print(
            f"📡 Scanner: trace={trace_id} {len(scanner_segments)} segments → {resp.status_code} "
            f"batch={batch_metadata.get('flush_reason')} words={batch_metadata.get('batch_word_count')} "
            f"rate_limited={rate_limit_status.get('limited')} "
            f"payload={scanner_payload_preview(scanner_segments)} "
            f"recent={scanner_payload_preview(payload.get('recent_segments', []))} "
            f"wake_prefix_recent={payload.get('wake_prefix_recent')}",
            flush=True,
        )
        return resp.status_code

    except requests.Timeout:
        _log_trace_event(
            trace_id=trace_id,
            uid=uid,
            stage="scanner_dispatched",
            status="timeout",
            metadata={
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                "segment_count": len(scanner_segments),
                "timeout_s": timeout,
                "scanner_batch": batch_metadata,
                "latency": latency_metadata or {},
                "segments_preview": scanner_payload_preview(scanner_segments),
                "recent_segments_preview": scanner_payload_preview(payload.get("recent_segments", [])),
                "wake_prefix_recent": payload.get("wake_prefix_recent"),
            },
        )
        print(f"📡 Scanner timeout trace={trace_id} ({timeout}s)", flush=True)
        return None

    except Exception as e:
        _log_trace_event(
            trace_id=trace_id,
            uid=uid,
            stage="scanner_dispatched",
            status="error",
            metadata={
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                "segment_count": len(scanner_segments),
                "error": str(e)[:200],
                "scanner_batch": batch_metadata,
                "latency": latency_metadata or {},
                "segments_preview": scanner_payload_preview(scanner_segments),
                "recent_segments_preview": scanner_payload_preview(payload.get("recent_segments", [])),
                "wake_prefix_recent": payload.get("wake_prefix_recent"),
            },
        )
        print(f"📡 Scanner error trace={trace_id}: {e}", flush=True)
        return None
