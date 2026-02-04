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

from .config import ELLA_CONFIG, is_ella_enabled
from .scanner import send_to_scanner
from .summary import call_summary_agent
from .memory import call_memory_agent
from .chat import is_ella_chat

__all__ = [
    'ELLA_CONFIG',
    'is_ella_enabled',
    'send_to_scanner',
    'call_summary_agent',
    'call_memory_agent',
    'is_ella_chat',
]
