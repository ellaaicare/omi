# Voice Mode Module
#
# Real-time voice conversations with Ella AI.
# Uses Groq for LLM streaming and OpenAI/ElevenLabs for TTS.
#
# Components:
#   - config.py: Voice mode configuration
#   - session.py: Voice session state machine
#   - llm.py: Groq streaming client
#   - tts.py: TTS streaming (OpenAI, modular for ElevenLabs)

from .config import VOICE_CONFIG
from .session import VoiceSession, VoiceState
from .llm import stream_llm_response, get_voice_config
from .tts import stream_tts

__all__ = [
    'VOICE_CONFIG',
    'VoiceSession',
    'VoiceState',
    'stream_llm_response',
    'get_voice_config',
    'stream_tts',
]
