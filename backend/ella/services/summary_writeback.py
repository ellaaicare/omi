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


class CanonicalSummaryWriteUnconfirmedError(RuntimeError):
    pass


class ConcurrentConversationSummaryChangeError(RuntimeError):
    pass


def _publish_canonical_summary(
    *,
    canonical_writer: Callable[..., dict],
    uid: str,
    canonical_conversation: dict[str, Any],
    summary_source: str,
    summary_kind: str,
    trace_id: Optional[str],
    canonical_egress_guard: Optional[Callable[[], Optional[bool | dict[str, Any]]]],
    canonical_egress_completion: Optional[Callable[[bool], None]],
    canonical_timeout_provider: Optional[Callable[[], float]],
) -> dict[str, Any]:
    """Fence, publish, and record completion within one uncancellable thread."""

    publication_authority = canonical_egress_guard() if canonical_egress_guard is not None else True
    if publication_authority is False:
        return {"ok": True, "inserted": 0, "duplicates": 1, "fenced_replay": True}

    writer_kwargs: dict[str, Any] = {
        "summary_source": summary_source,
        "summary_kind": summary_kind,
        "trace_id": trace_id,
    }
    if isinstance(publication_authority, dict):
        writer_kwargs["publication_fence"] = publication_authority
    try:
        if canonical_timeout_provider is not None:
            writer_kwargs["timeout"] = max(0.001, canonical_timeout_provider())
        result = canonical_writer(uid, canonical_conversation, **writer_kwargs)
    except Exception:
        if canonical_egress_completion is not None:
            canonical_egress_completion(False)
        raise

    confirmed = isinstance(result, dict) and result.get("ok") is True
    if canonical_egress_completion is not None:
        canonical_egress_completion(confirmed)
    return result


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
    require_canonical: bool = False,
    require_based_on_match: bool = False,
    preserve_generated_results: bool = False,
    canonical_egress_guard: Optional[Callable[[], Optional[bool | dict[str, Any]]]] = None,
    canonical_egress_completion: Optional[Callable[[bool], None]] = None,
    canonical_timeout_provider: Optional[Callable[[], float]] = None,
    correction_attempt_token: Optional[str] = None,
    correction_source_compare_and_set: Optional[Callable[[dict[str, Any]], str]] = None,
    source_mutation_guard: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    conversation = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if conversation is None:
        raise ConversationSummaryNotFoundError(conversation_id)

    enrichment_state = conversation.get('enrichment_state') or {}
    same_trace = bool(trace_id and enrichment_state.get('trace_id') == trace_id)
    if (
        same_trace
        and enrichment_state.get('status') == 'writeback_applied'
        and (not require_canonical or enrichment_state.get('canonical_status') == 'completed')
    ):
        return {
            'status': 'ok',
            'conversation_id': conversation_id,
            'updated_fields': [],
            'active_summary_version_id': conversation.get('active_summary_version_id'),
            'sanitizer_warnings': [],
            'idempotent_replay': True,
            'canonical_confirmed': enrichment_state.get('canonical_status') == 'completed',
        }

    if same_trace and require_canonical and enrichment_state.get('status') == 'writeback_pending_canonical':
        confirmed_state = {
            **enrichment_state,
            'status': 'writeback_applied',
            'pending': False,
            'canonical_status': 'completed',
            'error': None,
            # Keep the canonical payload byte-stable so transport ack loss can
            # replay the exact active version without mutating its event.
            'updated_at': enrichment_state.get('updated_at'),
        }
        canonical_conversation = {
            **conversation,
            'id': conversation_id,
            'enrichment_state': confirmed_state,
        }
        try:
            canonical_result = await asyncio.to_thread(
                _publish_canonical_summary,
                canonical_writer=canonical_writer,
                uid=uid,
                canonical_conversation=canonical_conversation,
                summary_source=summary_source,
                summary_kind=summary_kind,
                trace_id=trace_id,
                canonical_egress_guard=canonical_egress_guard,
                canonical_egress_completion=canonical_egress_completion,
                canonical_timeout_provider=canonical_timeout_provider,
            )
            if not isinstance(canonical_result, dict) or canonical_result.get('ok') is not True:
                raise RuntimeError('canonical_write_unconfirmed')
        except Exception as error:
            logger.exception(
                'Failed to confirm pending conversation summary in canonical ledger',
                extra={'uid': uid, 'conversation_id': conversation_id, 'trace_id': trace_id},
            )
            raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed') from error
        confirmed_update: dict[str, Any] = {'enrichment_state': confirmed_state}
        if correction_id:
            confirmed_update['correction_state'] = {
                **(conversation.get('correction_state') or {}),
                'correction_id': correction_id,
                'status': 'applied',
                'pending': False,
                'updated_at': confirmed_state['updated_at'],
                'active_summary_version_id': conversation.get('active_summary_version_id'),
            }
        try:
            if correction_source_compare_and_set is None:
                if source_mutation_guard is not None:
                    await asyncio.to_thread(source_mutation_guard)
                await asyncio.to_thread(conversations_db.update_conversation, uid, conversation_id, confirmed_update)
        except Exception:
            # The canonical writer already returned durable success. A source
            # confirmation marker is repairable and must not turn that commit
            # into a false failure or trigger another source version.
            logger.exception(
                'Canonical summary durable before pending marker confirmation',
                extra={'uid': uid, 'conversation_id': conversation_id, 'trace_id': trace_id},
            )
        return {
            'status': 'ok',
            'conversation_id': conversation_id,
            'updated_fields': ['enrichment_state'],
            'active_summary_version_id': conversation.get('active_summary_version_id'),
            'sanitizer_warnings': [],
            'idempotent_replay': True,
            'canonical_confirmed': True,
        }

    if require_based_on_match and conversation.get('active_summary_version_id') != based_on_version_id:
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')

    sanitized = sanitize_summary_update(title=title, overview=overview, emoji=emoji, category=category)
    update_data: dict[str, Any] = {}
    if sanitized.title is not None:
        update_data['structured.title'] = sanitized.title
    if sanitized.overview is not None:
        update_data['structured.overview'] = sanitized.overview
        if not preserve_generated_results:
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
        'status': 'writeback_pending_canonical' if require_canonical else 'writeback_applied',
        'pending': require_canonical,
        'source': summary_source,
        'kind': summary_kind,
        'trace_id': trace_id,
        'updated_at': state_updated_at,
        'error': None,
        'canonical_status': 'pending' if require_canonical else 'unconfirmed',
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
            'status': 'canonical_pending' if require_canonical else 'applied',
            'pending': require_canonical,
            'source': existing_state.get('source'),
            'submitted_at': existing_state.get('submitted_at'),
            'updated_at': state_updated_at,
            'active_summary_version_id': version_update['active_summary_version_id'],
            'retry_attempt_token': correction_attempt_token,
        }

    if require_based_on_match:
        if source_mutation_guard is not None:
            await asyncio.to_thread(source_mutation_guard)
        if correction_source_compare_and_set is not None:
            update_outcome = await asyncio.to_thread(correction_source_compare_and_set, update_data)
            updated = update_outcome == 'updated'
        else:
            updated = await asyncio.to_thread(
                conversations_db.update_conversation_if_active_summary_version,
                uid,
                conversation_id,
                based_on_version_id,
                update_data,
            )
        if not updated:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
    else:
        if source_mutation_guard is not None:
            await asyncio.to_thread(source_mutation_guard)
        await asyncio.to_thread(conversations_db.update_conversation, uid, conversation_id, update_data)
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
    confirmed_state = {
        **update_data['enrichment_state'],
        'status': 'writeback_applied',
        'pending': False,
        'canonical_status': 'completed',
        'error': None,
    }
    canonical_conversation['enrichment_state'] = confirmed_state
    confirmed_correction_state: Optional[dict[str, Any]] = None
    if correction_id:
        confirmed_correction_state = {
            **update_data['correction_state'],
            'status': 'applied',
            'pending': False,
        }
        canonical_conversation['correction_state'] = confirmed_correction_state
    canonical_result: Optional[dict[str, Any]] = None
    canonical_error: Optional[Exception] = None
    try:
        canonical_result = await asyncio.to_thread(
            _publish_canonical_summary,
            canonical_writer=canonical_writer,
            uid=uid,
            canonical_conversation=canonical_conversation,
            summary_source=summary_source,
            summary_kind=summary_kind,
            trace_id=trace_id,
            canonical_egress_guard=canonical_egress_guard,
            canonical_egress_completion=canonical_egress_completion,
            canonical_timeout_provider=canonical_timeout_provider,
        )
        if require_canonical and (not isinstance(canonical_result, dict) or canonical_result.get('ok') is not True):
            raise RuntimeError('canonical_write_unconfirmed')
    except Exception as error:
        canonical_error = error
        logger.exception(
            'Failed to write conversation summary to canonical ledger',
            extra={'uid': uid, 'conversation_id': conversation_id, 'trace_id': trace_id},
        )

    if require_canonical:
        if canonical_error is not None:
            failed_state = {
                **update_data['enrichment_state'],
                'canonical_status': 'failed',
                'error': 'canonical_write_unconfirmed',
                'updated_at': datetime.now(timezone.utc),
            }
            if correction_source_compare_and_set is None:
                if source_mutation_guard is not None:
                    await asyncio.to_thread(source_mutation_guard)
                await asyncio.to_thread(
                    conversations_db.update_conversation,
                    uid,
                    conversation_id,
                    {'enrichment_state': failed_state},
                )
            raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed') from canonical_error
        confirmed_update = {'enrichment_state': confirmed_state}
        if confirmed_correction_state is not None:
            confirmed_update['correction_state'] = confirmed_correction_state
        try:
            if correction_source_compare_and_set is None:
                if source_mutation_guard is not None:
                    await asyncio.to_thread(source_mutation_guard)
                await asyncio.to_thread(conversations_db.update_conversation, uid, conversation_id, confirmed_update)
        except Exception:
            logger.exception(
                'Canonical summary durable before source marker confirmation',
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
        'canonical_confirmed': bool(isinstance(canonical_result, dict) and canonical_result.get('ok') is True),
    }
