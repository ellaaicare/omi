"""
Pipecat Voice Mode Integration for Ella AI.

This module is designed to be extractable to a separate package.
All Pipecat-specific code is self-contained here.

Usage:
    from integrations.pipecat import create_voice_pipeline, run_voice_session

    async def voice_endpoint(websocket, uid):
        await run_voice_session(websocket, uid)

License:
    Pipecat is licensed under BSD 2-Clause License.
    See THIRD_PARTY_LICENSES.md for details.
"""

from .pipeline.builder import create_voice_pipeline, run_voice_session
from .pipeline.config import PipelineConfig, VADConfig, STTConfig, TTSConfig, LLMConfig, GrokV2VConfig
from .pipeline.grok_v2v_pipeline import run_grok_v2v_session

__all__ = [
    "create_voice_pipeline",
    "run_voice_session",
    "run_grok_v2v_session",
    "PipelineConfig",
    "VADConfig",
    "STTConfig",
    "TTSConfig",
    "LLMConfig",
    "GrokV2VConfig",
]

__version__ = "0.1.0"
