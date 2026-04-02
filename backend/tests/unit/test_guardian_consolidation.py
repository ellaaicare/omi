"""
Unit tests for guardian queue consolidation logic.

Tests _consolidate_queue, get_playback_event, and echo risk mapping
using mocked HTTP clients (no real DB or LLM calls).
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to patch module-level state without importing the full FastAPI app
# ---------------------------------------------------------------------------

# guardian.py top-level reads env vars at import time — set them first
import os

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("ELLA_POSTGRES_PASSWORD", "test")

from ella.routers.guardian import (  # noqa: E402
    _ECHO_RISK,
    _consolidate_queue,
    _get_recent_chat_turns,
    get_playback_event,
    _playback_events,
)

# ---------------------------------------------------------------------------
# Echo risk mapping tests
# ---------------------------------------------------------------------------


def test_echo_risk_speaker_is_high():
    assert _ECHO_RISK["Speaker"] == "high"


def test_echo_risk_headphones_is_none():
    assert _ECHO_RISK["Headphones"] == "none"


def test_echo_risk_airplay_is_very_high():
    assert _ECHO_RISK["AirPlay"] == "very_high"


def test_echo_risk_bluetooth_hfp_is_none():
    assert _ECHO_RISK["BluetoothHFP"] == "none"


# ---------------------------------------------------------------------------
# get_playback_event TTL tests
# ---------------------------------------------------------------------------


def test_get_playback_event_returns_none_when_missing():
    _playback_events.clear()
    assert get_playback_event("no-such-uid") is None


def test_get_playback_event_returns_event_within_ttl():
    _playback_events["test-uid"] = {
        "port_type": "Speaker",
        "port_name": "iPhone Speaker",
        "device_uid": "",
        "echo_risk": "high",
        "recorded_at": time.time(),
    }
    event = get_playback_event("test-uid")
    assert event is not None
    assert event["echo_risk"] == "high"


def test_get_playback_event_returns_none_when_stale():
    _playback_events["stale-uid"] = {
        "port_type": "Speaker",
        "port_name": "iPhone Speaker",
        "device_uid": "",
        "echo_risk": "high",
        "recorded_at": time.time() - 61,  # 61 seconds ago — past 60s TTL
    }
    assert get_playback_event("stale-uid") is None


# ---------------------------------------------------------------------------
# _consolidate_queue LLM call tests
# ---------------------------------------------------------------------------

PENDING_ITEMS = [
    {
        "id": "item-1",
        "url": "https://example.com/a.mp3",
        "priority": "normal",
        "message": "Mom seems tired",
        "trigger_type": "cyborg-response",
        "created_at": "2026-04-02T10:00:00",
    },
    {
        "id": "item-2",
        "url": "https://example.com/b.mp3",
        "priority": "normal",
        "message": "Mom asked for water",
        "trigger_type": "scanner-escalation",
        "created_at": "2026-04-02T10:01:00",
    },
    {
        "id": "item-3",
        "url": "https://example.com/c.mp3",
        "priority": "normal",
        "message": "Mom is watching TV",
        "trigger_type": "cron-reminder",
        "created_at": "2026-04-02T10:02:00",
    },
]


def _make_llm_response(text: str) -> MagicMock:
    """Build a mock httpx response that returns the given LLM text."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return mock_resp


@pytest.mark.asyncio
async def test_consolidate_queue_returns_merged_message():
    """Happy path: LLM merges 3 items into one message."""
    merged = "Mom is watching TV and asked for water. She seems tired."

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=_make_llm_response(merged))

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        result = await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
            echo_risk="unknown",
        )

    assert result == merged


@pytest.mark.asyncio
async def test_consolidate_queue_returns_none_on_llm_null():
    """LLM says NULL → returns None (nothing to say)."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=_make_llm_response("NULL"))

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        result = await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
        )

    assert result is None


@pytest.mark.asyncio
async def test_consolidate_queue_null_case_insensitive():
    """LLM returns 'null' (lower case) → also treated as None."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=_make_llm_response("null"))

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        result = await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
        )

    assert result is None


@pytest.mark.asyncio
async def test_consolidate_queue_fallback_on_llm_http_error():
    """LLM returns non-200 → falls back to first item's message."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        result = await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
        )

    # Should fall back to first pending item's message
    assert result == PENDING_ITEMS[0]["message"]


@pytest.mark.asyncio
async def test_consolidate_queue_fallback_on_network_exception():
    """Network exception → falls back to first item's message (no crash)."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        result = await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
        )

    assert result == PENDING_ITEMS[0]["message"]


@pytest.mark.asyncio
async def test_consolidate_queue_includes_echo_instruction_for_speaker():
    """When echo_risk=high, the prompt should include echo warning."""
    captured_prompts = []

    async def capture_post(url, **kwargs):
        captured_prompts.append(kwargs.get("json", {}))
        return _make_llm_response("Mom needs water.")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=capture_post)

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[{"message": "Mom asked for water", "trigger_type": "scanner-escalation"}],
            chat_turns=[],
            echo_risk="high",
        )

    assert captured_prompts, "No LLM call was made"
    user_content = captured_prompts[0]["messages"][1]["content"]
    assert "echo_risk=high" in user_content


@pytest.mark.asyncio
async def test_consolidate_queue_no_echo_instruction_for_headphones():
    """When echo_risk=none, the echo instruction is omitted."""
    captured_prompts = []

    async def capture_post(url, **kwargs):
        captured_prompts.append(kwargs.get("json", {}))
        return _make_llm_response("Mom needs water.")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=capture_post)

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=[],
            echo_risk="none",
        )

    user_content = captured_prompts[0]["messages"][1]["content"]
    assert "echo_risk" not in user_content


@pytest.mark.asyncio
async def test_consolidate_queue_includes_chat_context():
    """Chat turns are included in the LLM prompt."""
    captured_prompts = []

    async def capture_post(url, **kwargs):
        captured_prompts.append(kwargs.get("json", {}))
        return _make_llm_response("Mom needs water.")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=capture_post)

    chat = [
        {"role": "user", "content": "How is she doing?"},
        {"role": "assistant", "content": "She seems comfortable right now."},
    ]

    with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
        await _consolidate_queue(
            uid="test-uid",
            pending=PENDING_ITEMS,
            recently_consumed=[],
            chat_turns=chat,
        )

    user_content = captured_prompts[0]["messages"][1]["content"]
    assert "How is she doing?" in user_content


# ---------------------------------------------------------------------------
# _get_recent_chat_turns tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_chat_turns_returns_empty_on_no_routing():
    """If resolve_user_routing returns empty, return []."""
    with patch("ella.routers.guardian.resolve_user_routing", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = {}
        result = await _get_recent_chat_turns("test-uid")

    assert result == []


@pytest.mark.asyncio
async def test_get_recent_chat_turns_returns_empty_on_resolve_error():
    """If resolve raises, return [] without crashing."""
    with patch("ella.routers.guardian.resolve_user_routing", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.side_effect = Exception("Provision API down")
        result = await _get_recent_chat_turns("test-uid")

    assert result == []


@pytest.mark.asyncio
async def test_get_recent_chat_turns_returns_messages():
    """Happy path: provision API returns chat history."""
    with patch("ella.routers.guardian.resolve_user_routing", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = {"routing": {"agentId": "ella-abc123"}}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "messages": [
                {"role": "user", "content": "How are you?"},
                {"role": "assistant", "content": "I'm doing well, thank you."},
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
            result = await _get_recent_chat_turns("test-uid", limit=5)

    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "How are you?"


@pytest.mark.asyncio
async def test_get_recent_chat_turns_strips_ella_prefix():
    """agentId starting with 'ella-' has the prefix stripped for the API call."""
    with patch("ella.routers.guardian.resolve_user_routing", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = {"routing": {"agentId": "ella-xyz789"}}

        captured_urls = []

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"messages": []}

        async def capture_get(url, **kwargs):
            captured_urls.append(url)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=capture_get)

        with patch("ella.routers.guardian.httpx.AsyncClient", return_value=mock_client):
            await _get_recent_chat_turns("test-uid")

    assert captured_urls, "No HTTP call was made"
    assert "xyz789" in captured_urls[0]
    assert "ella-xyz789" not in captured_urls[0]
