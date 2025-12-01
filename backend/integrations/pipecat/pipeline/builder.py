"""
Pipeline builder for Pipecat voice mode.

This module creates and runs the Pipecat pipeline for real-time
voice conversations with Ella AI.
"""

import asyncio
import uuid
import os
from typing import Optional
from fastapi import WebSocket

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

from .config import PipelineConfig, DEFAULT_CONFIG
from ..processors.ella_config import EllaConfigProcessor
from ..processors.conversation_logger import ConversationLogger
from ..services.n8n_client import N8NClient


async def create_voice_pipeline(
    websocket: WebSocket,
    uid: str,
    session_id: Optional[str] = None,
    config: Optional[PipelineConfig] = None,
) -> tuple[Pipeline, PipelineTask]:
    """
    Create a Pipecat pipeline for voice mode.

    Args:
        websocket: FastAPI WebSocket connection
        uid: Firebase user ID
        session_id: Optional session ID (generated if not provided)
        config: Pipeline configuration (uses defaults if not provided)

    Returns:
        Tuple of (Pipeline, PipelineTask)
    """
    config = config or DEFAULT_CONFIG
    config.validate()

    session_id = session_id or str(uuid.uuid4())

    # Fetch Ella configuration from n8n
    n8n_client = N8NClient(config.n8n)
    ella_config = await n8n_client.fetch_voice_config(uid)

    # Build system prompt with memory context
    system_prompt = _build_system_prompt(ella_config)

    print(f"🎙️ Creating voice pipeline for uid={uid}, session={session_id[:8]}...")

    # 1. Configure transport with VAD
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    stop_secs=config.vad.stop_secs,
                    min_volume=config.vad.min_volume,
                )
            ),
            vad_audio_passthrough=True,
        ),
    )

    # 2. Configure STT (Deepgram)
    stt = DeepgramSTTService(
        api_key=config.stt.api_key,
        model=config.stt.model,
        language=config.stt.language,
    )

    # 3. Configure LLM (Groq)
    # Use model from n8n config if available, fallback to default
    llm_model = ella_config.get("agent_config", {}).get("model", config.llm.model)
    llm = GroqLLMService(
        api_key=config.llm.api_key,
        model=llm_model,
    )

    # 4. Configure TTS (OpenAI)
    tts = OpenAITTSService(
        api_key=config.tts.api_key,
        voice=config.tts.voice,
    )

    # 5. Create LLM context with system prompt
    context = OpenAILLMContext(
        messages=[{"role": "system", "content": system_prompt}]
    )
    context_aggregator = llm.create_context_aggregator(context)

    # 6. Create custom processors
    ella_processor = EllaConfigProcessor(ella_config)
    conversation_logger = ConversationLogger(
        uid=uid,
        session_id=session_id,
        n8n_client=n8n_client,
    )

    # 7. Build pipeline
    pipeline = Pipeline(
        [
            transport.input(),           # Audio from iOS
            stt,                          # Speech to text
            context_aggregator.user(),    # Add user message to context
            llm,                          # Generate response
            tts,                          # Text to speech
            transport.output(),           # Audio back to iOS
            context_aggregator.assistant(), # Add assistant message to context
            conversation_logger,          # Log conversation
        ]
    )

    # 8. Create task
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    # Register cleanup on task completion
    @task.on_task_completed.register
    async def on_completed(task):
        print(f"✅ Voice session completed: {session_id[:8]}")
        await conversation_logger.finalize_session()

    return pipeline, task


async def run_voice_session(
    websocket: WebSocket,
    uid: str,
    session_id: Optional[str] = None,
    config: Optional[PipelineConfig] = None,
) -> None:
    """
    Run a complete voice session.

    This is the main entry point for the /v2/voice endpoint.

    Args:
        websocket: FastAPI WebSocket connection
        uid: Firebase user ID
        session_id: Optional session ID
        config: Pipeline configuration
    """
    session_id = session_id or str(uuid.uuid4())

    print(f"🎤 Starting voice session for uid={uid}, session={session_id[:8]}")

    try:
        pipeline, task = await create_voice_pipeline(
            websocket=websocket,
            uid=uid,
            session_id=session_id,
            config=config,
        )

        runner = PipelineRunner()
        await runner.run(task)

    except Exception as e:
        print(f"❌ Voice session error: {e}")
        raise

    finally:
        print(f"🔚 Voice session ended: {session_id[:8]}")


def _build_system_prompt(ella_config: dict) -> str:
    """
    Build system prompt with persona and memory blocks from n8n.

    Args:
        ella_config: Configuration from n8n voice-config endpoint

    Returns:
        Complete system prompt string
    """
    persona = ella_config.get("persona", "You are Ella, a warm and caring AI companion.")
    blocks = ella_config.get("blocks", {})
    user_info = ella_config.get("user", {})

    user_name = user_info.get("name", "the user")
    user_profile = blocks.get("user_profile", "No profile available.")
    rolling_memories = blocks.get("rolling_memories", "No recent memories.")
    rolling_summaries = blocks.get("rolling_summaries", "No recent conversations.")

    return f"""
{persona}

## About {user_name}
{user_profile}

## Recent Memories
{rolling_memories}

## Recent Conversations
{rolling_summaries}

## Voice Conversation Guidelines
- Be warm, concise, and helpful
- Keep responses SHORT (1-3 sentences) - this is voice, not text
- Reference the user's memories naturally when relevant
- Ask follow-up questions to maintain conversation flow
- If you don't know something, say so briefly
- Use natural speech patterns, not formal writing
""".strip()
