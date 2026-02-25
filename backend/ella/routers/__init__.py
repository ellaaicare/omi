"""
Ella Routers - Ella-specific API endpoints.

Routers:
- callbacks: Receives callbacks from Ella n8n/Letta agents
- chat: Streaming chat via Grok (xAI)
- voice: Voice session management (token issuance)
- guardian: Guardian Mode audio delivery queue for iOS

These add endpoints that don't exist in vanilla OMI:
- /v1/ella/* - Callback endpoints for Letta agents
- /v1/ella/chat/* - Grok streaming chat
- /v1/ella/guardian/* - Guardian Mode audio queue
- /v1/voice/* - Voice session management
"""

from .callbacks import router as callbacks_router
from .chat import router as chat_router
from .guardian import router as guardian_router
from .voice import router as voice_router

__all__ = [
    "callbacks_router",
    "chat_router",
    "guardian_router",
    "voice_router",
]
