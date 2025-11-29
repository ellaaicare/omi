# Ella Scanner Integration
#
# Sends real-time transcript segments to n8n scanner for urgency detection.
# Fire-and-forget with short timeout - doesn't block transcription flow.

import requests
from typing import List, Optional

from .config import ELLA_CONFIG


def send_to_scanner(
    uid: str,
    conversation_id: str,
    segments: List[dict],
    device_type: str = "omi",
    timeout: Optional[float] = None
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

    try:
        resp = requests.post(
            ELLA_CONFIG.scanner_url,
            json={
                "uid": uid,
                "conversation_id": str(conversation_id),
                "device_type": device_type,
                "segments": scanner_segments,
            },
            timeout=timeout
        )
        print(f"📡 Scanner: {len(scanner_segments)} segments → {resp.status_code}", flush=True)
        return resp.status_code

    except requests.Timeout:
        print(f"📡 Scanner timeout ({timeout}s)", flush=True)
        return None

    except Exception as e:
        print(f"📡 Scanner error: {e}", flush=True)
        return None
