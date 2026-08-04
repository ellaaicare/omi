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

from importlib import import_module

__all__ = [
    'ELLA_CONFIG',
    'is_ella_enabled',
    'send_to_scanner',
    'call_summary_agent',
    'call_memory_agent',
    'is_ella_chat',
]

_LAZY_EXPORTS = {
    'ELLA_CONFIG': ('.config', 'ELLA_CONFIG'),
    'is_ella_enabled': ('.config', 'is_ella_enabled'),
    'send_to_scanner': ('.scanner', 'send_to_scanner'),
    'call_summary_agent': ('.summary', 'call_summary_agent'),
    'call_memory_agent': ('.memory', 'call_memory_agent'),
    'is_ella_chat': ('.chat', 'is_ella_chat'),
}


def __getattr__(name: str):
    """Load legacy convenience exports without importing service clients eagerly."""
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
