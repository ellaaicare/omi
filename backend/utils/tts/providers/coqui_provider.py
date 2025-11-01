"""
Coqui TTS Provider

Self-hosted open-source TTS using Mozilla's Coqui models.
Designed to run on M4 Pro Mac via Tailscale.
"""

import httpx
from typing import Dict, Any
from utils.tts.base import (
    BaseTTSProvider,
    TTSRequest,
    TTSVoice,
    TTSModel,
    TTSGenerationError
)


class CoquiTTSProvider(BaseTTSProvider):
    """Coqui TTS provider (self-hosted on M4 Mac)"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Tailscale endpoint for M4 Mac
        self.base_url = config.get("base_url") or "http://m4-mac.tailscale:5000"
        self.timeout = config.get("timeout", 30.0)  # 30s timeout

        # Coqui voice mapping (using VCTK dataset)
        self.voice_mapping = {
            TTSVoice.NOVA: "p225",      # Female, warm (Scottish)
            TTSVoice.SHIMMER: "p234",   # Female, soft (Scottish)
            TTSVoice.ALLOY: "p230",     # Neutral (English)
            TTSVoice.ECHO: "p245",      # Male, authoritative (Irish)
            TTSVoice.ONYX: "p260",      # Male, deep (Scottish)
            TTSVoice.FABLE: "p270",     # British (English)
        }

    async def generate(self, request: TTSRequest) -> bytes:
        """Generate audio using Coqui TTS server"""
        self.validate_request(request)

        try:
            coqui_voice = self.voice_mapping[request.voice]

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/tts",
                    json={
                        "text": request.text,
                        "speaker_id": coqui_voice,
                        "language_id": "en",
                        "speed": request.speed
                    }
                )

                if response.status_code != 200:
                    raise TTSGenerationError(
                        provider="Coqui",
                        message=f"Coqui server error: {response.status_code} - {response.text}"
                    )

                return response.content

        except httpx.TimeoutException as e:
            raise TTSGenerationError(
                provider="Coqui",
                message="Coqui server timeout (is M4 Mac running?)",
                original_error=e
            )
        except httpx.ConnectError as e:
            raise TTSGenerationError(
                provider="Coqui",
                message="Cannot connect to Coqui server (check Tailscale and M4 Mac)",
                original_error=e
            )
        except Exception as e:
            raise TTSGenerationError(
                provider="Coqui",
                message=f"Unexpected error: {str(e)}",
                original_error=e
            )

    def get_supported_voices(self) -> list[TTSVoice]:
        """Coqui supports all 6 voices (mapped to VCTK speakers)"""
        return list(self.voice_mapping.keys())

    def get_supported_models(self) -> list[TTSModel]:
        """Coqui only has one quality level"""
        return [TTSModel.HD]  # Map to HD quality

    def estimate_cost(self, text: str) -> float:
        """Coqui is free (self-hosted)"""
        return 0.0

    def get_max_text_length(self) -> int:
        """Coqui supports longer text than OpenAI"""
        return 10000  # 10K chars

    def get_latency_estimate_ms(self, text: str) -> int:
        """
        Coqui latency on M4 Pro (with GPU):
        ~200-500ms depending on text length
        """
        return 200 + (len(text) // 10)  # ~200ms + 10ms per 10 chars
