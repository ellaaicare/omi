# Voice Mode Configuration
#
# Centralized settings for voice mode.

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VoiceConfig:
    """Configuration for voice mode."""

    # n8n voice config endpoint
    voice_config_url: str = "https://n8n.ella-ai-care.com/webhook/voice-config"
    voice_config_timeout: float = 5.0

    # LLM settings (can be overridden by n8n config)
    default_model: str = "llama-3.3-70b-versatile"
    default_provider: str = "groq"
    default_max_tokens: int = 150
    default_temperature: float = 0.7

    # TTS settings
    tts_provider: str = "openai"  # "openai" or "elevenlabs"
    tts_model: str = "tts-1"  # "tts-1" or "tts-1-hd"
    tts_voice: str = "nova"  # OpenAI voices: alloy, echo, fable, onyx, nova, shimmer
    tts_speed: float = 1.0

    # Voice session settings
    silence_timeout_seconds: int = 10
    max_session_duration_seconds: int = 300  # 5 minutes

    # Audio format for WebSocket streaming
    audio_format: str = "pcm16"
    audio_sample_rate: int = 24000
    audio_channels: int = 1

    # Feature flags
    voice_mode_enabled: bool = True
    streaming_tts_enabled: bool = True


def load_config() -> VoiceConfig:
    """Load voice configuration from environment variables."""
    config = VoiceConfig()

    # Override from environment
    if os.getenv('VOICE_CONFIG_URL'):
        config.voice_config_url = os.getenv('VOICE_CONFIG_URL')

    if os.getenv('VOICE_TTS_PROVIDER'):
        config.tts_provider = os.getenv('VOICE_TTS_PROVIDER')

    if os.getenv('VOICE_TTS_VOICE'):
        config.tts_voice = os.getenv('VOICE_TTS_VOICE')

    if os.getenv('VOICE_SILENCE_TIMEOUT'):
        config.silence_timeout_seconds = int(os.getenv('VOICE_SILENCE_TIMEOUT'))

    if os.getenv('VOICE_MODE_ENABLED'):
        config.voice_mode_enabled = os.getenv('VOICE_MODE_ENABLED', 'true').lower() == 'true'

    return config


# Global config instance
VOICE_CONFIG = load_config()
