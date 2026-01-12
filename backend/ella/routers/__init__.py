"""
Ella Routers - Ella-specific API endpoints.

These routers add endpoints that don't exist in vanilla OMI:
- /api/ella/callback/* - n8n webhook callbacks
- /v2/voice - Grok V2V voice endpoint
- /api/v1/testing/* - E2E testing endpoints (dev only)

During migration, these import from legacy locations.
"""

# Import routers from legacy locations during migration
# After migration, these will be local files

try:
    from routers.ella import router as callbacks_router
except ImportError:
    callbacks_router = None

try:
    from routers.voice_v2 import router as voice_v2_router
except ImportError:
    voice_v2_router = None

# Testing router (optional)
try:
    from routers.testing import router as testing_router
except ImportError:
    testing_router = None

__all__ = [
    'callbacks_router',
    'voice_v2_router',
    'testing_router',
]
