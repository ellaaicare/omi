import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

sys.modules.setdefault("asyncpg", MagicMock())

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_module_path = _backend_path / "ella" / "routers" / "voice.py"
_spec = importlib.util.spec_from_file_location("ella_voice_test_module", _module_path)
voice = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(voice)


def test_openclaw_direct_provider_uses_v4_voice_proxy_path():
    provider = voice.V2V_PROVIDERS["openclaw-direct"]

    assert provider["name"] == "OpenClaw Direct"
    assert provider["default_mode"] == "v4"
    assert provider["endpoint_env"] == "ELLA_VOICE_ENDPOINT"


def test_openclaw_direct_is_rejected_from_tts_flow_with_session_redirect():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            voice.synthesize_speech(
                voice.TtsRequest(text="hello"),
                x_tts_provider="openclaw-direct",
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["provider"] == "openclaw-direct"
    assert exc_info.value.detail["use_endpoint"] == "/v1/voice/session"
