# Ella AI Integration Module
#
# This module contains all Ella/n8n integration code, kept separate from
# core OMI backend to minimize merge conflicts with upstream.
#
# Components:
#   - config.py: Endpoints, timeouts, feature flags
#   - scanner.py: Real-time transcript scanning
#   - summary.py: Conversation summary generation
#   - memory.py: Memory extraction
#   - chat.py: Chat routing helpers

__all__ = [
    'ELLA_CONFIG',
    'is_ella_enabled',
    'send_to_scanner',
    'call_summary_agent',
    'call_memory_agent',
    'is_ella_chat',
]


def __getattr__(name: str):
    """Load legacy convenience exports without importing service clients eagerly."""
    if name in {'ELLA_CONFIG', 'is_ella_enabled'}:
        from .config import ELLA_CONFIG, is_ella_enabled

        return {'ELLA_CONFIG': ELLA_CONFIG, 'is_ella_enabled': is_ella_enabled}[name]
    if name == 'send_to_scanner':
        from .scanner import send_to_scanner

        return send_to_scanner
    if name == 'call_summary_agent':
        from .summary import call_summary_agent

        return call_summary_agent
    if name == 'call_memory_agent':
        from .memory import call_memory_agent

        return call_memory_agent
    if name == 'is_ella_chat':
        from .chat import is_ella_chat

        return is_ella_chat
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
