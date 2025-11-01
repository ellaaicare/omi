"""
Base TTS Provider Interface

This module defines the abstract base class for TTS providers,
allowing easy A/B testing and provider swapping.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pydantic import BaseModel
from enum import Enum


class TTSVoice(str, Enum):
    """Standard voice types across providers"""
    ALLOY = "alloy"  # Neutral, balanced
    ECHO = "echo"    # Male, authoritative
    FABLE = "fable"  # British accent, warm
    ONYX = "onyx"    # Deep male, confident
    NOVA = "nova"    # Female, warm, caring (DEFAULT for healthcare)
    SHIMMER = "shimmer"  # Soft female, friendly


class TTSModel(str, Enum):
    """TTS model quality levels"""
    STANDARD = "standard"  # Fast, lower quality
    HD = "hd"             # High quality, slight latency
    ULTRA = "ultra"       # Ultra quality (if provider supports)


class TTSRequest(BaseModel):
    """TTS generation request"""
    text: str
    voice: TTSVoice = TTSVoice.NOVA
    model: TTSModel = TTSModel.HD
    cache_key: Optional[str] = None
    speed: float = 1.0  # Playback speed (0.25 - 4.0)


class TTSResponse(BaseModel):
    """TTS generation response"""
    audio_url: str
    duration_ms: Optional[int] = None
    cached: bool = False
    voice: str
    provider: str
    size_bytes: int
    expires_at: Optional[str] = None
    model: str


class BaseTTSProvider(ABC):
    """
    Abstract base class for TTS providers.

    Implementations: OpenAITTSProvider, CoquiTTSProvider, PiperTTSProvider, etc.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize TTS provider with configuration.

        Args:
            config: Provider-specific configuration dict
                   (API keys, model paths, endpoint URLs, etc.)
        """
        self.config = config
        self.provider_name = self.__class__.__name__.replace("TTSProvider", "").lower()

    @abstractmethod
    async def generate(self, request: TTSRequest) -> bytes:
        """
        Generate audio from text.

        Args:
            request: TTSRequest with text, voice, model, etc.

        Returns:
            Audio bytes (MP3 format)

        Raises:
            TTSGenerationError: If generation fails
        """
        pass

    @abstractmethod
    def get_supported_voices(self) -> list[TTSVoice]:
        """
        Get list of supported voices for this provider.

        Returns:
            List of TTSVoice enums supported
        """
        pass

    @abstractmethod
    def get_supported_models(self) -> list[TTSModel]:
        """
        Get list of supported models for this provider.

        Returns:
            List of TTSModel enums supported
        """
        pass

    @abstractmethod
    def estimate_cost(self, text: str) -> float:
        """
        Estimate generation cost in USD.

        Args:
            text: Input text

        Returns:
            Estimated cost in dollars
        """
        pass

    def validate_request(self, request: TTSRequest) -> None:
        """
        Validate TTS request for this provider.

        Args:
            request: TTSRequest to validate

        Raises:
            ValueError: If request is invalid for this provider
        """
        if request.voice not in self.get_supported_voices():
            raise ValueError(
                f"Voice '{request.voice}' not supported by {self.provider_name}. "
                f"Supported voices: {[v.value for v in self.get_supported_voices()]}"
            )

        if request.model not in self.get_supported_models():
            raise ValueError(
                f"Model '{request.model}' not supported by {self.provider_name}. "
                f"Supported models: {[m.value for m in self.get_supported_models()]}"
            )

        if len(request.text) == 0:
            raise ValueError("Text cannot be empty")

        if len(request.text) > self.get_max_text_length():
            raise ValueError(
                f"Text too long ({len(request.text)} chars). "
                f"Max for {self.provider_name}: {self.get_max_text_length()} chars"
            )

    @abstractmethod
    def get_max_text_length(self) -> int:
        """
        Get maximum text length in characters.

        Returns:
            Max character count
        """
        pass

    def get_provider_name(self) -> str:
        """Get provider name for logging/analytics"""
        return self.provider_name


class TTSGenerationError(Exception):
    """Raised when TTS generation fails"""
    def __init__(self, provider: str, message: str, original_error: Optional[Exception] = None):
        self.provider = provider
        self.message = message
        self.original_error = original_error
        super().__init__(f"[{provider}] {message}")
