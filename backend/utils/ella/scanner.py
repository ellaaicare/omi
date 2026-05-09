# Ella Scanner Integration
#
# Sends real-time transcript segments to n8n scanner for urgency detection.
# Fire-and-forget with short timeout - doesn't block transcription flow.

import os
import re
import requests
import time
from typing import List, Optional

from .config import ELLA_CONFIG

GUARDIAN_TRACE_LOG_URL = os.getenv(
    "ELLA_GUARDIAN_TRACE_LOG_URL",
    "http://127.0.0.1:8000/v1/ella/guardian/trace/log",
)
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
    "i heard you. i am checking that now",
)


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
    text = " ".join(str(segment.get("text") or "") for segment in scanner_segments).strip()
    if not _looks_like_guardian_echo_text(text):
        return False

    event = playback_event if playback_event is not None else _recent_risky_playback_event(uid)
    if not event:
        return False
    return event.get("echo_risk") in _ECHO_RISKY_OUTPUTS


def _trace_id_for(conversation_id: str) -> str:
    """Use the conversation id as the cross-service guardian trace id."""
    trace_id = str(conversation_id or "").strip()
    if trace_id and trace_id.lower() != "unknown":
        return trace_id
    return f"scanner-{int(time.time() * 1000)}"


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
    uid: str, conversation_id: str, segments: List[dict], device_type: str = "omi", timeout: Optional[float] = None
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
            "text": s.get("text", ""),
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
            },
        )
        preview = " ".join(segment["text"] for segment in scanner_segments)[:120]
        print(
            f"📡 Scanner suppressed guardian playback echo trace={trace_id} preview={preview!r}",
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
            },
        )
        print(f"📡 Scanner: trace={trace_id} {len(scanner_segments)} segments → {resp.status_code}", flush=True)
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
            },
        )
        print(f"📡 Scanner error trace={trace_id}: {e}", flush=True)
        return None
