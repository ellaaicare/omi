"""Conversation vector write helpers shared by initial processing and recovery."""

import json

import database.notifications as notification_db
from database.vector_db import upsert_vector2, update_vector_metadata
from models.conversation import Conversation, ConversationSource, ExternalIntegrationConversationSource
from utils.llm.chat import (
    retrieve_metadata_fields_from_transcript,
    retrieve_metadata_from_message,
    retrieve_metadata_from_text,
)
from utils.llm.clients import generate_embedding


def save_structured_vector(uid: str, conversation: Conversation, update_only: bool = False):
    vector = generate_embedding(str(conversation.structured)) if not update_only else None
    timezone = notification_db.get_user_time_zone(uid)
    metadata = {}

    if conversation.source == ConversationSource.external_integration:
        external_data = conversation.external_data or {}
        text_source = external_data.get('text_source')
        text_content = external_data.get('text')
        if text_content:
            text_source_spec = external_data.get('text_source_spec')
            if text_source == ExternalIntegrationConversationSource.message.value:
                metadata = retrieve_metadata_from_message(
                    uid,
                    conversation.created_at,
                    text_content,
                    timezone,
                    text_source_spec,
                )
            elif text_source == ExternalIntegrationConversationSource.other.value:
                metadata = retrieve_metadata_from_text(
                    uid,
                    conversation.created_at,
                    text_content,
                    timezone,
                    text_source_spec,
                )
    else:
        segments = [segment.dict() for segment in conversation.transcript_segments]
        metadata = retrieve_metadata_fields_from_transcript(
            uid,
            conversation.created_at,
            segments,
            timezone,
            photos=conversation.photos,
        )

    metadata['created_at'] = int(conversation.created_at.timestamp())
    if not update_only:
        print('save_structured_vector creating vector')
        upsert_vector2(uid, conversation, vector, metadata)
    else:
        print('save_structured_vector updating metadata')
        update_vector_metadata(uid, conversation.id, metadata)


def refresh_structured_summary_vector(
    uid: str,
    conversation: Conversation,
    *,
    summary_version_id: str,
    summary_content_sha256: str,
):
    """Refresh only the summary embedding while preserving existing vector metadata."""

    from database.vector_db import fetch_conversation_vector_metadata, upsert_conversation_vector

    structured = conversation.structured
    if hasattr(structured, 'model_dump'):
        structured = structured.model_dump(mode='json')
    summary_payload = json.dumps(
        structured,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )
    vector = generate_embedding(summary_payload)
    metadata = dict(fetch_conversation_vector_metadata(uid, conversation.id) or {})
    metadata.update(
        {
            'uid': uid,
            'memory_id': conversation.id,
            'created_at': metadata.get('created_at') or int(conversation.created_at.timestamp()),
            'active_summary_version_id': summary_version_id,
            'summary_content_sha256': summary_content_sha256,
        }
    )
    return upsert_conversation_vector(uid, conversation.id, vector, metadata)
