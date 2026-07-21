from datetime import datetime, timezone

from models.conversation import Conversation, ConversationStatus

CONVERSATION_SUMMARY_FAILED = "conversation_summary_failed"
CONVERSATION_PROCESSING_FAILED = "conversation_processing_failed"


def apply_conversation_processing_failed(
    conversation: Conversation,
    error_code: str = CONVERSATION_SUMMARY_FAILED,
    failed_at: datetime | None = None,
) -> Conversation:
    conversation.status = ConversationStatus.failed
    conversation.discarded = False
    conversation.processing_error = error_code
    conversation.processing_error_at = failed_at or datetime.now(timezone.utc)
    return conversation


def clear_conversation_processing_error(conversation: Conversation) -> Conversation:
    conversation.processing_error = None
    conversation.processing_error_at = None
    return conversation
