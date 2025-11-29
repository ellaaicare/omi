# Ella Memory Agent Integration
#
# Calls n8n memory agent to extract memories from conversations.
# Memories are facts, preferences, and insights about the user.

import requests
from typing import Optional, Tuple, List
from datetime import datetime

from .config import ELLA_CONFIG


def call_memory_agent(
    uid: str,
    conversation_id: str,
    transcript: str,
    timeout: Optional[float] = None
) -> Tuple[bool, Optional[List[dict]], Optional[str]]:
    """
    Call Ella memory agent to extract memories from conversation.

    Args:
        uid: User ID
        conversation_id: Conversation ID (for deduplication)
        transcript: Full transcript text
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success, memories_list, error_message)
        - success: True if call succeeded
        - memories_list: List of memory dicts if successful
        - error_message: Error description if failed
    """
    timeout = timeout or ELLA_CONFIG.memory_timeout

    payload = {
        "uid": uid,
        "conversation_id": conversation_id,
        "transcript": transcript,
    }

    try:
        print(f"📤 Calling Ella memory agent for uid={uid}, conv={conversation_id[:8]}...", flush=True)

        resp = requests.post(
            ELLA_CONFIG.memory_url,
            json=payload,
            timeout=timeout
        )

        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}: {resp.text[:100]}"
            print(f"⚠️ Ella memory agent error: {error}", flush=True)
            return False, None, error

        result = resp.json()

        # Check for async mode response
        if result.get("status") == "processing":
            print(f"⏳ Ella memory agent processing async (conv={conversation_id[:8]})", flush=True)
            return True, None, None  # Async - will receive callback

        # Extract memories from response
        memories = result.get("memories", [])

        if memories:
            print(f"✅ Ella extracted {len(memories)} memories", flush=True)
            return True, memories, None

        # No memories extracted (valid response)
        print(f"ℹ️ Ella memory agent: no memories extracted", flush=True)
        return True, [], None

    except requests.Timeout:
        error = f"Timeout after {timeout}s"
        print(f"⚠️ Ella memory agent timeout: {error}", flush=True)
        return False, None, error

    except requests.RequestException as e:
        error = str(e)
        print(f"⚠️ Ella memory agent request error: {error}", flush=True)
        return False, None, error

    except Exception as e:
        error = str(e)
        print(f"⚠️ Ella memory agent error: {error}", flush=True)
        return False, None, error


def parse_memory(memory_dict: dict) -> dict:
    """
    Parse and validate a single memory from Ella response.

    Args:
        memory_dict: Raw memory dict from n8n

    Returns:
        Normalized memory dict with expected fields
    """
    return {
        "content": memory_dict.get("content", ""),
        "category": memory_dict.get("category", "interesting"),
        "visibility": memory_dict.get("visibility", "private"),
        "tags": memory_dict.get("tags", []),
        "confidence": memory_dict.get("confidence", 1.0),
    }
