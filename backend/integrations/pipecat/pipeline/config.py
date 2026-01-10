"""
Pipeline configuration for Pipecat voice mode.

All configuration is centralized here for easy tuning.
"""

from dataclasses import dataclass, field
from typing import Optional
import os


@dataclass
class VADConfig:
    """Voice Activity Detection configuration."""

    provider: str = "silero"
    stop_secs: float = 1.5  # Silence duration before end-of-turn
    min_volume: float = 0.5  # Minimum volume threshold
    confidence_threshold: float = 0.7  # VAD confidence threshold


@dataclass
class STTConfig:
    """Speech-to-Text configuration."""

    provider: str = "deepgram"
    model: str = "nova-2"
    language: str = "en-US"
    api_key: str = field(default_factory=lambda: os.getenv("DEEPGRAM_API_KEY", ""))


@dataclass
class TTSConfig:
    """Text-to-Speech configuration."""

    provider: str = field(default_factory=lambda: os.getenv("TTS_PROVIDER", "openai"))  # openai, elevenlabs, deepgram
    voice: str = "nova"
    speed: float = 1.0
    streaming: bool = field(default_factory=lambda: os.getenv("TTS_STREAMING", "true").lower() == "true")  # Enable streaming TTS
    api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    elevenlabs_api_key: str = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY", ""))
    elevenlabs_voice_id: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"))  # Rachel


@dataclass
class LLMConfig:
    """Language Model configuration."""

    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "groq"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "llama-3.1-8b-instant"))  # Smaller, faster, higher rate limits
    temperature: float = 0.7
    max_tokens: int = 150  # Keep responses short for voice
    api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))


@dataclass
class GrokV2VConfig:
    """Grok Voice-to-Voice configuration for ultra-low latency mode."""

    enabled: bool = field(default_factory=lambda: os.getenv("GROK_V2V_ENABLED", "false").lower() == "true")
    proxy_url: str = field(default_factory=lambda: os.getenv("GROK_V2V_PROXY_URL", "wss://voice.ella-ai-care.com/ws"))
    api_key: str = field(default_factory=lambda: os.getenv("XAI_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("GROK_MODEL", "grok-4-1-fast-non-reasoning"))  # Latest fast Grok (non-reasoning for speed)
    input_sample_rate: int = 24000  # Grok expects 24kHz input
    output_sample_rate: int = 24000  # Grok outputs 24kHz


@dataclass
class N8NConfig:
    """n8n webhook endpoints configuration."""

    base_url: str = "https://n8n.ella-ai-care.com"
    voice_config_path: str = "/webhook/voice-init"
    memory_agent_path: str = "/webhook/memory-agent"
    summary_agent_path: str = "/webhook/summary-agent"
    call_state_path: str = "/webhook/call-state"  # Call state notifications
    timeout_seconds: float = 10.0

    @property
    def voice_config_url(self) -> str:
        return f"{self.base_url}{self.voice_config_path}"

    @property
    def memory_agent_url(self) -> str:
        return f"{self.base_url}{self.memory_agent_path}"

    @property
    def summary_agent_url(self) -> str:
        return f"{self.base_url}{self.summary_agent_path}"

    @property
    def call_state_url(self) -> str:
        return f"{self.base_url}{self.call_state_path}"


@dataclass
class PipelineConfig:
    """
    Main configuration for Pipecat voice pipeline.

    Usage:
        config = PipelineConfig()  # Use defaults
        config = PipelineConfig(
            vad=VADConfig(stop_secs=2.0),
            llm=LLMConfig(model="llama-3.1-8b-instant")
        )

    Voice Pipeline Modes:
        - "pipecat": Default STT→LLM→TTS pipeline (2-3s latency)
        - "grok_v2v": Grok voice-to-voice API (~500ms latency)
    """

    # Voice pipeline mode selection
    voice_pipeline_mode: str = field(default_factory=lambda: os.getenv("VOICE_PIPELINE_MODE", "pipecat"))

    vad: VADConfig = field(default_factory=VADConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    n8n: N8NConfig = field(default_factory=N8NConfig)
    grok_v2v: GrokV2VConfig = field(default_factory=GrokV2VConfig)

    # Audio settings
    audio_sample_rate: int = 16000  # Input audio sample rate (Pipecat mode)
    audio_channels: int = 1  # Mono

    # Session settings
    max_session_duration_seconds: int = 300  # 5 minutes max

    def validate(self) -> bool:
        """Validate that required API keys are set."""
        errors = []

        if not self.stt.api_key:
            errors.append("DEEPGRAM_API_KEY not set")
        if not self.tts.api_key:
            errors.append("OPENAI_API_KEY not set")
        if not self.llm.api_key:
            errors.append("GROQ_API_KEY not set")

        if errors:
            raise ValueError(f"Pipeline configuration errors: {', '.join(errors)}")

        return True


# Default configuration instance
DEFAULT_CONFIG = PipelineConfig()
