# Voice Mode LLM Integration
#
# Fetches voice config from n8n and streams LLM responses from multiple providers.

import os
import httpx
from typing import AsyncIterator, Optional, Dict, Any
from groq import AsyncGroq
from openai import AsyncOpenAI

from .config import VOICE_CONFIG


def detect_llm_provider(model_name: str) -> str:
    """Detect LLM provider from model name."""
    model_lower = model_name.lower()
    if "grok" in model_lower:
        return "xai"  # xAI Grok
    elif "gpt" in model_lower or "o1" in model_lower:
        return "openai"
    elif "claude" in model_lower:
        return "anthropic"
    elif "llama" in model_lower or "mixtral" in model_lower:
        return "groq"
    else:
        return "groq"  # Default to groq


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
    Stream LLM response from configured provider (Groq, OpenAI, or xAI).

    Args:
        config: Voice config from n8n
        user_message: Current user utterance
        conversation_history: Previous turns in this session

    Yields:
        Text chunks as they arrive from LLM
    """
    agent_config = config.get("agent_config", {})

    # Get model settings from n8n config
    model = agent_config.get("model", VOICE_CONFIG.default_model)
    temperature = agent_config.get("temperature", VOICE_CONFIG.default_temperature)
    max_tokens = agent_config.get("max_tokens", VOICE_CONFIG.default_max_tokens)

    # Handle deprecated model
    if model == "llama-3.1-70b-versatile":
        model = "llama-3.3-70b-versatile"
        print(f"⚠️ Model deprecated, using {model}", flush=True)

    # Detect provider from model name
    provider = detect_llm_provider(model)

    # Build messages
    messages = build_messages(config, user_message, conversation_history)

    print(f"🧠 Streaming from {provider.upper()} ({model})...", flush=True)

    # Route to correct provider
    if provider == "xai":
        async for chunk in _stream_xai(model, messages, temperature, max_tokens):
            yield chunk
    elif provider == "openai":
        async for chunk in _stream_openai(model, messages, temperature, max_tokens):
            yield chunk
    else:
        # Default to Groq
        async for chunk in _stream_groq(model, messages, temperature, max_tokens):
            yield chunk


async def _stream_groq(model: str, messages: list, temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from Groq API."""
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

    print(f"✅ Groq response: {len(full_response)} chars", flush=True)


async def _stream_openai(model: str, messages: list, temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from OpenAI API."""
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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

    print(f"✅ OpenAI response: {len(full_response)} chars", flush=True)


async def _stream_xai(model: str, messages: list, temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from xAI (Grok) API."""
    xai_api_key = os.environ.get("XAI_API_KEY")
    if not xai_api_key:
        raise ValueError("XAI_API_KEY not set, cannot use Grok model")

    # xAI uses OpenAI-compatible API
    client = AsyncOpenAI(
        api_key=xai_api_key,
        base_url="https://api.x.ai/v1"
    )

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

    print(f"✅ xAI response: {len(full_response)} chars", flush=True)
