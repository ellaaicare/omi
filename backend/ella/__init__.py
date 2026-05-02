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
    ELLA_SUMMARY_ENABLED=false  Legacy n8n summarize-transcript generation
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
ELLA_SUMMARY_ENABLED = os.getenv("ELLA_SUMMARY_ENABLED", "false").lower() == "true"
ELLA_MEMORY_ENABLED = os.getenv("ELLA_MEMORY_ENABLED", "true").lower() == "true"
ELLA_SCANNER_ENABLED = os.getenv("ELLA_SCANNER_ENABLED", "true").lower() == "true"
ELLA_NOTIFICATIONS_ENABLED = os.getenv("ELLA_NOTIFICATIONS_ENABLED", "true").lower() == "true"
ELLA_VOICE_V2_ENABLED = os.getenv("ELLA_VOICE_V2_ENABLED", "true").lower() == "true"
ELLA_TESTING_ENABLED = os.getenv("ELLA_TESTING_ENABLED", "false").lower() == "true"
ELLA_GUARDIAN_ENABLED = os.getenv("ELLA_GUARDIAN_ENABLED", "true").lower() == "true"

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
    # 0. Apply Compatibility Patches (for old Ella data)
    # -------------------------------------------------------------------------
    _apply_compat_patches()

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
    # Get compat status
    try:
        from ella.compat import ELLA_COMPAT_ENABLED
    except ImportError:
        ELLA_COMPAT_ENABLED = False

    from ella.config import ELLA_CONFIG

    _debug_labels = {0: "production", 1: "ACK", 2: "Grok LLM", 3: "n8n callback"}
    _dl = ELLA_CONFIG.debug_level
    _dl_label = _debug_labels.get(_dl, f"unknown({_dl})")

    print("🏥 Ella Extensions Status:", flush=True)
    print(f"   • Compat Layer:   {'✅ ON' if ELLA_COMPAT_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • n8n Summary:    {'✅ ON' if ELLA_SUMMARY_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • n8n Memory:     {'✅ ON' if ELLA_MEMORY_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • n8n Scanner:    {'✅ ON' if ELLA_SCANNER_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Notifications:  {'✅ ON' if ELLA_NOTIFICATIONS_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Voice V2:       {'✅ ON' if ELLA_VOICE_V2_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Guardian Mode: {'✅ ON' if ELLA_GUARDIAN_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Testing:        {'✅ ON' if ELLA_TESTING_ENABLED else '❌ OFF'}", flush=True)
    print(f"   • Chat Debug:     Level {_dl} ({_dl_label})", flush=True)
    print("", flush=True)
    print(f"🏥 n8n Base URL: {ELLA_N8N_BASE_URL}", flush=True)
    print(f"🏥 Grok Proxy:   {GROK_V2V_PROXY_URL}", flush=True)
    print("🏥 ════════════════════════════════════════════", flush=True)
    print("", flush=True)


def _apply_compat_patches() -> None:
    """
    Apply compatibility patches for old Ella data.

    This monkey-patches database functions to normalize conversation data
    before Pydantic validation, fixing issues with old Ella conversations
    that are missing fields required by vanilla OMI models.

    Patches applied:
    - database.conversations.get_conversation -> adds missing fields
    - database.conversations.get_conversations -> patches list results
    """
    print("", flush=True)
    print("🔧 Applying Ella Compatibility Patches...", flush=True)

    try:
        from ella.compat import patch_conversation_data, patch_conversation_list, ELLA_COMPAT_ENABLED

        if not ELLA_COMPAT_ENABLED:
            print("  ⚠️ Compat patches disabled (ELLA_COMPAT_ENABLED=false)", flush=True)
            return

        import database.conversations as conversations_db

        # Store original functions
        _original_get_conversation = conversations_db.get_conversation
        _original_get_conversations = conversations_db.get_conversations

        # Create patched versions
        def patched_get_conversation(uid: str, conversation_id: str):
            result = _original_get_conversation(uid, conversation_id)
            if result:
                return patch_conversation_data(result)
            return result

        def patched_get_conversations(uid: str, *args, **kwargs):
            result = _original_get_conversations(uid, *args, **kwargs)
            if result:
                return patch_conversation_list(result)
            return result

        # Apply patches
        conversations_db.get_conversation = patched_get_conversation
        conversations_db.get_conversations = patched_get_conversations

        print("  ✅ Patched: get_conversation (adds missing fields)", flush=True)
        print("  ✅ Patched: get_conversations (patches list results)", flush=True)

    except Exception as e:
        print(f"  ⚠️ Compat patches failed: {e}", flush=True)


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

    # Ella callback endpoints (receives callbacks from n8n/Letta agents)
    try:
        from ella.routers.callbacks import router as callbacks_router

        app.include_router(callbacks_router, tags=["Ella Callbacks"])
        print("  🌐 /v1/ella/* - Callback endpoints", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella callbacks not available: {e}", flush=True)

    # Chat streaming (Grok xAI)
    try:
        from ella.routers.chat import router as chat_router

        app.include_router(chat_router, tags=["Ella Chat"])
        print("  🌐 /v1/ella/chat/* - Chat streaming endpoints", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella chat not available: {e}", flush=True)

    # Resolve endpoint (user identity -> agent routing)
    try:
        from ella.routers.resolve import router as resolve_router

        app.include_router(resolve_router, tags=["Ella Resolve"])
        print("  🌐 /v1/ella/resolve - User-to-agent resolution", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella resolve not available: {e}", flush=True)

    # Debug tracing (routing visibility)
    try:
        from ella.routers.trace import router as trace_router

        app.include_router(trace_router, tags=["Ella Debug"])
        print("  🌐 /v1/ella/debug/* - Routing trace & debug", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella trace not available: {e}", flush=True)

    # Debug metadata proxy (Observer sidecars)
    try:
        from ella.routers.debug_metadata import router as debug_metadata_router

        app.include_router(debug_metadata_router, tags=["Ella Debug"])
        print("  🌐 /v1/ella/debug/conversations/* - Observer metadata", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella debug metadata not available: {e}", flush=True)

    # App-facing conversation correction endpoint (iOS Correct Summary)
    try:
        from ella.routers.corrections import router as corrections_router

        app.include_router(corrections_router, tags=["Conversation Corrections"])
        print("  🌐 /v1/ella/conversations/*/corrections - Summary correction loop", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella corrections not available: {e}", flush=True)

    # Escalation policy resolver (classifier output -> deterministic delivery plan)
    try:
        from ella.routers.escalations import router as escalations_router

        app.include_router(escalations_router, tags=["Ella Escalations"])
        print("  🌐 /v1/ella/escalations/* - Escalation policy", flush=True)
    except ImportError as e:
        print(f"  ⚠️ Ella escalation policy not available: {e}", flush=True)

    # Voice session management (token issuance for Ella Voice)
    if ELLA_VOICE_V2_ENABLED:
        try:
            from ella.routers.voice import router as voice_router

            app.include_router(voice_router, tags=["Ella Voice"])
            print("  🌐 /v1/voice/* - Voice session endpoints", flush=True)
        except ImportError as e:
            print(f"  ⚠️ Voice endpoints not available: {e}", flush=True)

    # Guardian Mode (audio queue for iOS)
    if ELLA_GUARDIAN_ENABLED:
        try:
            from ella.routers.guardian import router as guardian_router

            app.include_router(guardian_router, tags=["Guardian Mode"])
            print("  🌐 /v1/ella/guardian/* - Guardian Mode endpoints", flush=True)
        except ImportError as e:
            print(f"  ⚠️ Guardian Mode not available: {e}", flush=True)

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
    'ELLA_GUARDIAN_ENABLED',
    'ELLA_N8N_BASE_URL',
    'GROK_V2V_PROXY_URL',
    # Adapter registry
    'register_adapter',
    'get_adapter',
    'get_all_adapters',
    # Main entry point
    'register_ella_extensions',
]
