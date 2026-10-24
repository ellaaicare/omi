"""
Ella Adapters - Swap-in replacements for upstream functions.

These adapters implement the same interface as upstream OMI functions
but route to Ella's n8n/Letta backend instead of direct OpenAI calls.

Current adapters (imported from legacy locations during migration):
- summary: Routes summary generation to n8n
- memory: Routes memory extraction to n8n
- scanner: Sends real-time transcripts to scanner agent
"""

# During migration, import from legacy locations
# After migration, these will be local imports

try:
    from utils.ella.summary import call_summary_agent as summary_adapter
except ImportError:
    summary_adapter = None

try:
    from utils.ella.memory import call_memory_agent as memory_adapter
except ImportError:
    memory_adapter = None

try:
    from utils.ella.scanner import send_to_scanner as scanner_adapter
except ImportError:
    scanner_adapter = None

__all__ = [
    'summary_adapter',
    'memory_adapter',
    'scanner_adapter',
]
