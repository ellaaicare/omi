# Ella Scanner Integration
#
# Sends real-time transcript segments to n8n scanner for urgency detection.
# Fire-and-forget with short timeout - doesn't block transcription flow.

import os
import re
import time
from typing import List, Optional

import requests

from .config import ELLA_CONFIG

GUARDIAN_TRACE_LOG_URL = os.getenv(
    "ELLA_GUARDIAN_TRACE_LOG_URL",
    "http://127.0.0.1:8000/v1/ella/guardian/trace/log",
)
WAKE_WORD_PREFIX_MAX_WORDS = 4
WAKE_WORD_PENDING_WINDOW_S = float(os.getenv("ELLA_WAKE_WORD_PENDING_WINDOW_S", "12.0"))
SCANNER_CONTEXT_WINDOW_S = float(os.getenv("ELLA_SCANNER_CONTEXT_WINDOW_S", "12.0"))
GUARDIAN_ECHO_SUPPRESSION_SECONDS = int(os.getenv("ELLA_GUARDIAN_ECHO_SUPPRESSION_SECONDS", "45"))
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


def _trace_id_for(conversation_id: str) -> str:
    """Use the conversation id as the cross-service guardian trace id."""
    trace_id = str(conversation_id or "").strip()
    if trace_id and trace_id.lower() != "unknown":
        return trace_id
    return f"scanner-{int(time.time() * 1000)}"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()


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
    Hold very short wake-word-only batches briefly and prepend them to the next batch.

    This preserves "Hey Ella" / "Ela" prefixes when diarization or STT flushes the
    prefix separately from the actual question.
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
            [],
            list(current_segments),
            now,
            {
                "action": "hold_wake_prefix",
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
            timeout=0.25,
        )
    except Exception:
        pass


def send_to_scanner(
    uid: str,
    conversation_id: str,
    segments: List[dict],
    device_type: str = "omi",
    timeout: Optional[float] = None,
    recent_segments: Optional[List[dict]] = None,
    scanner_window_text: Optional[str] = None,
    wake_prefix_recent: Optional[bool] = None,
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
            "stt_source": s.get("source"),  # edge_asr, deepgram, soniox
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
    }
    if recent_segments is not None:
        payload["recent_segments"] = [
            {
                "speaker": s.get("speaker") or f"SPEAKER_{s.get('speaker_id', 0)}",
                "text": canonicalize_wake_phrase(s.get("text", "")),
                "stt_source": s.get("source"),
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
                "segments_preview": scanner_payload_preview(scanner_segments),
                "recent_segments_preview": scanner_payload_preview(payload.get("recent_segments", [])),
                "wake_prefix_recent": payload.get("wake_prefix_recent"),
            },
        )
        print(
            f"📡 Scanner: trace={trace_id} {len(scanner_segments)} segments → {resp.status_code} "
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
                "segments_preview": scanner_payload_preview(scanner_segments),
                "recent_segments_preview": scanner_payload_preview(payload.get("recent_segments", [])),
                "wake_prefix_recent": payload.get("wake_prefix_recent"),
            },
        )
        print(f"📡 Scanner error trace={trace_id}: {e}", flush=True)
        return None
