"""Shared direct conversation-summary writeback with version and ledger provenance."""

import asyncio
import copy
import hashlib
import json
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

_REQUEST_VALUE_UNSET = object()
_SUMMARY_ENRICHMENT_AUTHORITY_FIELDS = (
    'trace_id',
    'request_fingerprint',
    'status',
    'canonical_status',
    'source',
    'kind',
)
_CURRENT_REQUEST_FINGERPRINT_KEYS = frozenset(
    {
        'submitted_structured',
        'result_structured',
        'submitted_ella_tags',
        'result_ella_tags',
        'submitted_ella_signal',
        'result_ella_signal',
    }
)
_LEGACY_REQUEST_FINGERPRINT_KEYS = frozenset({'structured', 'ella_tags', 'ella_signal'})


class ConversationSummaryNotFoundError(Exception):
    pass


class InvalidConversationSummaryCategoryError(Exception):
    pass


class CanonicalSummaryWriteUnconfirmedError(RuntimeError):
    pass


class ConcurrentConversationSummaryChangeError(RuntimeError):
    pass


class CanonicalSummaryRepairExhaustedError(ConcurrentConversationSummaryChangeError):
    pass


class CanonicalSummaryRetryReceiptUnconfirmedError(RuntimeError):
    pass


def _active_summary_version(conversation: dict[str, Any]) -> dict[str, Any]:
    active_id = str(conversation.get('active_summary_version_id') or '').strip()
    for version in conversation.get('summary_versions') or []:
        if isinstance(version, dict) and str(version.get('id') or '').strip() == active_id:
            return version
    return {}


def _normalized_tags(raw_tags: Optional[list[str]], existing: Any = None) -> list[str]:
    if raw_tags is None:
        return list(existing) if isinstance(existing, list) else []
    tags: list[str] = []
    for tag in raw_tags:
        clean = str(tag).strip().lower().replace(' ', '_')
        if clean and len(clean) <= 64 and clean not in tags:
            tags.append(clean)
    return tags[:12]


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()


def _optional_json_fingerprint(value: Any) -> Optional[str]:
    return None if value is None else _json_fingerprint(value)


def _summary_request_fingerprint_input(
    *,
    structured: dict[str, Any],
    summary_source: str,
    summary_kind: str,
    correction_id: Optional[str],
    based_on_version_id: Optional[str],
    set_active: bool,
    require_canonical: bool,
    require_based_on_match: bool,
    preserve_generated_results: bool,
    ella_tags: list[str],
    ella_signal: Any,
    today_card_grounding: Any,
    today_card_grounding_evidence: Any,
    expected_transcript_hash: Optional[str] = None,
    require_source_match: bool = False,
    submitted_structured: Optional[dict[str, Any]] = None,
    submitted_ella_tags: Any = _REQUEST_VALUE_UNSET,
    submitted_ella_signal: Any = _REQUEST_VALUE_UNSET,
) -> dict[str, Any]:
    exact_submitted_structured = (
        submitted_structured
        if submitted_structured is not None
        else {key: structured.get(key) for key in ('title', 'overview', 'emoji', 'category')}
    )
    exact_submitted_tags = ella_tags if submitted_ella_tags is _REQUEST_VALUE_UNSET else submitted_ella_tags
    exact_submitted_signal = ella_signal if submitted_ella_signal is _REQUEST_VALUE_UNSET else submitted_ella_signal
    return {
        'submitted_structured': exact_submitted_structured,
        'result_structured': {key: structured.get(key) for key in ('title', 'overview', 'emoji', 'category')},
        'summary_source': summary_source,
        'summary_kind': summary_kind,
        'correction_id': correction_id,
        'based_on_version_id': based_on_version_id,
        'set_active': bool(set_active),
        'require_canonical': bool(require_canonical),
        'require_based_on_match': bool(require_based_on_match),
        'preserve_generated_results': bool(preserve_generated_results),
        'submitted_ella_tags': exact_submitted_tags,
        'result_ella_tags': ella_tags,
        'submitted_ella_signal': exact_submitted_signal,
        'result_ella_signal': ella_signal,
        'today_card_grounding_sha256': _optional_json_fingerprint(today_card_grounding),
        'today_card_grounding_evidence_sha256': _optional_json_fingerprint(today_card_grounding_evidence),
        'expected_transcript_hash': expected_transcript_hash,
        'require_source_match': bool(require_source_match),
    }


def _legacy_summary_request_fingerprint_input(
    *,
    structured: dict[str, Any],
    summary_source: str,
    summary_kind: str,
    correction_id: Optional[str],
    based_on_version_id: Optional[str],
    set_active: bool,
    require_canonical: bool,
    require_based_on_match: bool,
    preserve_generated_results: bool,
    ella_tags: list[str],
    ella_signal: Any,
    today_card_grounding: Any,
    today_card_grounding_evidence: Any,
    expected_transcript_hash: Optional[str] = None,
    require_source_match: bool = False,
) -> dict[str, Any]:
    """Reconstruct the pre-migration receipt only for validating existing records."""
    return {
        'structured': {key: structured.get(key) for key in ('title', 'overview', 'emoji', 'category')},
        'summary_source': summary_source,
        'summary_kind': summary_kind,
        'correction_id': correction_id,
        'based_on_version_id': based_on_version_id,
        'set_active': bool(set_active),
        'require_canonical': bool(require_canonical),
        'require_based_on_match': bool(require_based_on_match),
        'preserve_generated_results': bool(preserve_generated_results),
        'ella_tags': ella_tags,
        'ella_signal': ella_signal,
        'today_card_grounding_sha256': _optional_json_fingerprint(today_card_grounding),
        'today_card_grounding_evidence_sha256': _optional_json_fingerprint(today_card_grounding_evidence),
        'expected_transcript_hash': expected_transcript_hash,
        'require_source_match': bool(require_source_match),
    }


def is_current_summary_request_fingerprint_input(value: Any) -> bool:
    return isinstance(value, dict) and _CURRENT_REQUEST_FINGERPRINT_KEYS.issubset(value)


def _is_legacy_summary_request_fingerprint_input(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _LEGACY_REQUEST_FINGERPRINT_KEYS.issubset(value)
        and not _CURRENT_REQUEST_FINGERPRINT_KEYS.intersection(value)
    )


def _summary_request_fingerprint(request_input: dict[str, Any]) -> str:
    return _json_fingerprint(request_input)


def _active_summary_authority(conversation: dict[str, Any]) -> dict[str, Any]:
    active_version = _active_summary_version(conversation)
    enrichment_state = conversation.get('enrichment_state') or {}
    return {
        'active_summary_version_id': str(conversation.get('active_summary_version_id') or '').strip(),
        'structured': {key: active_version.get(key) for key in ('title', 'overview', 'emoji', 'category')},
        'source': str(active_version.get('source') or enrichment_state.get('source') or ''),
        'kind': str(active_version.get('kind') or enrichment_state.get('kind') or ''),
    }


def _summary_enrichment_authority(conversation: dict[str, Any]) -> dict[str, Any]:
    enrichment_state = conversation.get('enrichment_state') or {}
    return {field: enrichment_state.get(field) for field in _SUMMARY_ENRICHMENT_AUTHORITY_FIELDS}


def _conversation_projected_to_active_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    authority = _active_summary_authority(conversation)
    if not authority['active_summary_version_id'] or not _active_summary_version(conversation):
        raise ConcurrentConversationSummaryChangeError('active_summary_version_missing')
    return {
        **conversation,
        'structured': authority['structured'],
    }


def _assert_replay_binding_matches_active(
    *,
    conversation: dict[str, Any],
    active_version: dict[str, Any],
    request_input: dict[str, Any],
) -> None:
    if not active_version:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
    if is_current_summary_request_fingerprint_input(request_input):
        bound_structured = request_input.get('result_structured')
        bound_tags = request_input.get('result_ella_tags')
        bound_signal = request_input.get('result_ella_signal')
    elif _is_legacy_summary_request_fingerprint_input(request_input):
        bound_structured = request_input.get('structured')
        bound_tags = request_input.get('ella_tags')
        bound_signal = request_input.get('ella_signal')
    else:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
    if not isinstance(bound_structured, dict):
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
    current_structured = {
        key: active_version.get(key, (conversation.get('structured') or {}).get(key))
        for key in ('title', 'overview', 'emoji', 'category')
    }
    if bound_structured != current_structured:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    if bound_tags != _normalized_tags(None, conversation.get('ella_tags')):
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    if bound_signal != conversation.get('ella_signal'):
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    enrichment_state = conversation.get('enrichment_state') or {}
    if str(request_input.get('summary_source') or '') != str(
        active_version.get('source') or enrichment_state.get('source') or ''
    ) or str(request_input.get('summary_kind') or '') != str(
        active_version.get('kind') or enrichment_state.get('kind') or ''
    ):
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')


def _assert_replay_authority_unchanged(
    *,
    conversation: dict[str, Any],
    expected_active_authority: dict[str, Any],
    expected_enrichment_authority: dict[str, Any],
) -> None:
    if (
        _active_summary_authority(conversation) != expected_active_authority
        or _summary_enrichment_authority(conversation) != expected_enrichment_authority
    ):
        raise ConcurrentConversationSummaryChangeError('idempotency_authority_changed')


def _candidate_replay_structured(
    conversation: dict[str, Any],
    active_version: dict[str, Any],
    *,
    sanitized: Any,
) -> dict[str, Any]:
    structured = {
        key: active_version.get(key, (conversation.get('structured') or {}).get(key))
        for key in ('title', 'overview', 'emoji', 'category')
    }
    for key in ('title', 'overview', 'emoji', 'category'):
        value = getattr(sanitized, key)
        if value is not None:
            structured[key] = value
    return structured


def _assert_legacy_same_trace_payload_matches(
    *,
    conversation: dict[str, Any],
    active_version: dict[str, Any],
    enrichment_state: dict[str, Any],
    candidate_structured: dict[str, Any],
    summary_source: str,
    summary_kind: str,
    correction_id: Optional[str],
    based_on_version_id: Optional[str],
    set_active: bool,
    require_canonical: bool,
) -> None:
    if require_canonical:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
    if not active_version or not set_active:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
    if (
        str(active_version.get('source') or enrichment_state.get('source') or '') != summary_source
        or str(active_version.get('kind') or enrichment_state.get('kind') or '') != summary_kind
    ):
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    if correction_id is not None and active_version.get('correction_id') != correction_id:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    if based_on_version_id is not None and active_version.get('based_on_version_id') != based_on_version_id:
        raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
    for key in ('title', 'overview', 'emoji', 'category'):
        expected = active_version.get(key, (conversation.get('structured') or {}).get(key))
        if candidate_structured.get(key) != expected:
            raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')


async def _repair_canonical_to_latest_summary(
    *,
    uid: str,
    conversation_id: str,
    canonical_writer: Callable[..., dict],
    canonical_retry_recorder: Optional[Callable[[str], Awaitable[bool]]] = None,
    max_attempts: int = 3,
) -> None:
    """Converge the canonical row after a stale writer loses its Firestore CAS."""
    for _attempt in range(max_attempts):
        latest = conversations_db.get_conversation(uid, conversation_id)
        if latest is None:
            raise ConversationSummaryNotFoundError(conversation_id)
        expected_active_authority = _active_summary_authority(latest)
        if not expected_active_authority['active_summary_version_id']:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_missing')
        active_version = _active_summary_version(latest)
        enrichment_state = latest.get('enrichment_state') or {}
        expected_enrichment_authority = _summary_enrichment_authority(latest)
        canonical_conversation = _conversation_projected_to_active_summary({**latest, 'id': conversation_id})
        canonical_result = await asyncio.to_thread(
            canonical_writer,
            uid,
            canonical_conversation,
            summary_source=str(active_version.get('source') or enrichment_state.get('source') or 'observer'),
            summary_kind=str(active_version.get('kind') or enrichment_state.get('kind') or 'observer_enriched'),
            trace_id=enrichment_state.get('trace_id'),
        )
        if not isinstance(canonical_result, dict) or canonical_result.get('ok') is not True:
            raise CanonicalSummaryWriteUnconfirmedError('canonical_repair_unconfirmed')
        refreshed = conversations_db.get_conversation(uid, conversation_id)
        if refreshed is None:
            raise ConversationSummaryNotFoundError(conversation_id)
        try:
            _assert_replay_authority_unchanged(
                conversation=refreshed,
                expected_active_authority=expected_active_authority,
                expected_enrichment_authority=expected_enrichment_authority,
            )
        except ConcurrentConversationSummaryChangeError:
            continue
        return
    try:
        await _mark_latest_summary_pending_canonical(
            uid=uid,
            conversation_id=conversation_id,
            error='canonical_repair_raced',
        )
    except ConcurrentConversationSummaryChangeError:
        if canonical_retry_recorder is None:
            raise
        recorded = await canonical_retry_recorder('canonical_repair_raced')
        if recorded is not True:
            raise CanonicalSummaryRetryReceiptUnconfirmedError('canonical_retry_receipt_unconfirmed')
    raise CanonicalSummaryRepairExhaustedError('active_summary_version_changed_during_canonical_repair')


async def _mark_latest_summary_pending_canonical(
    *,
    uid: str,
    conversation_id: str,
    error: str,
    max_attempts: int = 3,
) -> None:
    """Persist a retryable receipt on the exact active version after repair races."""
    for _attempt in range(max_attempts):
        latest = conversations_db.get_conversation(uid, conversation_id)
        if latest is None:
            raise ConversationSummaryNotFoundError(conversation_id)
        expected_version_id = str(latest.get('active_summary_version_id') or '').strip()
        if not expected_version_id:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_missing')
        enrichment_state = latest.get('enrichment_state') or {}
        pending_state = {
            **enrichment_state,
            'status': 'writeback_pending_canonical',
            'pending': True,
            'canonical_status': 'failed',
            'error': error,
            'updated_at': datetime.now(timezone.utc),
        }
        if conversations_db.update_conversation_if_summary_authority(
            uid,
            conversation_id,
            expected_version_id,
            enrichment_state,
            {'enrichment_state': pending_state},
        ):
            return
    raise ConcurrentConversationSummaryChangeError('active_summary_version_changed_while_marking_canonical_retry')


async def _confirm_canonical_if_result_is_active(
    *,
    uid: str,
    conversation_id: str,
    expected_result_version_id: str,
    expected_state: dict[str, Any],
    confirmed_state: dict[str, Any],
    canonical_writer: Callable[..., dict],
    canonical_retry_recorder: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> None:
    confirmed = conversations_db.update_conversation_if_summary_authority(
        uid,
        conversation_id,
        expected_result_version_id,
        expected_state,
        {'enrichment_state': confirmed_state},
    )
    if confirmed:
        return
    await _repair_canonical_to_latest_summary(
        uid=uid,
        conversation_id=conversation_id,
        canonical_writer=canonical_writer,
        canonical_retry_recorder=canonical_retry_recorder,
    )
    raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')


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
    replay_request_fingerprint_input: Optional[dict[str, Any]] = None,
    canonical_retry_recorder: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> dict[str, Any]:
    submitted_structured = {
        'title': title,
        'overview': overview,
        'emoji': emoji,
        'category': category,
    }
    submitted_ella_tags = copy.deepcopy(ella_tags)
    submitted_ella_signal = copy.deepcopy(ella_signal)
    submitted_today_card_grounding = copy.deepcopy(today_card_grounding)
    submitted_today_card_grounding_evidence = copy.deepcopy(today_card_grounding_evidence)
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise ConversationSummaryNotFoundError(conversation_id)

    enrichment_state = conversation.get('enrichment_state') or {}
    same_trace = bool(trace_id and enrichment_state.get('trace_id') == trace_id)
    if require_source_match:
        if not trace_id or not expected_transcript_hash:
            raise ValueError('summary_source_match_fields_required')
        observed_transcript_hash = transcript_grounding_hash(_conversation_transcript_segments(conversation))
        if observed_transcript_hash != expected_transcript_hash:
            raise ConcurrentConversationSummaryChangeError('transcript_changed')
        if same_trace:
            if (
                enrichment_state.get('source_transcript_hash') != expected_transcript_hash
                or enrichment_state.get('source_active_summary_version_id') != based_on_version_id
            ):
                raise ConcurrentConversationSummaryChangeError('summary_source_changed')
            result_summary_version_id = str(enrichment_state.get('result_summary_version_id') or '').strip()
            if result_summary_version_id and (
                str(conversation.get('active_summary_version_id') or '').strip() != result_summary_version_id
            ):
                raise ConcurrentConversationSummaryChangeError('summary_result_version_changed')
        elif conversation.get('active_summary_version_id') != based_on_version_id:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
    if (
        not same_trace
        and require_based_on_match
        and conversation.get('active_summary_version_id') != based_on_version_id
    ):
        raise ConcurrentConversationSummaryChangeError('active_summary_version_changed')
    active_version = _active_summary_version(conversation)
    sanitized = sanitize_summary_update(title=title, overview=overview, emoji=emoji, category=category)
    candidate_structured = _candidate_replay_structured(
        conversation,
        active_version,
        sanitized=sanitized,
    )
    requested_tags = _normalized_tags(ella_tags)
    effective_tags = requested_tags if requested_tags else _normalized_tags(None, conversation.get('ella_tags'))
    effective_signal = ella_signal if ella_signal is not None else conversation.get('ella_signal')
    request_input = (
        dict(replay_request_fingerprint_input)
        if replay_request_fingerprint_input is not None
        else _summary_request_fingerprint_input(
            structured=candidate_structured,
            summary_source=summary_source,
            summary_kind=summary_kind,
            correction_id=correction_id,
            based_on_version_id=based_on_version_id,
            set_active=set_active,
            require_canonical=require_canonical,
            require_based_on_match=require_based_on_match,
            preserve_generated_results=preserve_generated_results,
            ella_tags=effective_tags,
            ella_signal=effective_signal,
            today_card_grounding=submitted_today_card_grounding,
            today_card_grounding_evidence=submitted_today_card_grounding_evidence,
            submitted_structured=submitted_structured,
            submitted_ella_tags=submitted_ella_tags,
            submitted_ella_signal=submitted_ella_signal,
            expected_transcript_hash=expected_transcript_hash,
            require_source_match=require_source_match,
        )
    )
    request_fingerprint = _summary_request_fingerprint(request_input)
    existing_grounding = enrichment_state.get('today_card_grounding')
    if (
        same_trace
        and summary_source == 'hermes_parallel'
        and isinstance(existing_grounding, dict)
        and existing_grounding.get('transcript_hash')
        != transcript_grounding_hash(_conversation_transcript_segments(conversation))
    ):
        raise ConcurrentConversationSummaryChangeError('transcript_changed')
    if same_trace:
        stored_fingerprint = str(enrichment_state.get('request_fingerprint') or '').strip()
        if stored_fingerprint:
            stored_request_input = enrichment_state.get('request_fingerprint_input')
            if replay_request_fingerprint_input is not None:
                if (
                    enrichment_state.get('status') != 'writeback_pending_canonical'
                    or not is_current_summary_request_fingerprint_input(stored_request_input)
                    or stored_request_input != request_input
                ):
                    raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
                _assert_replay_binding_matches_active(
                    conversation=conversation,
                    active_version=active_version,
                    request_input=request_input,
                )
            elif _is_legacy_summary_request_fingerprint_input(stored_request_input):
                legacy_request_input = _legacy_summary_request_fingerprint_input(
                    structured=candidate_structured,
                    summary_source=summary_source,
                    summary_kind=summary_kind,
                    correction_id=correction_id,
                    based_on_version_id=based_on_version_id,
                    set_active=set_active,
                    require_canonical=require_canonical,
                    require_based_on_match=require_based_on_match,
                    preserve_generated_results=preserve_generated_results,
                    ella_tags=effective_tags,
                    ella_signal=effective_signal,
                    today_card_grounding=submitted_today_card_grounding,
                    today_card_grounding_evidence=submitted_today_card_grounding_evidence,
                    expected_transcript_hash=expected_transcript_hash,
                    require_source_match=require_source_match,
                )
                if stored_request_input != legacy_request_input:
                    raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
                request_fingerprint = _summary_request_fingerprint(legacy_request_input)
                _assert_replay_binding_matches_active(
                    conversation=conversation,
                    active_version=active_version,
                    request_input=legacy_request_input,
                )
            elif not is_current_summary_request_fingerprint_input(stored_request_input):
                raise ConcurrentConversationSummaryChangeError('idempotency_payload_unverifiable')
            if stored_fingerprint != request_fingerprint:
                raise ConcurrentConversationSummaryChangeError('idempotency_payload_changed')
        else:
            _assert_legacy_same_trace_payload_matches(
                conversation=conversation,
                active_version=active_version,
                enrichment_state=enrichment_state,
                candidate_structured=candidate_structured,
                summary_source=summary_source,
                summary_kind=summary_kind,
                correction_id=correction_id,
                based_on_version_id=based_on_version_id,
                set_active=set_active,
                require_canonical=require_canonical,
            )
    if (
        same_trace
        and enrichment_state.get('status') == 'writeback_applied'
        and (not require_canonical or enrichment_state.get('canonical_status') == 'completed')
    ):
        expected_active_authority = _active_summary_authority(conversation)
        expected_enrichment_authority = _summary_enrichment_authority(conversation)
        if require_canonical:
            await _repair_canonical_to_latest_summary(
                uid=uid,
                conversation_id=conversation_id,
                canonical_writer=canonical_writer,
                canonical_retry_recorder=canonical_retry_recorder,
            )
            refreshed = conversations_db.get_conversation(uid, conversation_id)
            if refreshed is None:
                raise ConversationSummaryNotFoundError(conversation_id)
            _assert_replay_authority_unchanged(
                conversation=refreshed,
                expected_active_authority=expected_active_authority,
                expected_enrichment_authority=expected_enrichment_authority,
            )
            conversation = refreshed
            enrichment_state = conversation.get('enrichment_state') or {}
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
        result_summary_version_id = str(conversation.get('active_summary_version_id') or '').strip()
        if not result_summary_version_id:
            raise ConcurrentConversationSummaryChangeError('active_summary_version_missing')
        confirmed_state = {
            **enrichment_state,
            'status': 'writeback_applied',
            'pending': False,
            'canonical_status': 'completed',
            'error': None,
            'updated_at': datetime.now(timezone.utc),
        }
        canonical_conversation = _conversation_projected_to_active_summary(
            {
                **conversation,
                'id': conversation_id,
                'enrichment_state': confirmed_state,
            }
        )
        try:
            canonical_result = await asyncio.to_thread(
                canonical_writer,
                uid,
                canonical_conversation,
                summary_source=str(enrichment_state.get('source') or active_version.get('source') or ''),
                summary_kind=str(enrichment_state.get('kind') or active_version.get('kind') or ''),
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
        await _confirm_canonical_if_result_is_active(
            uid=uid,
            conversation_id=conversation_id,
            expected_result_version_id=result_summary_version_id,
            expected_state=enrichment_state,
            confirmed_state=confirmed_state,
            canonical_writer=canonical_writer,
            canonical_retry_recorder=canonical_retry_recorder,
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
    normalized_tags = _normalized_tags(ella_tags)
    written_tags = normalized_tags if normalized_tags else _normalized_tags(None, conversation.get('ella_tags'))
    written_signal = ella_signal if ella_signal is not None else conversation.get('ella_signal')
    request_input = _summary_request_fingerprint_input(
        structured=structured,
        summary_source=summary_source,
        summary_kind=summary_kind,
        correction_id=correction_id,
        based_on_version_id=based_on_version_id,
        set_active=set_active,
        require_canonical=require_canonical,
        require_based_on_match=require_based_on_match,
        preserve_generated_results=preserve_generated_results,
        ella_tags=written_tags,
        ella_signal=written_signal,
        today_card_grounding=submitted_today_card_grounding,
        today_card_grounding_evidence=submitted_today_card_grounding_evidence,
        submitted_structured=submitted_structured,
        submitted_ella_tags=submitted_ella_tags,
        submitted_ella_signal=submitted_ella_signal,
        expected_transcript_hash=expected_transcript_hash,
        require_source_match=require_source_match,
    )
    request_fingerprint = _summary_request_fingerprint(request_input)
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
        'request_fingerprint': request_fingerprint,
        'request_fingerprint_input': request_input,
    }

    if internal_assessment_fetcher:
        internal_assessment = await internal_assessment_fetcher(uid, conversation_id)
        if internal_assessment:
            update_data['internal_assessment'] = internal_assessment

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
            expected_enrichment_state=enrichment_state,
        )
        if not updated:
            raise ConcurrentConversationSummaryChangeError('transcript_changed')
    elif require_based_on_match:
        updated = conversations_db.update_conversation_if_summary_authority(
            uid,
            conversation_id,
            based_on_version_id,
            enrichment_state,
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
        if canonical_error is not None:
            failed_state = {
                **update_data['enrichment_state'],
                'canonical_status': 'failed',
                'error': 'canonical_write_unconfirmed',
                'updated_at': datetime.now(timezone.utc),
            }
            if not conversations_db.update_conversation_if_summary_authority(
                uid,
                conversation_id,
                version_update['active_summary_version_id'],
                update_data['enrichment_state'],
                {'enrichment_state': failed_state},
            ):
                raise ConcurrentConversationSummaryChangeError('summary_result_version_changed') from canonical_error
            raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed') from canonical_error
        await _confirm_canonical_if_result_is_active(
            uid=uid,
            conversation_id=conversation_id,
            expected_result_version_id=version_update['active_summary_version_id'],
            expected_state=update_data['enrichment_state'],
            confirmed_state=confirmed_state,
            canonical_writer=canonical_writer,
            canonical_retry_recorder=canonical_retry_recorder,
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
