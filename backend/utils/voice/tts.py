# Voice Mode TTS Integration
#
# Converts text to speech using OpenAI TTS (or ElevenLabs).
# Supports streaming for low-latency audio response.

import os
import io
from typing import AsyncIterator, Optional
from openai import AsyncOpenAI

from .config import VOICE_CONFIG


async def stream_tts(
    text: str,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    speed: Optional[float] = None
) -> AsyncIterator[bytes]:
    """
    Stream TTS audio from OpenAI.

    Args:
        text: Text to convert to speech
        voice: Voice ID (default from config)
        model: TTS model (default from config)
        speed: Speech speed multiplier (default from config)

    Yields:
        Audio chunks as bytes (PCM16 format)
    """
    if not text or not text.strip():
        return

    voice = voice or VOICE_CONFIG.tts_voice
    model = model or VOICE_CONFIG.tts_model
    speed = speed or VOICE_CONFIG.tts_speed

    print(f"🔊 TTS streaming: {len(text)} chars, voice={voice}", flush=True)

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    # Request streaming audio
    response = await client.audio.speech.create(
        model=model,
        voice=voice,
        input=text,
        response_format="pcm",  # Raw PCM for streaming
        speed=speed
    )

    # Stream chunks - read content and yield in chunks
    # Note: OpenAI TTS doesn't support true streaming, but we chunk the response
    content = response.content
    chunk_count = 0
    chunk_size = 4096

    for i in range(0, len(content), chunk_size):
        chunk = content[i:i + chunk_size]
        chunk_count += 1
        yield chunk

    print(f"✅ TTS complete: {chunk_count} chunks, {len(content)} bytes", flush=True)


async def generate_tts(
    text: str,
    voice: Optional[str] = None,
    model: Optional[str] = None
) -> bytes:
    """
    Generate complete TTS audio (non-streaming).

    Args:
        text: Text to convert to speech
        voice: Voice ID (default from config)
        model: TTS model (default from config)

    Returns:
        Complete audio as bytes
    """
    chunks = []
    async for chunk in stream_tts(text, voice, model):
        chunks.append(chunk)

    return b''.join(chunks)


async def stream_tts_chunked(
    text_iterator: AsyncIterator[str],
    min_chars: int = 50,
    voice: Optional[str] = None
) -> AsyncIterator[bytes]:
    """
    Stream TTS from text chunks, buffering for natural speech.

    This accumulates text until we have enough for a natural phrase,
    then generates TTS for that phrase. Provides lower latency than
    waiting for full response.

    Args:
        text_iterator: Async iterator of text chunks from LLM
        min_chars: Minimum characters before generating TTS
        voice: Voice ID (default from config)

    Yields:
        Audio chunks as bytes
    """
    buffer = ""
    sentence_endings = {'.', '!', '?', ':', ';'}

    async for text_chunk in text_iterator:
        buffer += text_chunk

        # Check if we have a complete sentence or enough text
        should_flush = False

        # Look for sentence boundaries
        for ending in sentence_endings:
            if ending in buffer:
                # Find the last sentence boundary
                last_boundary = max(buffer.rfind(e) for e in sentence_endings if e in buffer)
                if last_boundary > 0 and last_boundary >= min_chars - 1:
                    should_flush = True
                    break

        # Or if buffer is getting large
        if len(buffer) >= min_chars * 2:
            should_flush = True

        if should_flush and len(buffer.strip()) > 0:
            # Generate TTS for accumulated text
            async for audio_chunk in stream_tts(buffer.strip(), voice):
                yield audio_chunk
            buffer = ""

    # Flush remaining text
    if buffer.strip():
        async for audio_chunk in stream_tts(buffer.strip(), voice):
            yield audio_chunk
