"""Custom Pipecat processors for Ella AI integration."""

from .ella_config import EllaConfigProcessor
from .conversation_logger import ConversationLogger

__all__ = ["EllaConfigProcessor", "ConversationLogger"]
