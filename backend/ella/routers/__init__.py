"""
Ella Routers - Ella-specific API endpoints.

Routers:
- callbacks: Receives callbacks from Ella n8n/Letta agents
- chat: Streaming chat via Grok (xAI)
- voice: Voice session management (token issuance)

These add endpoints that don't exist in vanilla OMI:
- /v1/ella/* - Callback endpoints for Letta agents
- /v1/ella/chat/* - Grok streaming chat
- /v1/voice/* - Voice session management
"""

from .callbacks import router as callbacks_router
from .chat import router as chat_router
from .voice import router as voice_router

__all__ = [
    'callbacks_router',
    'chat_router',
    'voice_router',
]
