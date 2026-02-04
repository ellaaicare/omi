"""
Ella Callback Router - DEPRECATED

All custom Ella endpoints have been removed in favor of official OMI API endpoints.
See ella/docs/N8N_MIGRATION_GUIDE.md for migration instructions.

Endpoint Migration:
- POST /v1/ella/memory -> POST /v1/memories (official)
- POST /v1/ella/conversation-summary -> PATCH /v1/conversations/{id} (official)
- POST /v1/ella/notification -> POST /v1/notification (official)
- POST /v1/ella/voice-session -> POST /v1/dev/user/conversations/from-segments (official)

Why removed:
1. Redundant with official API endpoints
2. Risk of data corruption (bypassed Pydantic validation)
3. Caused production 500 errors due to schema mismatch
4. Maintenance burden of duplicate functionality

See docs/DATA_SCHEMA_REQUIREMENTS.md for schema requirements.
"""

import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-deprecated"])


@router.get("/health")
async def ella_health():
    """
    Health check for Ella integration.

    NOTE: Custom callback endpoints have been removed.
    Use official OMI API endpoints instead.
    See ella/docs/N8N_MIGRATION_GUIDE.md
    """
    return {
        "status": "ok",
        "service": "ella-integration",
        "message": "Custom callbacks removed - use official OMI API",
        "migration_guide": "ella/docs/N8N_MIGRATION_GUIDE.md",
        "official_endpoints": {
            "memories": "POST /v1/memories",
            "notifications": "POST /v1/notification",
            "conversations": "POST /v1/dev/user/conversations/from-segments"
        }
    }
