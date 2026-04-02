"""
Unit tests for guardian router — Layer 2 (mode switching in enqueue)
and Layer 3 (set-mode endpoint).

Tests use mocked DB pool and HTTP clients — no real DB or network calls.
"""

import os

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("ELLA_POSTGRES_PASSWORD", "test")
os.environ.setdefault("GUARDIAN_WEBHOOK_KEY", "test-guardian-key")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ella.routers.guardian import (
    VALID_MODES,
    _MODE_SWITCH_TRIGGERS,
    _TIER1_MODES,
    _TIER2_MODES,
    _update_guardian_override,
    EnqueueRequest,
    SetModeRequest,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_valid_modes_includes_tier1():
    assert "CYBORG" in VALID_MODES
    assert "DEMO" in VALID_MODES
    assert "CHATBOT" in VALID_MODES


def test_valid_modes_includes_tier2():
    assert "ACTIVE_SUPPORT" in VALID_MODES
    assert "EMERGENCY_ONLY" in VALID_MODES
    assert "MAXIMUM_AWARENESS" in VALID_MODES
    assert "MEMORY_SUPPORT" in VALID_MODES


def test_tier1_and_tier2_are_disjoint():
    assert _TIER1_MODES.isdisjoint(_TIER2_MODES)


def test_mode_switch_triggers_set():
    assert "set_guardian_override" in _MODE_SWITCH_TRIGGERS
    assert "clear_guardian_override" in _MODE_SWITCH_TRIGGERS
    assert "guardian-mode-switch" in _MODE_SWITCH_TRIGGERS


# ---------------------------------------------------------------------------
# EnqueueRequest model
# ---------------------------------------------------------------------------


def test_enqueue_request_accepts_mode_field():
    req = EnqueueRequest(uid="user123", url="https://example.com/a.mp3", mode="CYBORG")
    assert req.mode == "CYBORG"


def test_enqueue_request_mode_defaults_none():
    req = EnqueueRequest(uid="user123", url="https://example.com/a.mp3")
    assert req.mode is None


def test_enqueue_request_accepts_userid_alias():
    req = EnqueueRequest(userID="user123", url="https://example.com/a.mp3")
    assert req.userID == "user123"
    assert req.uid is None


# ---------------------------------------------------------------------------
# SetModeRequest model
# ---------------------------------------------------------------------------


def test_set_mode_request_requires_uid():
    req = SetModeRequest(uid="user123", mode="CYBORG")
    assert req.uid == "user123"
    assert req.mode == "CYBORG"


def test_set_mode_request_mode_optional():
    req = SetModeRequest(uid="user123")
    assert req.mode is None


# ---------------------------------------------------------------------------
# _update_guardian_override helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_guardian_override_sets_mode():
    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": None, "guardian_mode": "ACTIVE_SUPPORT"}
    mock_pool.execute = AsyncMock()

    with patch("ella.routers.guardian._get_pool", return_value=mock_pool):
        result = await _update_guardian_override("user123", "CYBORG")

    assert result["updated"] is True
    assert result["previous_mode"] is None
    assert result["new_mode"] == "CYBORG"
    mock_pool.execute.assert_called_once()


@pytest.mark.asyncio
async def test_update_guardian_override_clears_mode():
    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": "DEMO", "guardian_mode": "DEMO"}
    mock_pool.execute = AsyncMock()

    with patch("ella.routers.guardian._get_pool", return_value=mock_pool):
        result = await _update_guardian_override("user123", None)

    assert result["updated"] is True
    assert result["previous_mode"] == "DEMO"
    assert result["new_mode"] is None


@pytest.mark.asyncio
async def test_update_guardian_override_normalizes_normal_to_none():
    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": "CYBORG", "guardian_mode": "CYBORG"}
    mock_pool.execute = AsyncMock()

    with patch("ella.routers.guardian._get_pool", return_value=mock_pool):
        result = await _update_guardian_override("user123", "NORMAL")

    assert result["new_mode"] is None


@pytest.mark.asyncio
async def test_update_guardian_override_user_not_found():
    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = None

    with patch("ella.routers.guardian._get_pool", return_value=mock_pool):
        result = await _update_guardian_override("unknown-uid", "CYBORG")

    assert result["updated"] is False


# ---------------------------------------------------------------------------
# Enqueue endpoint — mode-switch detection
# ---------------------------------------------------------------------------


_AUTH = {"X-Guardian-Key": "test-key"}
_KEY_PATCH = patch("ella.routers.guardian.GUARDIAN_WEBHOOK_KEY", "test-key")


@pytest.mark.asyncio
async def test_enqueue_detects_mode_field():
    """req.mode="CYBORG" should trigger _update_guardian_override."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": None, "guardian_mode": None}
    mock_pool.execute = AsyncMock()
    mock_pool.fetchval = AsyncMock(return_value=1)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/enqueue",
            json={"uid": "user123", "url": "https://example.com/a.mp3", "mode": "CYBORG"},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["mode_updated"] is True


@pytest.mark.asyncio
async def test_enqueue_detects_clear_guardian_override_trigger():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": "DEMO", "guardian_mode": "DEMO"}
    mock_pool.execute = AsyncMock()
    mock_pool.fetchval = AsyncMock(return_value=0)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/enqueue",
            json={"uid": "user123", "url": "https://example.com/a.mp3", "trigger": "clear_guardian_override"},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    assert resp.json()["mode_updated"] is True


@pytest.mark.asyncio
async def test_enqueue_no_mode_does_not_update_db():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()
    mock_pool.fetchval = AsyncMock(return_value=1)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/enqueue",
            json={"uid": "user123", "url": "https://example.com/a.mp3"},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    assert resp.json()["mode_updated"] is False
    # pool.execute called once (INSERT into guardian_queue), not for user update
    assert mock_pool.execute.call_count == 1


@pytest.mark.asyncio
async def test_enqueue_invalid_mode_returns_400():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock()):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/enqueue",
            json={"uid": "user123", "url": "https://example.com/a.mp3", "mode": "INVALID_MODE"},
            headers=_AUTH,
        )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# set-mode endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_mode_updates_guardian_override():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": None, "guardian_mode": None}
    mock_pool.execute = AsyncMock()

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/set-mode",
            json={"uid": "user123", "mode": "DEMO"},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["new_mode"] == "DEMO"
    assert data["uid"] == "user123"


@pytest.mark.asyncio
async def test_set_mode_clears_override_with_normal():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = {"guardian_override": "CYBORG", "guardian_mode": "CYBORG"}
    mock_pool.execute = AsyncMock()

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/set-mode",
            json={"uid": "user123", "mode": "NORMAL"},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    assert resp.json()["new_mode"] is None
    assert resp.json()["previous_mode"] == "CYBORG"


@pytest.mark.asyncio
async def test_set_mode_invalid_mode_returns_400():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock()):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/set-mode",
            json={"uid": "user123", "mode": "BOGUS"},
            headers=_AUTH,
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_mode_user_not_found_returns_404():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    mock_pool = AsyncMock()
    mock_pool.fetchrow.return_value = None

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock(return_value=mock_pool)):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/set-mode",
            json={"uid": "nonexistent-uid", "mode": "DEMO"},
            headers=_AUTH,
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_mode_requires_auth_key():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from ella.routers.guardian import router

    app = FastAPI()
    app.include_router(router)

    with _KEY_PATCH, patch("ella.routers.guardian._get_pool", new=AsyncMock()):
        client = TestClient(app)
        resp = client.post(
            "/v1/ella/guardian/set-mode",
            json={"uid": "user123", "mode": "DEMO"},
            # No auth header — should be 403
        )

    assert resp.status_code == 403
