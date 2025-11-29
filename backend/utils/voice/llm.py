# Voice Mode LLM Integration
#
# Fetches voice config from n8n and streams LLM responses from Groq.

import os
import httpx
from typing import AsyncIterator, Optional, Dict, Any
from groq import AsyncGroq

from .config import VOICE_CONFIG


async def get_voice_config(uid: str, session_id: str) -> Dict[str, Any]:
    """
    Fetch voice agent config from n8n.

    Args:
        uid: User ID
        session_id: Voice session ID

    Returns:
        Config dict with agent_config, blocks, etc.

    Raises:
        Exception if config fetch fails
    """
    async with httpx.AsyncClient(timeout=VOICE_CONFIG.voice_config_timeout) as client:
        response = await client.post(
            VOICE_CONFIG.voice_config_url,
            json={"uid": uid, "session_id": session_id}
        )
        response.raise_for_status()
        config = response.json()

        print(f"🎤 Voice config fetched: cache_hit={config.get('cache_hit')}, "
              f"context={config.get('total_context_chars', 0)} chars", flush=True)

        return config


def build_messages(
    config: Dict[str, Any],
    user_message: str,
    conversation_history: list = None
) -> list:
    """
    Build messages array for LLM from config and conversation.

    Args:
        config: Voice config from n8n
        user_message: Current user utterance
        conversation_history: Previous turns in this session

    Returns:
        List of message dicts for LLM
    """
    messages = []

    # System prompt from config
    agent_config = config.get("agent_config", {})
    system_prompt = agent_config.get("system_prompt", "You are Ella, a helpful AI assistant.")

    # Add memory blocks to system prompt if present
    blocks = config.get("blocks", {})
    if blocks:
        context_parts = [system_prompt, "\n\n=== USER CONTEXT ===\n"]

        if blocks.get("user_profile"):
            context_parts.append(f"\n### User Profile\n{blocks['user_profile']}")

        if blocks.get("rolling_memories"):
            context_parts.append(f"\n### Recent Memories\n{blocks['rolling_memories']}")

        if blocks.get("rolling_summaries"):
            context_parts.append(f"\n### Recent Conversations\n{blocks['rolling_summaries']}")

        system_prompt = "".join(context_parts)

    messages.append({"role": "system", "content": system_prompt})

    # Add conversation history
    if conversation_history:
        for turn in conversation_history:
            messages.append({
                "role": turn.get("role", "user"),
                "content": turn.get("content", "")
            })

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    return messages


async def stream_llm_response(
    config: Dict[str, Any],
    user_message: str,
    conversation_history: list = None
) -> AsyncIterator[str]:
    """
    Stream LLM response from Groq.

    Args:
        config: Voice config from n8n
        user_message: Current user utterance
        conversation_history: Previous turns in this session

    Yields:
        Text chunks as they arrive from LLM
    """
    agent_config = config.get("agent_config", {})

    # Get model settings (with fallbacks)
    model = agent_config.get("model", VOICE_CONFIG.default_model)
    temperature = agent_config.get("temperature", VOICE_CONFIG.default_temperature)
    max_tokens = agent_config.get("max_tokens", VOICE_CONFIG.default_max_tokens)

    # Handle deprecated model
    if model == "llama-3.1-70b-versatile":
        model = "llama-3.3-70b-versatile"
        print(f"⚠️ Model deprecated, using {model}", flush=True)

    # Build messages
    messages = build_messages(config, user_message, conversation_history)

    print(f"🧠 Streaming from Groq ({model})...", flush=True)

    # Stream from Groq
    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True
    )

    full_response = ""
    async for chunk in stream:
        if chunk.choices[0].delta.content:
            text = chunk.choices[0].delta.content
            full_response += text
            yield text

    print(f"✅ LLM response: {len(full_response)} chars", flush=True)
