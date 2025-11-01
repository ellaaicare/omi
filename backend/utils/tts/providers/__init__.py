"""TTS Providers"""

from utils.tts.providers.openai_provider import OpenAITTSProvider
from utils.tts.providers.coqui_provider import CoquiTTSProvider

__all__ = ["OpenAITTSProvider", "CoquiTTSProvider"]
