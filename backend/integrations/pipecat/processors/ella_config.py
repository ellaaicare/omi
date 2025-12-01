"""
Ella configuration processor for Pipecat pipeline.

Injects system prompt and memory context from n8n into the LLM context.
"""

from pipecat.frames.frames import Frame, LLMMessagesFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class EllaConfigProcessor(FrameProcessor):
    """
    Custom processor that injects Ella's persona and memory context.

    This processor monitors the pipeline and can modify LLM message frames
    to ensure Ella's personality and user context are maintained.

    Note: The main system prompt injection happens in the pipeline builder.
    This processor is available for additional context manipulation if needed.
    """

    def __init__(self, ella_config: dict):
        """
        Initialize with Ella configuration from n8n.

        Args:
            ella_config: Configuration dict from n8n voice-config endpoint
        """
        super().__init__()
        self.ella_config = ella_config
        self.agent_config = ella_config.get("agent_config", {})
        self.user_info = ella_config.get("user", {})

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """
        Process frames passing through the pipeline.

        Currently passes all frames through unchanged.
        Can be extended to:
        - Modify LLM parameters based on context
        - Inject additional context mid-conversation
        - Filter or transform messages

        Args:
            frame: The frame to process
            direction: Direction the frame is traveling
        """
        # For now, pass through unchanged
        # The system prompt is injected in the pipeline builder
        await self.push_frame(frame, direction)

    def get_user_name(self) -> str:
        """Get the user's name from config."""
        return self.user_info.get("name", "there")

    def get_model(self) -> str:
        """Get the LLM model from config."""
        return self.agent_config.get("model", "llama-3.3-70b-versatile")

    def get_temperature(self) -> float:
        """Get the LLM temperature from config."""
        return self.agent_config.get("temperature", 0.7)
