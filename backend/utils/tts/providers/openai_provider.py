"""
OpenAI TTS Provider

High-quality cloud-based TTS using OpenAI's neural voices.
"""

import os
from typing import Dict, Any
from openai import OpenAI, OpenAIError
from utils.tts.base import (
    BaseTTSProvider,
    TTSRequest,
    TTSVoice,
    TTSModel,
    TTSGenerationError
)


class OpenAITTSProvider(BaseTTSProvider):
    """OpenAI TTS provider (tts-1, tts-1-hd)"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key required (OPENAI_API_KEY env var or config)")

        self.client = OpenAI(api_key=api_key)
        self.cost_per_char = 0.000015  # $15 per 1M characters

        # Map our standard models to OpenAI models
        self.model_mapping = {
            TTSModel.STANDARD: "tts-1",
            TTSModel.HD: "tts-1-hd"
        }

    async def generate(self, request: TTSRequest) -> bytes:
        """Generate audio using OpenAI TTS API"""
        self.validate_request(request)

        try:
            openai_model = self.model_mapping[request.model]

            response = self.client.audio.speech.create(
                model=openai_model,
                voice=request.voice.value,
                input=request.text,
                response_format="mp3",
                speed=request.speed
            )

            return response.content

        except OpenAIError as e:
            raise TTSGenerationError(
                provider="OpenAI",
                message=f"OpenAI API error: {str(e)}",
                original_error=e
            )
        except Exception as e:
            raise TTSGenerationError(
                provider="OpenAI",
                message=f"Unexpected error: {str(e)}",
                original_error=e
            )

    def get_supported_voices(self) -> list[TTSVoice]:
        """OpenAI supports all 6 standard voices"""
        return [
            TTSVoice.ALLOY,
            TTSVoice.ECHO,
            TTSVoice.FABLE,
            TTSVoice.ONYX,
            TTSVoice.NOVA,  # Best for healthcare
            TTSVoice.SHIMMER
        ]

    def get_supported_models(self) -> list[TTSModel]:
        """OpenAI supports standard and HD"""
        return [TTSModel.STANDARD, TTSModel.HD]

    def estimate_cost(self, text: str) -> float:
        """
        Estimate OpenAI TTS cost.

        OpenAI pricing: $15 per 1M characters
        """
        return len(text) * self.cost_per_char

    def get_max_text_length(self) -> int:
        """OpenAI max: 4096 characters"""
        return 4096

    def get_latency_estimate_ms(self, text: str, model: TTSModel) -> int:
        """
        Estimate generation latency.

        Based on OpenAI benchmarks:
        - tts-1: ~500ms
        - tts-1-hd: ~1000-2000ms
        """
        if model == TTSModel.STANDARD:
            return 500
        else:  # HD
            return 1500
