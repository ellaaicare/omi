# Ella Summary Agent Integration
#
# Calls n8n summary agent to generate conversation summaries.
# Supports both sync (wait for response) and async (callback) modes.

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

    payload = {
        "uid": uid,
        "conversation_id": conversation_id,
        "transcript": transcript,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
    }

    try:
        print(f"📤 Calling Ella summary agent for uid={uid}, conv={conversation_id[:8]}...", flush=True)

        resp = requests.post(
            ELLA_CONFIG.summary_url,
            json=payload,
            timeout=timeout
        )

        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}: {resp.text[:100]}"
            print(f"⚠️ Ella summary agent error: {error}", flush=True)
            return False, None, error

        result = resp.json()

        # Check for async mode response
        if result.get("status") == "processing":
            print(f"⏳ Ella summary agent processing async (conv={conversation_id[:8]})", flush=True)
            return True, None, None  # Async - will receive callback

        # Sync response with summary
        if result.get("title"):
            print(f"✅ Ella summary: {result.get('title', 'No title')}", flush=True)
            return True, result, None

        # Empty or unexpected response
        print(f"⚠️ Ella summary agent returned unexpected: {result}", flush=True)
        return False, None, "Unexpected response format"

    except requests.Timeout:
        error = f"Timeout after {timeout}s"
        print(f"⚠️ Ella summary agent timeout: {error}", flush=True)
        return False, None, error

    except requests.RequestException as e:
        error = str(e)
        print(f"⚠️ Ella summary agent request error: {error}", flush=True)
        return False, None, error

    except Exception as e:
        error = str(e)
        print(f"⚠️ Ella summary agent error: {error}", flush=True)
        return False, None, error


def parse_summary_response(response: dict) -> dict:
    """
    Parse and validate summary response from Ella.

    Args:
        response: Raw response dict from n8n

    Returns:
        Normalized summary dict with expected fields
    """
    return {
        "title": response.get("title", "Untitled Conversation"),
        "overview": response.get("overview", ""),
        "emoji": response.get("emoji", "💬"),
        "category": response.get("category", "other"),
        "action_items": response.get("action_items", []),
        "events": response.get("events", []),
    }
