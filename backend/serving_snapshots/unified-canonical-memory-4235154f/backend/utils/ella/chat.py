# Ella Chat Adapter
#
# Routes chat messages to the n8n LLM Proxy for Ella users.
# This enables personalized responses with Letta context injection.
#
# Architecture: Uses standard ChatOpenAI pointing to n8n's OpenAI-compatible endpoint.
# No custom SSE parsing needed - LangChain handles it.
# See: utils/retrieval/graph.py for llm_ella_proxy_stream definition

from typing import Optional

# App IDs that should be routed to Ella LLM Proxy
ELLA_APP_IDS = {"ella-ai-agent", "ella-ai-app", "ella-ai"}


def is_ella_chat(app_id: Optional[str]) -> bool:
    """Check if this chat should be routed to Ella LLM Proxy."""
    if not app_id:
        return False
    return app_id in ELLA_APP_IDS
