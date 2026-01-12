"""
Ella Backend API Extensions
===========================

This module contains ALL Ella-specific backend code for the OMI fork.
Import this module to activate Ella features.

Usage in main.py:
    from ella import register_ella_extensions
    register_ella_extensions(app)

Environment Variables:
    ELLA_ENABLED=true           Master switch
    ELLA_SUMMARY_ENABLED=true   n8n summary generation
    ELLA_MEMORY_ENABLED=true    n8n memory extraction
    ELLA_VOICE_V2_ENABLED=true  Grok V2V endpoint

See ella/README.md for full documentation.
"""

import os
from typing import Optional, Callable, Dict

# =============================================================================
# CONFIGURATION
# =============================================================================

ELLA_ENABLED = os.getenv("ELLA_ENABLED", "true").lower() == "true"
ELLA_SUMMARY_ENABLED = os.getenv("ELLA_SUMMARY_ENABLED", "true").lower() == "true"
ELLA_MEMORY_ENABLED = os.getenv("ELLA_MEMORY_ENABLED", "true").lower() == "true"
ELLA_SCANNER_ENABLED = os.getenv("ELLA_SCANNER_ENABLED", "true").lower() == "true"
ELLA_NOTIFICATIONS_ENABLED = os.getenv("ELLA_NOTIFICATIONS_ENABLED", "true").lower() == "true"
ELLA_VOICE_V2_ENABLED = os.getenv("ELLA_VOICE_V2_ENABLED", "true").lower() == "true"
ELLA_TESTING_ENABLED = os.getenv("ELLA_TESTING_ENABLED", "false").lower() == "true"

# n8n Configuration
ELLA_N8N_BASE_URL = os.getenv("ELLA_N8N_BASE_URL", "https://n8n.ella-ai-care.com")
ELLA_N8N_TIMEOUT = float(os.getenv("ELLA_N8N_TIMEOUT", "30.0"))

# Grok V2V Configuration
GROK_V2V_PROXY_URL = os.getenv("GROK_V2V_PROXY_URL", "wss://voice.ella-ai-care.com/ws")

# =============================================================================
# ADAPTER REGISTRY
# =============================================================================

_ADAPTERS: Dict[str, Callable] = {}


def register_adapter(name: str, adapter: Callable) -> None:
    """Register an Ella adapter to replace upstream functionality."""
    _ADAPTERS[name] = adapter
    print(f"  📦 Registered adapter: {name}", flush=True)


def get_adapter(name: str) -> Optional[Callable]:
    """
    Get an Ella adapter by name.

    Returns None if:
    - ELLA_ENABLED is False
    - Adapter not registered
    - Specific feature is disabled

    Usage:
        from ella import get_adapter

        ella_summary = get_adapter("summary")
        if ella_summary:
            result = await ella_summary(uid, conversation, transcript)
            if result:
                return result  # Ella handled it
        # Fall through to upstream...
    """
    if not ELLA_ENABLED:
        return None
    return _ADAPTERS.get(name)


def get_all_adapters() -> Dict[str, Callable]:
    """Get all registered adapters."""
    return _ADAPTERS.copy()


# =============================================================================
# EXTENSION REGISTRATION
# =============================================================================

def register_ella_extensions(app) -> None:
    """
    Register all Ella extensions with the FastAPI app.

    This is the main entry point called from main.py:
        from ella import register_ella_extensions
        register_ella_extensions(app)

    Args:
        app: FastAPI application instance
    """
    if not ELLA_ENABLED:
        print("🔕 Ella extensions disabled (ELLA_ENABLED=false)", flush=True)
        return

    print("", flush=True)
    print("🏥 ════════════════════════════════════════════", flush=True)
    print("🏥  ELLA AI CARE - Backend Extensions Loading", flush=True)
    print("🏥 ════════════════════════════════════════════", flush=True)

    # -------------------------------------------------------------------------
    # 1. Register Adapters (replace upstream functions)
    # -------------------------------------------------------------------------
    _register_adapters()

    # -------------------------------------------------------------------------
    # 2. Register Routers (Ella-specific endpoints)
    # -------------------------------------------------------------------------
    _register_routers(app)

    # -------------------------------------------------------------------------
    # 3. Print Status
    # -------------------------------------------------------------------------
    print("", flush=True)
    print("🏥 Ella Extensions Status:", flush=True)
    print(f"   • n8n Summary:    {'✅ ON' if ELLA_SUMMARY_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • n8n Memory:     {'✅ ON' if ELLA_MEMORY_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • n8n Scanner:    {'✅ ON' if ELLA_SCANNER_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Notifications:  {'✅ ON' if ELLA_NOTIFICATIONS_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Voice V2:       {'✅ ON' if ELLA_VOICE_V2_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Testing:        {'✅ ON' if ELLA_TESTING_ENABLED else '❌ OFF'}", flush=True)
    print("", flush=True)
    print(f"🏥 n8n Base URL: {ELLA_N8N_BASE_URL}", flush=True)
    print(f"🏥 Grok Proxy:   {GROK_V2V_PROXY_URL}", flush=True)
    print("🏥 ════════════════════════════════════════════", flush=True)
    print("", flush=True)


def _register_adapters() -> None:
    """Register all Ella adapters."""
    print("", flush=True)
    print("📦 Registering Ella Adapters...", flush=True)

    # Import adapters (they self-register)
    # For now, import from existing locations - will consolidate later

    if ELLA_SUMMARY_ENABLED:
        try:
            from utils.ella.summary import call_summary_agent
            register_adapter("summary", call_summary_agent)
        except ImportError as e:
            print(f"  ⚠️ Summary adapter not available: {e}", flush=True)

    if ELLA_MEMORY_ENABLED:
        try:
            from utils.ella.memory import call_memory_agent
            register_adapter("memory", call_memory_agent)
        except ImportError as e:
            print(f"  ⚠️ Memory adapter not available: {e}", flush=True)

    if ELLA_SCANNER_ENABLED:
        try:
            from utils.ella.scanner import send_to_scanner
            register_adapter("scanner", send_to_scanner)
        except ImportError as e:
            print(f"  ⚠️ Scanner adapter not available: {e}", flush=True)

    print(f"📦 Registered {len(_ADAPTERS)} adapters", flush=True)


def _register_routers(app) -> None:
    """Register Ella-specific API routers."""
    print("", flush=True)
    print("🌐 Registering Ella Routers...", flush=True)

    # Ella callback endpoints (n8n webhooks)
    try:
        from routers.ella import router as ella_callback_router
        app.include_router(ella_callback_router, tags=["Ella Callbacks"])
        print("  🌐 /api/ella/* - Callback endpoints", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella callbacks not available: {e}", flush=True)

    # Voice V2 endpoint (Grok V2V)
    if ELLA_VOICE_V2_ENABLED:
        try:
            from routers.voice_v2 import router as voice_v2_router
            app.include_router(voice_v2_router, tags=["Ella Voice V2"])
            print("  🌐 /v2/voice - Grok V2V endpoint", flush=True)
        except ImportError as e:
            print(f"  ⚠️ Voice V2 not available: {e}", flush=True)

    # Testing endpoints (dev only)
    if ELLA_TESTING_ENABLED:
        try:
            from routers.testing import router as testing_router
            app.include_router(testing_router, tags=["Ella Testing"])
            print("  🌐 /api/v1/testing/* - E2E test endpoints", flush=True)
        except ImportError as e:
            print(f"  ⚠️ Testing endpoints not available: {e}", flush=True)


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    'ELLA_ENABLED',
    'ELLA_SUMMARY_ENABLED',
    'ELLA_MEMORY_ENABLED',
    'ELLA_SCANNER_ENABLED',
    'ELLA_NOTIFICATIONS_ENABLED',
    'ELLA_VOICE_V2_ENABLED',
    'ELLA_TESTING_ENABLED',
    'ELLA_N8N_BASE_URL',
    'GROK_V2V_PROXY_URL',

    # Adapter registry
    'register_adapter',
    'get_adapter',
    'get_all_adapters',

    # Main entry point
    'register_ella_extensions',
]
