"""Shared direct conversation-summary writeback with version and ledger provenance."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import database.conversations as conversations_db
from ella.services.summary_sanitizer import sanitize_summary_update
from models.conversation import CategoryEnum
from utils.ella.canonical_omi import write_omi_canonical_event

logger = logging.getLogger(__name__)


class ConversationSummaryNotFoundError(Exception):
    pass


class InvalidConversationSummaryCategoryError(Exception):
    pass


async def write_conversation_summary(
    *,
    uid: str,
    conversation_id: str,
    title: Optional[str] = None,
    overview: Optional[str] = None,
    emoji: Optional[str] = None,
    category: Optional[str] = None,
    summary_source: str = 'observer',
    summary_kind: str = 'observer_enriched',
    correction_id: Optional[str] = None,
    based_on_version_id: Optional[str] = None,
    set_active: bool = True,
    trace_id: Optional[str] = None,
    ella_tags: Optional[list[str]] = None,
    ella_signal: Optional[dict[str, Any]] = None,
    internal_assessment_fetcher: Optional[Callable[[str, str], Awaitable[Optional[dict]]]] = None,
    correction_audit_updater: Optional[Callable[[str, str, str, dict], None]] = None,
    canonical_writer: Callable[..., dict] = write_omi_canonical_event,
) -> dict[str, Any]:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise ConversationSummaryNotFoundError(conversation_id)

    enrichment_state = conversation.get('enrichment_state') or {}
    if (
        trace_id
        and enrichment_state.get('status') == 'writeback_applied'
        and enrichment_state.get('trace_id') == trace_id
    ):
        return {
            'status': 'ok',
            'conversation_id': conversation_id,
            'updated_fields': [],
            'active_summary_version_id': conversation.get('active_summary_version_id'),
            'sanitizer_warnings': [],
            'idempotent_replay': True,
        }

    sanitized = sanitize_summary_update(title=title, overview=overview, emoji=emoji, category=category)
    update_data: dict[str, Any] = {}
    if sanitized.title is not None:
        update_data['structured.title'] = sanitized.title
    if sanitized.overview is not None:
        update_data['structured.overview'] = sanitized.overview
        update_data['apps_results'] = []
        update_data['plugins_results'] = []
    if sanitized.emoji is not None:
        update_data['structured.emoji'] = sanitized.emoji
    if sanitized.category is not None:
        try:
            CategoryEnum(sanitized.category)
        except ValueError as error:
            raise InvalidConversationSummaryCategoryError(sanitized.category) from error
        update_data['structured.category'] = sanitized.category
    if not update_data:
        raise ValueError('No fields to update')

    structured = dict(conversation.get('structured') or {})
    if sanitized.title is not None:
        structured['title'] = sanitized.title
    if sanitized.overview is not None:
        structured['overview'] = sanitized.overview
    if sanitized.emoji is not None:
        structured['emoji'] = sanitized.emoji
    if sanitized.category is not None:
        structured['category'] = sanitized.category

    version_update = conversations_db.build_summary_version_update(
        conversation,
        next_structured=structured,
        source=summary_source,
        kind=summary_kind,
        correction_id=correction_id,
        based_on_version_id=based_on_version_id,
        activate=set_active,
    )
    state_updated_at = datetime.now(timezone.utc)
    update_data['summary_versions'] = version_update['summary_versions']
    update_data['active_summary_version_id'] = version_update['active_summary_version_id']
    update_data['enrichment_state'] = {
        'status': 'writeback_applied',
        'pending': False,
        'source': summary_source,
        'kind': summary_kind,
        'trace_id': trace_id,
        'updated_at': state_updated_at,
        'error': None,
    }

    if internal_assessment_fetcher:
        internal_assessment = await internal_assessment_fetcher(uid, conversation_id)
        if internal_assessment:
            update_data['internal_assessment'] = internal_assessment

    normalized_tags = []
    for tag in ella_tags or []:
        clean = str(tag).strip().lower().replace(' ', '_')
        if clean and len(clean) <= 64 and clean not in normalized_tags:
            normalized_tags.append(clean)
    if normalized_tags:
        update_data['ella_tags'] = normalized_tags[:12]
    if ella_signal is not None:
        update_data['ella_signal'] = ella_signal
    if correction_id:
        existing_state = conversation.get('correction_state') or {}
        update_data['correction_state'] = {
            'correction_id': correction_id,
            'status': 'applied',
            'pending': False,
            'source': existing_state.get('source'),
            'submitted_at': existing_state.get('submitted_at'),
            'updated_at': state_updated_at,
            'active_summary_version_id': version_update['active_summary_version_id'],
        }

    conversations_db.update_conversation(uid, conversation_id, update_data)
    canonical_base = dict(conversation)
    canonical_conversation = {
        **canonical_base,
        'id': conversation_id,
        'structured': structured,
        'summary_versions': version_update['summary_versions'],
        'active_summary_version_id': version_update['active_summary_version_id'],
        'enrichment_state': update_data['enrichment_state'],
        'internal_assessment': update_data.get('internal_assessment', canonical_base.get('internal_assessment')),
        'ella_tags': update_data.get('ella_tags', canonical_base.get('ella_tags') or []),
        'ella_signal': update_data.get('ella_signal', canonical_base.get('ella_signal')),
    }
    try:
        await asyncio.to_thread(
            canonical_writer,
            uid,
            canonical_conversation,
            summary_source=summary_source,
            summary_kind=summary_kind,
            trace_id=trace_id,
        )
    except Exception:
        logger.exception(
            'Failed to write conversation summary to canonical ledger',
            extra={'uid': uid, 'conversation_id': conversation_id, 'trace_id': trace_id},
        )

    if correction_id and correction_audit_updater:
        try:
            correction_audit_updater(
                uid,
                conversation_id,
                correction_id,
                {
                    'status': 'applied',
                    'updated_at': state_updated_at.isoformat(),
                    'applied_at': state_updated_at.isoformat(),
                    'applied_summary_version_id': version_update['active_summary_version_id'],
                    'summary_version_kind': summary_kind,
                    'summary_version_source': summary_source,
                },
            )
        except Exception:
            logger.exception(
                'Failed to update correction audit after summary apply',
                extra={'uid': uid, 'conversation_id': conversation_id, 'correction_id': correction_id},
            )

    return {
        'status': 'ok',
        'conversation_id': conversation_id,
        'updated_fields': list(update_data.keys()),
        'active_summary_version_id': version_update['active_summary_version_id'],
        'sanitizer_warnings': sanitized.warnings,
    }
