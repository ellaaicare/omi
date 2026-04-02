"""
Conftest for guardian consolidation unit tests.

Mocks heavy OMI backend dependencies so we can import guardian.py
without needing a full database setup.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Mock all heavy OMI modules before any test imports ella.routers.guardian
# ---------------------------------------------------------------------------

# Database modules
sys.modules.setdefault("database", MagicMock())
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("database.redis_db", MagicMock())
sys.modules.setdefault("database.cache", MagicMock())
sys.modules.setdefault("database.auth", MagicMock())

# Sibling routers (imported by ella.routers.__init__)
callbacks_mock = MagicMock()
callbacks_mock.router = MagicMock()
sys.modules.setdefault("ella.routers.callbacks", callbacks_mock)

chat_mock = MagicMock()
chat_mock.router = MagicMock()
sys.modules.setdefault("ella.routers.chat", chat_mock)

voice_mock = MagicMock()
voice_mock.router = MagicMock()
sys.modules.setdefault("ella.routers.voice", voice_mock)

# Prevent ella.routers.__init__ from re-importing everything
import importlib
import ella.routers  # noqa: E402 — must come AFTER sys.modules patches

ella.routers.callbacks_router = callbacks_mock.router
ella.routers.chat_router = chat_mock.router
ella.routers.voice_router = voice_mock.router
