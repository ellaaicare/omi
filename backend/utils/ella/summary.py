# Ella Summary Agent Integration
#
# Calls n8n summary agent to generate conversation summaries.
# Supports both sync (wait for response) and async (callback) modes.

import time
import requests
from typing import Optional, Tuple, Any
from datetime import datetime

from .config import ELLA_CONFIG


def call_summary_agent(
    uid: str,
    conversation_id: str,
    transcript: str,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    timeout: Optional[float] = None
) -> Tuple[bool, Optional[dict], Optional[str]]:
    """
    Call Ella summary agent to generate conversation summary.

    Args:
        uid: User ID
        conversation_id: Conversation ID
        transcript: Full transcript text
        started_at: Conversation start time
        finished_at: Conversation end time
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success, result_dict, error_message)
        - success: True if call succeeded
        - result_dict: Summary data if sync response, None if async
        - error_message: Error description if failed

    Modes:
        - Sync: n8n returns summary immediately in response
        - Async: n8n returns {"status": "processing"}, will callback later
    """
    timeout = timeout or ELLA_CONFIG.summary_timeout
    _start = time.time()
    conv_short = conversation_id[:8] if conversation_id else "unknown"

    payload = {
        "uid": uid,
        "conversation_id": conversation_id,
        "transcript": transcript,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
    }

    try:
        print(f"[FLOW:SUMMARY] calling n8n uid={uid} conv={conv_short} url={ELLA_CONFIG.summary_url} timeout={timeout}s transcript_len={len(transcript) if transcript else 0}", flush=True)

        resp = requests.post(
            ELLA_CONFIG.summary_url,
            json=payload,
            timeout=timeout
        )

        _elapsed = int((time.time() - _start) * 1000)

        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}: {resp.text[:100]}"
            print(f"[FLOW:SUMMARY] ERROR n8n status={resp.status_code} uid={uid} conv={conv_short} latency={_elapsed}ms", flush=True)
            return False, None, error

        result = resp.json()

        # Check for async mode response
        if result.get("status") == "processing":
            print(f"[FLOW:SUMMARY] ASYNC n8n processing uid={uid} conv={conv_short} latency={_elapsed}ms", flush=True)
            return True, None, None  # Async - will receive callback

        # Sync response with summary — accept both flat and nested formats
        summary_data = result
        response_format = "flat"
        if not result.get("title") and isinstance(result.get("summary"), dict):
            summary_data = result["summary"]
            response_format = "nested"

        if summary_data.get("title"):
            title = summary_data.get('title', 'No title')
            print(f"[FLOW:SUMMARY] OK n8n uid={uid} conv={conv_short} format={response_format} title={title} latency={_elapsed}ms", flush=True)
            return True, summary_data, None

        # Empty or unexpected response
        print(f"[FLOW:SUMMARY] UNEXPECTED n8n uid={uid} conv={conv_short} response_keys={list(result.keys())} latency={_elapsed}ms", flush=True)
        return False, None, "Unexpected response format"

    except requests.Timeout:
        _elapsed = int((time.time() - _start) * 1000)
        error = f"Timeout after {timeout}s"
        print(f"[FLOW:SUMMARY] TIMEOUT n8n uid={uid} conv={conv_short} timeout={timeout}s latency={_elapsed}ms", flush=True)
        return False, None, error

    except requests.RequestException as e:
        _elapsed = int((time.time() - _start) * 1000)
        error = str(e)
        print(f"[FLOW:SUMMARY] ERROR n8n uid={uid} conv={conv_short} error={error} latency={_elapsed}ms", flush=True)
        return False, None, error

    except Exception as e:
        _elapsed = int((time.time() - _start) * 1000)
        error = str(e)
        print(f"[FLOW:SUMMARY] UNEXPECTED uid={uid} conv={conv_short} error={error} latency={_elapsed}ms", flush=True)
        return False, None, error


def parse_summary_response(response: dict) -> dict:
    """
    Parse and validate summary response from Ella.

    Args:
        response: Raw response dict from n8n

    Returns:
        Normalized summary dict with expected fields
    """
    # Handle both flat and nested response formats
    data = response
    if not response.get("title") and isinstance(response.get("summary"), dict):
        data = response["summary"]

    return {
        "title": data.get("title", "Untitled Conversation"),
        "overview": data.get("overview", ""),
        "emoji": data.get("emoji", "\U0001f4ac"),
        "category": data.get("category", "other"),
        "action_items": data.get("action_items", []),
        "events": data.get("events", []),
    }
