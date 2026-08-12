"""Shared direct conversation-summary writeback with version and ledger provenance."""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import database.conversations as conversations_db
from ella.services.summary_sanitizer import sanitize_summary_update
from models.conversation import CategoryEnum
from utils.ella.canonical_omi import (
    TODAY_CARD_GROUNDING_ATTESTER,
    TODAY_CARD_GROUNDING_CONTRACT_VERSION,
    TODAY_CARD_PARALLEL_GROUNDING_ATTESTER,
    summary_grounding_hash,
    today_card_grounding_identity_is_valid,
    transcript_grounding_hash,
    write_omi_canonical_event,
)

logger = logging.getLogger(__name__)


class ConversationSummaryNotFoundError(Exception):
    pass


class InvalidConversationSummaryCategoryError(Exception):
    pass


class CanonicalSummaryWriteUnconfirmedError(RuntimeError):
    pass


class ConcurrentConversationSummaryChangeError(RuntimeError):
    pass


def _normalized_grounding_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def _conversation_transcript_segments(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    transcript_segments: list[dict[str, Any]] = []
    for segment in conversation.get('transcript_segments') or []:
        if isinstance(segment, dict):
            transcript_segments.append(dict(segment))
        elif hasattr(segment, 'model_dump'):
            transcript_segments.append(segment.model_dump(mode='json'))
        elif hasattr(segment, 'dict'):
            transcript_segments.append(segment.dict())
    return transcript_segments


def _parallel_grounding_attestation(
    evidence: dict[str, Any],
    *,
    uid: str,
    conversation_id: str,
    structured: dict[str, Any],
    transcript_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    allowed_keys = {
        'attester',
        'semantic_outcome',
        'supporting_quotes',
        'policy_version',
        'transcript_hash',
        'summary_request_id',
        'summary_response_id',
        'verifier_request_id',
        'verifier_response_id',
    }
    if (
        set(evidence) != allowed_keys
        or evidence.get('attester') != TODAY_CARD_PARALLEL_GROUNDING_ATTESTER
        or evidence.get('semantic_outcome') != 'supported'
        or evidence.get('policy_version') != 'hermes-parallel-grounding-verifier-v1'
    ):
        raise ValueError('today_card_grounding_evidence_invalid')
    transcript = _normalized_grounding_text(
        ' '.join(str(segment.get('text') or '') for segment in transcript_segments if isinstance(segment, dict))
    ).casefold()
    raw_quotes = evidence.get('supporting_quotes')
    if not isinstance(raw_quotes, list):
        raise ValueError('today_card_grounding_evidence_invalid')
    quotes = [_normalized_grounding_text(value) for value in raw_quotes]
    if not 1 <= len(quotes) <= 3 or any(not quote for quote in quotes):
        raise ValueError('today_card_grounding_evidence_invalid')
    if any(quote.casefold() not in transcript for quote in quotes):
        raise ValueError('today_card_grounding_quote_not_in_transcript')
    if any(sum(character.isalnum() for character in quote) < 8 for quote in quotes):
        raise ValueError('today_card_grounding_quote_too_short')
    observed_transcript_hash = str(evidence.get('transcript_hash') or '').strip()
    if observed_transcript_hash != transcript_grounding_hash(transcript_segments):
        raise ValueError('today_card_grounding_transcript_changed')
    receipt = {
        'contract_version': TODAY_CARD_GROUNDING_CONTRACT_VERSION,
        'attester': TODAY_CARD_PARALLEL_GROUNDING_ATTESTER,
        'semantic_outcome': 'supported',
        'transcript_hash': observed_transcript_hash,
        'summary_hash': summary_grounding_hash(structured),
        'supporting_quote_hashes': ['sha256:' + hashlib.sha256(quote.encode('utf-8')).hexdigest() for quote in quotes],
        'policy_version': 'hermes-parallel-grounding-verifier-v1',
        'owner_hash': 'sha256:' + hashlib.sha256(uid.encode('utf-8')).hexdigest(),
        'conversation_id_hash': 'sha256:' + hashlib.sha256(conversation_id.encode('utf-8')).hexdigest(),
        'summary_request_id': str(evidence.get('summary_request_id') or '').strip(),
        'summary_response_id': str(evidence.get('summary_response_id') or '').strip(),
        'verifier_request_id': str(evidence.get('verifier_request_id') or '').strip(),
        'verifier_response_id': str(evidence.get('verifier_response_id') or '').strip(),
    }
    if not today_card_grounding_identity_is_valid(receipt, 'hermes_parallel'):
        raise ValueError('today_card_grounding_identity_invalid')
    return receipt


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
    expected_transcript_hash: Optional[str] = None,
    require_source_match: bool = False,
    preserve_generated_results: bool = False,
    today_card_grounding: Optional[dict[str, Any]] = None,
    today_card_grounding_evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise ConversationSummaryNotFoundError(conversation_id)
    if require_source_match:
        if not trace_id or not expected_transcript_hash:
            raise ValueError('summary_source_match_fields_required')
        observed_transcript_hash = transcript_grounding_hash(_conversation_transcript_segments(conversation))
        if observed_transcript_hash != expected_transcript_hash:
            raise ConcurrentConversationSummaryChangeError('transcript_changed')
    if require_based_on_match and conversation.get('active_summary_version_id') != based_on_version_id:
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')

    enrichment_state = conversation.get('enrichment_state') or {}
    same_trace = bool(trace_id and enrichment_state.get('trace_id') == trace_id)
    if require_source_match and same_trace:
        if (
            enrichment_state.get('source_transcript_hash') != expected_transcript_hash
            or enrichment_state.get('source_active_summary_version_id') != based_on_version_id
        ):
            raise ConcurrentConversationSummaryChangeError('summary_source_changed')
    elif require_source_match and conversation.get('active_summary_version_id') != based_on_version_id:
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
    existing_grounding = enrichment_state.get('today_card_grounding')
    if (
        same_trace
        and summary_source == 'hermes_parallel'
        and isinstance(existing_grounding, dict)
        and existing_grounding.get('transcript_hash')
        != transcript_grounding_hash(_conversation_transcript_segments(conversation))
    ):
        raise ConcurrentConversationSummaryChangeError('transcript_changed')
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
        result_summary_version_id = str(enrichment_state.get('result_summary_version_id') or '').strip()
        if (
            not result_summary_version_id
            or str(conversation.get('active_summary_version_id') or '').strip() != result_summary_version_id
        ):
            raise ConcurrentConversationSummaryChangeError('summary_result_version_changed')
        confirmed_state = {
            **enrichment_state,
            'status': 'writeback_applied',
            'pending': False,
            'canonical_status': 'completed',
            'error': None,
            'updated_at': datetime.now(timezone.utc),
        }
        canonical_conversation = {
            **conversation,
            'id': conversation_id,
            'enrichment_state': confirmed_state,
        }
        try:
            canonical_result = await asyncio.to_thread(
                canonical_writer,
                uid,
                canonical_conversation,
                summary_source=summary_source,
                summary_kind=summary_kind,
                trace_id=trace_id,
            )
            if not isinstance(canonical_result, dict) or canonical_result.get('ok') is not True:
                raise RuntimeError('canonical_write_unconfirmed')
        except Exception as error:
            logger.exception(
                'Failed to confirm pending conversation summary in canonical ledger',
                extra={'uid': uid, 'conversation_id': conversation_id, 'trace_id': trace_id},
            )
            raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed') from error
        if not conversations_db.update_conversation_if_active_summary_version(
            uid,
            conversation_id,
            result_summary_version_id,
            {'enrichment_state': confirmed_state},
        ):
            raise ConcurrentConversationSummaryChangeError('summary_result_version_changed')
        return {
            'status': 'ok',
            'conversation_id': conversation_id,
            'updated_fields': ['enrichment_state'],
            'active_summary_version_id': conversation.get('active_summary_version_id'),
            'sanitizer_warnings': [],
            'idempotent_replay': True,
            'canonical_confirmed': True,
        }

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
    if today_card_grounding is not None and today_card_grounding_evidence is not None:
        raise ValueError('today_card_grounding_inputs_conflict')
    grounding_bound_from_evidence = False
    bound_today_card_grounding: Optional[dict[str, Any]] = None
    transcript_segments = _conversation_transcript_segments(conversation)
    if today_card_grounding_evidence is not None:
        if summary_source != 'hermes_parallel' or summary_kind != 'hermes_enriched' or not require_canonical:
            raise ValueError('today_card_grounding_evidence_scope_invalid')
        today_card_grounding = _parallel_grounding_attestation(
            today_card_grounding_evidence,
            uid=uid,
            conversation_id=conversation_id,
            structured=structured,
            transcript_segments=transcript_segments,
        )
        grounding_bound_from_evidence = True
    if today_card_grounding is not None:
        quote_hashes = today_card_grounding.get('supporting_quote_hashes')
        quote_hashes_are_valid = (
            isinstance(quote_hashes, list)
            and bool(quote_hashes)
            and all(
                isinstance(value, str)
                and value.startswith('sha256:')
                and len(value.removeprefix('sha256:')) == 64
                and all(character in '0123456789abcdef' for character in value.removeprefix('sha256:'))
                for value in quote_hashes
            )
        )
        if not (
            summary_source in {'hermes_cloud', 'hermes_parallel'}
            and (summary_source != 'hermes_parallel' or grounding_bound_from_evidence)
            and summary_kind == 'hermes_enriched'
            and today_card_grounding.get('contract_version') == TODAY_CARD_GROUNDING_CONTRACT_VERSION
            and today_card_grounding.get('attester')
            == (
                TODAY_CARD_GROUNDING_ATTESTER
                if summary_source == 'hermes_cloud'
                else TODAY_CARD_PARALLEL_GROUNDING_ATTESTER
            )
            and today_card_grounding.get('semantic_outcome') == 'supported'
            and today_card_grounding.get('transcript_hash') == transcript_grounding_hash(transcript_segments)
            and today_card_grounding.get('summary_hash') == summary_grounding_hash(structured)
            and today_card_grounding.get('owner_hash') == 'sha256:' + hashlib.sha256(uid.encode('utf-8')).hexdigest()
            and today_card_grounding.get('conversation_id_hash')
            == 'sha256:' + hashlib.sha256(conversation_id.encode('utf-8')).hexdigest()
            and today_card_grounding_identity_is_valid(today_card_grounding, summary_source)
            and today_card_grounding.get('policy_version')
            == (
                'hermes-cloud-grounding-verifier-v1'
                if summary_source == 'hermes_cloud'
                else 'hermes-parallel-grounding-verifier-v1'
            )
            and quote_hashes_are_valid
        ):
            raise ValueError('today_card_grounding_attestation_invalid')
        bound_today_card_grounding = {
            **today_card_grounding,
            'source_version_id': version_update['new_summary_version_id'],
        }
    state_updated_at = datetime.now(timezone.utc)
    update_data['summary_versions'] = version_update['summary_versions']
    update_data['active_summary_version_id'] = version_update['active_summary_version_id']
    update_data['enrichment_state'] = {
        'status': 'writeback_pending_canonical' if require_canonical else 'writeback_applied',
        'pending': require_canonical,
        'source': summary_source,
        'kind': summary_kind,
        'trace_id': trace_id,
        'result_summary_version_id': version_update['active_summary_version_id'],
        **(
            {
                'source_transcript_hash': expected_transcript_hash,
                'source_active_summary_version_id': based_on_version_id,
            }
            if require_source_match
            else {}
        ),
        'updated_at': state_updated_at,
        'error': None,
        'canonical_status': 'pending' if require_canonical else 'unconfirmed',
        'today_card_grounding': bound_today_card_grounding,
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

    if require_source_match:
        updated = conversations_db.update_conversation_if_transcript_hash(
            uid,
            conversation_id,
            expected_transcript_hash,
            update_data,
            expected_active_summary_version_id=based_on_version_id,
            match_active_summary_version=True,
        )
        if not updated:
            raise ConcurrentConversationSummaryChangeError('summary_source_changed')
    elif grounding_bound_from_evidence:
        updated = conversations_db.update_conversation_if_transcript_hash(
            uid,
            conversation_id,
            bound_today_card_grounding['transcript_hash'],
            update_data,
            expected_active_summary_version_id=(based_on_version_id if require_based_on_match else None),
        )
        if not updated:
            raise ConcurrentConversationSummaryChangeError('transcript_changed')
    elif require_based_on_match:
        updated = conversations_db.update_conversation_if_active_summary_version(
            uid,
            conversation_id,
            based_on_version_id,
            update_data,
        )
        if not updated:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
    else:
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
    confirmed_state = {
        **update_data['enrichment_state'],
        'status': 'writeback_applied',
        'pending': False,
        'canonical_status': 'completed',
        'error': None,
    }
    canonical_conversation['enrichment_state'] = confirmed_state
    canonical_result: Optional[dict[str, Any]] = None
    canonical_error: Optional[Exception] = None
    try:
        canonical_result = await asyncio.to_thread(
            canonical_writer,
            uid,
            canonical_conversation,
            summary_source=summary_source,
            summary_kind=summary_kind,
            trace_id=trace_id,
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
        result_summary_version_id = version_update['active_summary_version_id']
        if canonical_error is not None:
            failed_state = {
                **update_data['enrichment_state'],
                'canonical_status': 'failed',
                'error': 'canonical_write_unconfirmed',
                'updated_at': datetime.now(timezone.utc),
            }
            if not conversations_db.update_conversation_if_active_summary_version(
                uid,
                conversation_id,
                result_summary_version_id,
                {'enrichment_state': failed_state},
            ):
                raise ConcurrentConversationSummaryChangeError('summary_result_version_changed') from canonical_error
            raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed') from canonical_error
        if not conversations_db.update_conversation_if_active_summary_version(
            uid,
            conversation_id,
            result_summary_version_id,
            {'enrichment_state': confirmed_state},
        ):
            raise ConcurrentConversationSummaryChangeError('summary_result_version_changed')

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
