import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("httpx", MagicMock())

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

resolve_module = types.ModuleType("ella.routers.resolve")
resolve_module.resolve_user_routing = MagicMock()
sys.modules.setdefault("ella.routers.resolve", resolve_module)

_guardian_path = _backend_path / "ella" / "routers" / "guardian.py"
_guardian_spec = importlib.util.spec_from_file_location("ella_guardian_test_module", _guardian_path)
guardian = importlib.util.module_from_spec(_guardian_spec)
assert _guardian_spec is not None and _guardian_spec.loader is not None
_guardian_spec.loader.exec_module(guardian)


def test_promotes_scanner_escalation_debug_event_in_cyborg_mode():
    req = guardian.EnqueueRequest(
        uid="uid-123",
        url="",
        priority="debug",
        trigger="scanner-l3-escalation",
        message="Scanner escalated: question -> agent",
        metadata={"summary": "A direct question was detected.", "category": "question"},
    )

    promoted = guardian._promote_cyborg_debug_event(req, "CYBORG")

    assert promoted is True
    assert req.priority == "normal"
    assert req.trigger == "cyborg-scanner-l3-escalation"
    assert req.message == "I noticed: A direct question was detected."
    assert req.metadata["original_priority"] == "debug"
    assert req.metadata["original_trigger"] == "scanner-l3-escalation"
    assert req.metadata["cyborg_promoted"] is True


def test_promotes_scanner_clear_debug_event_in_cyborg_mode():
    req = guardian.EnqueueRequest(
        uid="uid-123",
        url="",
        priority="debug",
        trigger="scanner-l3-clear",
        message="Scanner: clear - none",
        metadata={"summary": "none", "category": "none"},
    )

    promoted = guardian._promote_cyborg_debug_event(req, "CYBORG")

    assert promoted is True
    assert req.priority == "normal"
    assert req.trigger == "cyborg-scanner-l3-clear"
    assert req.message == "I heard that. No action needed."


def test_keeps_scanner_debug_event_silent_outside_cyborg_mode():
    req = guardian.EnqueueRequest(
        uid="uid-123",
        url="",
        priority="debug",
        trigger="scanner-l3-escalation",
        message="Scanner escalated: question -> agent",
        metadata={"summary": "A direct question was detected.", "category": "question"},
    )

    promoted = guardian._promote_cyborg_debug_event(req, "ACTIVE_SUPPORT")

    assert promoted is False
    assert req.priority == "debug"
    assert req.trigger == "scanner-l3-escalation"
    assert req.message == "Scanner escalated: question -> agent"
