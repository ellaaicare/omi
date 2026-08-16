"""Shared direct conversation-summary writeback with version and ledger provenance."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import database.conversations as conversations_db
from ella.services.canonical_summary_source import (
    ELLA_CANONICAL_SOURCE_CONTRACT,
    canonical_source_from_conversation,
    canonical_source_sha256,
)
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


class CanonicalConversationSourceMismatchError(RuntimeError):
    pass


class CanonicalSummaryDependencyUnavailableError(RuntimeError):
    pass


class CanonicalSummaryReconciliationPendingError(RuntimeError):
    pass


class ConversationSummaryOutcomeUnknownError(RuntimeError):
    pass


SUMMARY_WRITEBACK_RECEIPT_FIELD = 'summary_writeback_receipt'
SUMMARY_WRITEBACK_PENDING = 'pending_reconciliation'
SUMMARY_WRITEBACK_COMPLETED = 'completed'
SUMMARY_WRITEBACK_SUPERSEDED = 'superseded'
CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD = 'canonical_summary_publication_sequence'
CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD = 'canonical_summary_publication_sha256'
OPERATION_RECEIPT_PUBLIC_FIELDS = (
    'token',
    'status',
    'payload_sha256',
    'source_sha256',
    'source_version',
)


class CanonicalSummaryOperationConflictError(RuntimeError):
    pass


class _CanonicalSummaryRepairRetry(RuntimeError):
    pass


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _active_summary_version(conversation: dict[str, Any]) -> Optional[dict[str, Any]]:
    active_id = str(conversation.get('active_summary_version_id') or '')
    versions = conversation.get('summary_versions') or []
    for version in versions:
        if str(version.get('id') or '') == active_id:
            return version
    return next((version for version in versions if version.get('is_active')), None)


def _active_summary_version_sha256(conversation: dict[str, Any]) -> str:
    return _sha256_json(_active_summary_version(conversation) or {})


def _canonical_source_version(source: dict[str, Any]) -> str:
    return str(source.get('finished_at') or source.get('started_at') or '')


def _publication_post_image_sha256(conversation: dict[str, Any]) -> str:
    """Fence every stable conversation field published to the canonical event."""
    return _sha256_json(
        {
            'started_at': conversation.get('started_at'),
            'finished_at': conversation.get('finished_at'),
            'created_at': conversation.get('created_at'),
            'structured': conversation.get('structured') or {},
            'transcript_segments': conversation.get('transcript_segments') or [],
            'summary_versions': conversation.get('summary_versions') or [],
            'active_summary_version_id': conversation.get('active_summary_version_id'),
            'enrichment_state': conversation.get('enrichment_state') or {},
            'internal_assessment': conversation.get('internal_assessment') or {},
            'ella_tags': conversation.get('ella_tags') or [],
            'ella_signal': conversation.get('ella_signal') or {},
            'source': conversation.get('source'),
            'status': conversation.get('status'),
        }
    )


def _next_publication_sequence(conversation: dict[str, Any], receipt: Optional[dict[str, Any]] = None) -> int:
    candidates = [conversation.get(CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD)]
    if receipt:
        candidates.extend(
            (
                receipt.get('publication_sequence'),
                receipt.get('reconciliation_publication_sequence'),
            )
        )
    normalized = []
    for candidate in candidates:
        try:
            normalized.append(max(0, int(candidate or 0)))
        except (TypeError, ValueError):
            continue
    return max(normalized or [0]) + 1


def _receipt_matches_request(
    receipt: dict[str, Any],
    *,
    operation_token: str,
    payload_sha256: str,
    source_sha256: str,
    source_version: str,
) -> bool:
    return all(
        str(receipt.get(key) or '') == expected
        for key, expected in (
            ('token', operation_token),
            ('payload_sha256', payload_sha256),
            ('source_sha256', source_sha256),
            ('source_version', source_version),
        )
    )


def _receipt_matches_current_post_image(
    receipt: dict[str, Any],
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
) -> bool:
    current_source = canonical_source_from_conversation(
        uid=uid,
        conversation_id=conversation_id,
        conversation=conversation,
    )
    expected_post_image_sha256 = (
        receipt.get('completed_post_image_sha256')
        if receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED
        else receipt.get('post_image_sha256')
    )
    return (
        str(conversation.get('active_summary_version_id') or '') == str(receipt.get('active_summary_version_id') or '')
        and canonical_source_sha256(current_source) == receipt.get('canonical_source_post_image_sha256')
        and _publication_post_image_sha256(conversation) == expected_post_image_sha256
        and _active_summary_version_sha256(conversation) == receipt.get('active_summary_version_sha256')
    )


def _superseded_enrichment_state(
    conversation: dict[str, Any],
    receipt: dict[str, Any],
    *,
    updated_at: datetime,
) -> dict[str, Any]:
    current_state = dict(conversation.get('enrichment_state') or {})
    receipt_owns_state = (
        str(conversation.get('active_summary_version_id') or '') == str(receipt.get('active_summary_version_id') or '')
        and current_state.get('status') == 'writeback_pending_canonical'
        and current_state.get('trace_id') == receipt.get('trace_id')
        and current_state.get('source') == receipt.get('summary_source')
        and current_state.get('kind') == receipt.get('summary_kind')
    )
    if not receipt_owns_state:
        return current_state
    return {
        **current_state,
        'status': 'writeback_applied',
        'pending': False,
        'canonical_status': SUMMARY_WRITEBACK_SUPERSEDED,
        'error': 'canonical_summary_operation_superseded',
        'updated_at': updated_at,
    }


def _receipt_reconciliation_matches_current(
    receipt: dict[str, Any],
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
) -> bool:
    current_source = canonical_source_from_conversation(
        uid=uid,
        conversation_id=conversation_id,
        conversation=conversation,
    )
    return (
        _publication_post_image_sha256(conversation) == receipt.get('reconciliation_post_image_sha256')
        and str(conversation.get('active_summary_version_id') or '')
        == str(receipt.get('reconciliation_active_summary_version_id') or '')
        and _active_summary_version_sha256(conversation) == receipt.get('reconciliation_active_summary_version_sha256')
        and canonical_source_sha256(current_source) == receipt.get('reconciliation_canonical_source_sha256')
        and int(conversation.get(CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD) or 0)
        == int(receipt.get('reconciliation_publication_sequence') or -1)
        and str(conversation.get(CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD) or '')
        == str(receipt.get('reconciliation_publication_sha256') or '')
    )


def _public_operation_receipt(receipt: dict[str, Any]) -> dict[str, str]:
    return {key: str(receipt.get(key) or '') for key in OPERATION_RECEIPT_PUBLIC_FIELDS}


def _cas_result(
    *,
    conversation_id: str,
    receipt: dict[str, Any],
    sanitizer_warnings: list[str],
    idempotent_replay: bool,
) -> dict[str, Any]:
    return {
        'status': receipt.get('status'),
        'operation_receipt': _public_operation_receipt(receipt),
    }


async def _reconcile_superseded_canonical_publication(
    *,
    uid: str,
    conversation_id: str,
    operation_token: str,
    payload_sha256: str,
    source_sha256: str,
    source_version: str,
    canonical_writer: Callable[..., dict],
    fallback_summary_source: str,
    fallback_summary_kind: str,
    fallback_trace_id: Optional[str],
) -> dict[str, Any]:
    """Restore the current canonical image before terminally superseding a stale receipt."""
    last_receipt: dict[str, Any] = {}
    for _attempt in range(3):
        reconciliation_at = datetime.now(timezone.utc)

        def prepare(current: dict[str, Any]):
            current_receipt = current.get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
            if current_receipt.get('token') != operation_token or not _receipt_matches_request(
                current_receipt,
                operation_token=operation_token,
                payload_sha256=payload_sha256,
                source_sha256=source_sha256,
                source_version=source_version,
            ):
                raise CanonicalSummaryOperationConflictError('canonical_summary_reconciliation_conflict')
            if current_receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED:
                return {}, {
                    'receipt': current_receipt,
                    'conversation': current,
                    'repair_required': False,
                    'correction_audit': None,
                }
            if current_receipt.get('status') == SUMMARY_WRITEBACK_SUPERSEDED:
                raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
            if current_receipt.get('status') != SUMMARY_WRITEBACK_PENDING:
                raise CanonicalSummaryOperationConflictError('canonical_summary_reconciliation_conflict')

            if _receipt_reconciliation_matches_current(
                current_receipt,
                uid=uid,
                conversation_id=conversation_id,
                conversation=current,
            ):
                terminal_state = dict(current_receipt.get('reconciliation_terminal_enrichment_state') or {})
                canonical_conversation = {
                    **current,
                    'id': conversation_id,
                    'enrichment_state': terminal_state,
                }
                return {}, {
                    'receipt': current_receipt,
                    'conversation': canonical_conversation,
                    'repair_required': True,
                    'correction_audit': None,
                }

            publication_sequence = _next_publication_sequence(current, current_receipt)
            source_post_image_sha256 = _publication_post_image_sha256(current)
            terminal_state = _superseded_enrichment_state(
                current,
                current_receipt,
                updated_at=reconciliation_at,
            )
            canonical_conversation = {
                **current,
                'id': conversation_id,
                'enrichment_state': terminal_state,
                CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD: publication_sequence,
            }
            publication_sha256 = _publication_post_image_sha256(canonical_conversation)
            canonical_conversation[CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD] = publication_sha256
            current_source = canonical_source_from_conversation(
                uid=uid,
                conversation_id=conversation_id,
                conversation=current,
            )
            reconciliation_receipt = {
                **current_receipt,
                'canonical_status': 'supersession_pending',
                'reconciliation_post_image_sha256': source_post_image_sha256,
                'reconciliation_terminal_post_image_sha256': publication_sha256,
                'reconciliation_terminal_enrichment_state': terminal_state,
                'reconciliation_active_summary_version_id': current.get('active_summary_version_id'),
                'reconciliation_active_summary_version_sha256': _active_summary_version_sha256(current),
                'reconciliation_canonical_source_sha256': canonical_source_sha256(current_source),
                'reconciliation_publication_sequence': publication_sequence,
                'reconciliation_publication_sha256': publication_sha256,
                'updated_at': reconciliation_at,
            }
            canonical_conversation[SUMMARY_WRITEBACK_RECEIPT_FIELD] = reconciliation_receipt
            return {
                CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD: publication_sequence,
                CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD: publication_sha256,
                SUMMARY_WRITEBACK_RECEIPT_FIELD: reconciliation_receipt,
            }, {
                'receipt': reconciliation_receipt,
                'conversation': canonical_conversation,
                'repair_required': True,
                'correction_audit': None,
            }

        prepared = conversations_db.update_conversation_with_builder(uid, conversation_id, prepare)
        if prepared is None:
            raise ConversationSummaryNotFoundError(conversation_id)
        repair = prepared['result']
        last_receipt = repair['receipt']
        if not repair['repair_required']:
            return last_receipt

        canonical_conversation = repair['conversation']
        repair_state = canonical_conversation.get('enrichment_state') or {}
        try:
            canonical_result = await asyncio.to_thread(
                canonical_writer,
                uid,
                canonical_conversation,
                summary_source=repair_state.get('source') or fallback_summary_source,
                summary_kind=repair_state.get('kind') or fallback_summary_kind,
                trace_id=repair_state.get('trace_id') or fallback_trace_id,
            )
            if not isinstance(canonical_result, dict) or canonical_result.get('ok') is not True:
                raise RuntimeError('canonical_write_unconfirmed')
        except Exception:
            logger.error('Canonical summary supersession remains pending after repair publication failure')
            return last_receipt

        superseded_at = datetime.now(timezone.utc)

        def terminalize(current: dict[str, Any]):
            current_receipt = current.get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
            if current_receipt.get('token') != operation_token or not _receipt_matches_request(
                current_receipt,
                operation_token=operation_token,
                payload_sha256=payload_sha256,
                source_sha256=source_sha256,
                source_version=source_version,
            ):
                raise CanonicalSummaryOperationConflictError('canonical_summary_reconciliation_conflict')
            if current_receipt.get('status') == SUMMARY_WRITEBACK_SUPERSEDED:
                raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
            if current_receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED:
                return {}, {
                    'receipt': current_receipt,
                    'conversation': current,
                    'correction_audit': None,
                }
            if current_receipt.get(
                'status'
            ) != SUMMARY_WRITEBACK_PENDING or not _receipt_reconciliation_matches_current(
                current_receipt,
                uid=uid,
                conversation_id=conversation_id,
                conversation=current,
            ):
                raise _CanonicalSummaryRepairRetry('canonical_summary_repair_target_changed')
            terminal_state = dict(current_receipt.get('reconciliation_terminal_enrichment_state') or {})
            if _publication_post_image_sha256({**current, 'enrichment_state': terminal_state}) != current_receipt.get(
                'reconciliation_terminal_post_image_sha256'
            ):
                raise _CanonicalSummaryRepairRetry('canonical_summary_repair_terminal_image_changed')
            superseded_receipt = {
                **current_receipt,
                'status': SUMMARY_WRITEBACK_SUPERSEDED,
                'canonical_status': SUMMARY_WRITEBACK_SUPERSEDED,
                'updated_at': superseded_at,
            }
            return {
                'enrichment_state': terminal_state,
                SUMMARY_WRITEBACK_RECEIPT_FIELD: superseded_receipt,
            }, {
                'receipt': superseded_receipt,
                'conversation': current,
                'correction_audit': None,
            }

        try:
            terminalized = conversations_db.update_conversation_with_builder(uid, conversation_id, terminalize)
            if terminalized is None:
                raise ConversationSummaryOutcomeUnknownError('canonical_summary_supersession_missing')
        except _CanonicalSummaryRepairRetry:
            continue
        except CanonicalSummaryOperationConflictError:
            raise
        except Exception:
            logger.error('Canonical summary repair was published but terminal supersession remains unconfirmed')
            return last_receipt
        raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')

    logger.error('Canonical summary kept pending because its repair target changed repeatedly')
    return last_receipt


async def write_conversation_summary_cas(
    *,
    uid: str,
    conversation_id: str,
    expected_canonical_source_sha256: str,
    operation_token: str,
    source_version: str,
    payload_sha256: str,
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
    canonical_writer: Callable[..., dict] = write_omi_canonical_event,
    canonical_preflight: Optional[Callable[[str], None]] = None,
    preserve_generated_results: bool = False,
) -> dict[str, Any]:
    """Commit a source CAS with a durable receipt, then reconcile canonical storage."""
    sanitized = sanitize_summary_update(title=title, overview=overview, emoji=emoji, category=category)
    if sanitized.category is not None:
        try:
            CategoryEnum(sanitized.category)
        except ValueError as error:
            raise InvalidConversationSummaryCategoryError(sanitized.category) from error
    if all(value is None for value in (sanitized.title, sanitized.overview, sanitized.emoji, sanitized.category)):
        raise ValueError('No fields to update')

    if canonical_preflight is not None:
        try:
            canonical_preflight(uid)
        except Exception as error:
            raise CanonicalSummaryDependencyUnavailableError('canonical_summary_dependency_unavailable') from error

    internal_assessment = None
    if internal_assessment_fetcher:
        internal_assessment = await internal_assessment_fetcher(uid, conversation_id)

    normalized_tags: list[str] = []
    for tag in ella_tags or []:
        clean = str(tag).strip().lower().replace(' ', '_')
        if clean and len(clean) <= 64 and clean not in normalized_tags:
            normalized_tags.append(clean)
    normalized_tags = normalized_tags[:12]
    state_updated_at = datetime.now(timezone.utc)

    def build_update(conversation: dict[str, Any]):
        existing_receipt = conversation.get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
        if existing_receipt.get('token') == operation_token:
            if existing_receipt.get('status') == SUMMARY_WRITEBACK_SUPERSEDED:
                raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
            if existing_receipt.get('status') not in {SUMMARY_WRITEBACK_PENDING, SUMMARY_WRITEBACK_COMPLETED}:
                raise CanonicalSummaryOperationConflictError('operation_receipt_invalid')
            if not _receipt_matches_request(
                existing_receipt,
                operation_token=operation_token,
                payload_sha256=payload_sha256,
                source_sha256=expected_canonical_source_sha256,
                source_version=source_version,
            ):
                raise CanonicalSummaryOperationConflictError('operation_token_reused')
            post_image_matches = _receipt_matches_current_post_image(
                existing_receipt,
                uid=uid,
                conversation_id=conversation_id,
                conversation=conversation,
            )
            if existing_receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED and not post_image_matches:
                raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
            return {}, {
                'receipt': existing_receipt,
                'conversation': conversation,
                'correction_audit': None,
                'idempotent_replay': True,
                'repair_required': not post_image_matches,
            }
        if existing_receipt.get('status') == SUMMARY_WRITEBACK_PENDING:
            raise CanonicalSummaryReconciliationPendingError('canonical_summary_reconciliation_pending')

        current_source = canonical_source_from_conversation(
            uid=uid,
            conversation_id=conversation_id,
            conversation=conversation,
        )
        if canonical_source_sha256(current_source) != expected_canonical_source_sha256:
            raise CanonicalConversationSourceMismatchError('canonical_source_changed')
        if _canonical_source_version(current_source) != source_version:
            raise CanonicalConversationSourceMismatchError('canonical_source_version_changed')

        structured = dict(conversation.get('structured') or {})
        update_data: dict[str, Any] = {}
        if sanitized.title is not None:
            structured['title'] = sanitized.title
            update_data['structured.title'] = sanitized.title
        if sanitized.overview is not None:
            structured['overview'] = sanitized.overview
            update_data['structured.overview'] = sanitized.overview
            if not preserve_generated_results:
                update_data['apps_results'] = []
                update_data['plugins_results'] = []
        if sanitized.emoji is not None:
            structured['emoji'] = sanitized.emoji
            update_data['structured.emoji'] = sanitized.emoji
        if sanitized.category is not None:
            structured['category'] = sanitized.category
            update_data['structured.category'] = sanitized.category

        version_update = conversations_db.build_summary_version_update(
            conversation,
            next_structured=structured,
            source=summary_source,
            kind=summary_kind,
            correction_id=correction_id,
            based_on_version_id=based_on_version_id,
            activate=set_active,
        )
        update_data['summary_versions'] = version_update['summary_versions']
        update_data['active_summary_version_id'] = version_update['active_summary_version_id']
        update_data['enrichment_state'] = {
            'status': 'writeback_pending_canonical',
            'pending': True,
            'source': summary_source,
            'kind': summary_kind,
            'trace_id': trace_id,
            'updated_at': state_updated_at,
            'error': None,
            'canonical_status': 'pending',
        }
        if internal_assessment:
            update_data['internal_assessment'] = internal_assessment
        if normalized_tags:
            update_data['ella_tags'] = normalized_tags
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
        correction_audit = None
        if correction_id:
            correction_audit = {
                'status': 'applied',
                'updated_at': state_updated_at.isoformat(),
                'applied_at': state_updated_at.isoformat(),
                'applied_summary_version_id': version_update['active_summary_version_id'],
                'summary_version_kind': summary_kind,
                'summary_version_source': summary_source,
            }
        canonical_conversation = {
            **conversation,
            'id': conversation_id,
            'structured': structured,
            'summary_versions': version_update['summary_versions'],
            'active_summary_version_id': version_update['active_summary_version_id'],
            'enrichment_state': update_data['enrichment_state'],
            'internal_assessment': update_data.get('internal_assessment', conversation.get('internal_assessment')),
            'ella_tags': update_data.get('ella_tags', conversation.get('ella_tags') or []),
            'ella_signal': update_data.get('ella_signal', conversation.get('ella_signal')),
        }
        publication_sequence = _next_publication_sequence(conversation, existing_receipt)
        post_image_source = canonical_source_from_conversation(
            uid=uid,
            conversation_id=conversation_id,
            conversation=canonical_conversation,
        )
        post_image_sha256 = _publication_post_image_sha256(canonical_conversation)
        confirmed_state = {
            **update_data['enrichment_state'],
            'status': 'writeback_applied',
            'pending': False,
            'canonical_status': 'completed',
            'error': None,
            'updated_at': state_updated_at,
        }
        completed_conversation = {
            **canonical_conversation,
            'enrichment_state': confirmed_state,
        }
        receipt = {
            'contract': ELLA_CANONICAL_SOURCE_CONTRACT,
            'token': operation_token,
            'status': SUMMARY_WRITEBACK_PENDING,
            'payload_sha256': payload_sha256,
            'source_sha256': expected_canonical_source_sha256,
            'source_version': source_version,
            'canonical_status': 'pending',
            'correction_audit_status': 'applied' if correction_id else 'not_requested',
            'summary_source': summary_source,
            'summary_kind': summary_kind,
            'trace_id': trace_id,
            'active_summary_version_id': version_update['active_summary_version_id'],
            'active_summary_version_sha256': _active_summary_version_sha256(canonical_conversation),
            'canonical_source_post_image_sha256': canonical_source_sha256(post_image_source),
            'post_image_sha256': post_image_sha256,
            'completed_post_image_sha256': _publication_post_image_sha256(completed_conversation),
            'publication_sequence': publication_sequence,
            'updated_fields': list(update_data.keys()),
            'created_at': state_updated_at,
            'updated_at': state_updated_at,
        }
        update_data[CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD] = publication_sequence
        update_data[CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD] = post_image_sha256
        update_data[SUMMARY_WRITEBACK_RECEIPT_FIELD] = receipt
        canonical_conversation[CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD] = publication_sequence
        canonical_conversation[CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD] = post_image_sha256
        canonical_conversation[SUMMARY_WRITEBACK_RECEIPT_FIELD] = receipt
        return update_data, {
            'receipt': receipt,
            'conversation': canonical_conversation,
            'correction_audit': correction_audit,
            'idempotent_replay': False,
            'repair_required': False,
        }

    try:
        transaction_result = conversations_db.update_conversation_with_builder(
            uid,
            conversation_id,
            build_update,
            correction_id=correction_id,
        )
    except (
        CanonicalConversationSourceMismatchError,
        CanonicalSummaryOperationConflictError,
        CanonicalSummaryReconciliationPendingError,
    ):
        raise
    except Exception as error:
        try:
            conversation = conversations_db.get_conversation(uid, conversation_id)
        except Exception as read_error:
            raise ConversationSummaryOutcomeUnknownError('conversation_summary_outcome_unknown') from read_error
        receipt = (conversation or {}).get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
        if receipt.get('token') != operation_token:
            if conversation is not None:
                current_source = canonical_source_from_conversation(
                    uid=uid,
                    conversation_id=conversation_id,
                    conversation=conversation,
                )
                if (
                    canonical_source_sha256(current_source) != expected_canonical_source_sha256
                    or _canonical_source_version(current_source) != source_version
                ):
                    raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded') from error
            raise ConversationSummaryOutcomeUnknownError('conversation_summary_outcome_unknown') from error
        if not _receipt_matches_request(
            receipt,
            operation_token=operation_token,
            payload_sha256=payload_sha256,
            source_sha256=expected_canonical_source_sha256,
            source_version=source_version,
        ):
            raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded') from error
        post_image_matches = _receipt_matches_current_post_image(
            receipt,
            uid=uid,
            conversation_id=conversation_id,
            conversation=conversation,
        )
        if receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED and not post_image_matches:
            raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded') from error
        transaction_result = {
            'conversation': conversation,
            'update_data': {},
            'result': {
                'receipt': receipt,
                'conversation': conversation,
                'correction_audit': None,
                'idempotent_replay': True,
                'repair_required': not post_image_matches,
            },
        }
    if transaction_result is None:
        raise ConversationSummaryNotFoundError(conversation_id)

    result = transaction_result['result']
    receipt = result['receipt']
    idempotent_replay = bool(result['idempotent_replay'])
    if result.get('repair_required'):
        repaired_receipt = await _reconcile_superseded_canonical_publication(
            uid=uid,
            conversation_id=conversation_id,
            operation_token=operation_token,
            payload_sha256=payload_sha256,
            source_sha256=expected_canonical_source_sha256,
            source_version=source_version,
            canonical_writer=canonical_writer,
            fallback_summary_source=summary_source,
            fallback_summary_kind=summary_kind,
            fallback_trace_id=trace_id,
        )
        return _cas_result(
            conversation_id=conversation_id,
            receipt=repaired_receipt,
            sanitizer_warnings=sanitized.warnings,
            idempotent_replay=idempotent_replay,
        )
    if receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED:
        return _cas_result(
            conversation_id=conversation_id,
            receipt=receipt,
            sanitizer_warnings=sanitized.warnings,
            idempotent_replay=idempotent_replay,
        )

    canonical_conversation = result['conversation']
    if not _receipt_matches_current_post_image(
        receipt,
        uid=uid,
        conversation_id=conversation_id,
        conversation=canonical_conversation,
    ):
        raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
    confirmed_state = {
        **(canonical_conversation.get('enrichment_state') or {}),
        'status': 'writeback_applied',
        'pending': False,
        'canonical_status': 'completed',
        'error': None,
        'updated_at': receipt.get('created_at'),
    }
    canonical_conversation['enrichment_state'] = confirmed_state
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
    except Exception:
        logger.error('CAS summary committed with canonical reconciliation pending')
        return _cas_result(
            conversation_id=conversation_id,
            receipt=receipt,
            sanitizer_warnings=sanitized.warnings,
            idempotent_replay=idempotent_replay,
        )

    completed_at = datetime.now(timezone.utc)

    def finalize(current: dict[str, Any]):
        current_receipt = current.get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
        if current_receipt.get('token') != operation_token or not _receipt_matches_request(
            current_receipt,
            operation_token=operation_token,
            payload_sha256=payload_sha256,
            source_sha256=expected_canonical_source_sha256,
            source_version=source_version,
        ):
            raise CanonicalSummaryOperationConflictError('canonical_summary_finalize_conflict')
        if current_receipt.get('status') == SUMMARY_WRITEBACK_COMPLETED:
            return {}, {
                'receipt': current_receipt,
                'conversation': current,
                'correction_audit': None,
                'idempotent_replay': True,
                'repair_required': False,
            }
        if current_receipt.get('status') == SUMMARY_WRITEBACK_SUPERSEDED:
            raise CanonicalSummaryOperationConflictError('canonical_summary_operation_superseded')
        if current_receipt.get('status') != SUMMARY_WRITEBACK_PENDING:
            raise CanonicalSummaryOperationConflictError('canonical_summary_finalize_conflict')
        if not _receipt_matches_current_post_image(
            current_receipt,
            uid=uid,
            conversation_id=conversation_id,
            conversation=current,
        ):
            return {}, {
                'receipt': current_receipt,
                'conversation': current,
                'correction_audit': None,
                'idempotent_replay': idempotent_replay,
                'repair_required': True,
            }
        completed_receipt = {
            **current_receipt,
            'status': SUMMARY_WRITEBACK_COMPLETED,
            'canonical_status': 'completed',
            'updated_at': completed_at,
        }
        return {
            'enrichment_state': confirmed_state,
            SUMMARY_WRITEBACK_RECEIPT_FIELD: completed_receipt,
        }, {
            'receipt': completed_receipt,
            'conversation': current,
            'correction_audit': None,
            'idempotent_replay': idempotent_replay,
            'repair_required': False,
        }

    try:
        finalized = conversations_db.update_conversation_with_builder(uid, conversation_id, finalize)
        if finalized is None:
            raise ConversationSummaryOutcomeUnknownError('canonical_summary_finalize_missing')
    except CanonicalSummaryOperationConflictError:
        raise
    except Exception:
        logger.error('Canonical summary durable receipt remains pending after ledger confirmation')
        return _cas_result(
            conversation_id=conversation_id,
            receipt=receipt,
            sanitizer_warnings=sanitized.warnings,
            idempotent_replay=idempotent_replay,
        )

    if finalized['result'].get('repair_required'):
        repaired_receipt = await _reconcile_superseded_canonical_publication(
            uid=uid,
            conversation_id=conversation_id,
            operation_token=operation_token,
            payload_sha256=payload_sha256,
            source_sha256=expected_canonical_source_sha256,
            source_version=source_version,
            canonical_writer=canonical_writer,
            fallback_summary_source=summary_source,
            fallback_summary_kind=summary_kind,
            fallback_trace_id=trace_id,
        )
        return _cas_result(
            conversation_id=conversation_id,
            receipt=repaired_receipt,
            sanitizer_warnings=sanitized.warnings,
            idempotent_replay=idempotent_replay,
        )

    return _cas_result(
        conversation_id=conversation_id,
        receipt=finalized['result']['receipt'],
        sanitizer_warnings=sanitized.warnings,
        idempotent_replay=idempotent_replay,
    )


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
    receipt = conversation.get(SUMMARY_WRITEBACK_RECEIPT_FIELD) or {}
    if receipt.get('status') == SUMMARY_WRITEBACK_PENDING:
        raise CanonicalSummaryReconciliationPendingError('canonical_summary_reconciliation_pending')
    if require_based_on_match and conversation.get('active_summary_version_id') != based_on_version_id:
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')

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
    # A completed CAS receipt is superseded by this later legacy write. A
    # pending receipt is rejected transactionally by the database helper.
    update_data[SUMMARY_WRITEBACK_RECEIPT_FIELD] = None
    confirmed_state = {
        **update_data['enrichment_state'],
        'status': 'writeback_applied',
        'pending': False,
        'canonical_status': 'completed',
        'error': None,
    }
    canonical_base = dict(conversation)
    canonical_conversation = {
        **canonical_base,
        'id': conversation_id,
        'structured': structured,
        'summary_versions': version_update['summary_versions'],
        'active_summary_version_id': version_update['active_summary_version_id'],
        'enrichment_state': confirmed_state,
        'internal_assessment': update_data.get('internal_assessment', canonical_base.get('internal_assessment')),
        'ella_tags': update_data.get('ella_tags', canonical_base.get('ella_tags') or []),
        'ella_signal': update_data.get('ella_signal', canonical_base.get('ella_signal')),
    }
    confirmed_correction_state: Optional[dict[str, Any]] = None
    if correction_id:
        confirmed_correction_state = {
            **update_data['correction_state'],
            'status': 'applied',
            'pending': False,
        }
        canonical_conversation['correction_state'] = confirmed_correction_state
    publication_sequence = _next_publication_sequence(conversation, receipt)
    publication_sha256 = _publication_post_image_sha256(canonical_conversation)
    update_data[CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD] = publication_sequence
    update_data[CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD] = publication_sha256
    canonical_conversation[CANONICAL_SUMMARY_PUBLICATION_SEQUENCE_FIELD] = publication_sequence
    canonical_conversation[CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD] = publication_sha256

    if source_mutation_guard is not None:
        await asyncio.to_thread(source_mutation_guard)
    try:
        if require_based_on_match and correction_source_compare_and_set is not None:
            update_outcome = await asyncio.to_thread(correction_source_compare_and_set, update_data)
            updated = update_outcome == 'updated'
        else:
            updated = await asyncio.to_thread(
                conversations_db.update_conversation_if_active_summary_version,
                uid,
                conversation_id,
                based_on_version_id if require_based_on_match else conversation.get('active_summary_version_id'),
                update_data,
            )
    except conversations_db.PendingConversationSummaryReconciliationError as error:
        raise CanonicalSummaryReconciliationPendingError('canonical_summary_reconciliation_pending') from error
    if not updated:
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
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
