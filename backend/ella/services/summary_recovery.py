"""Shared Hermes summary generation and failed-conversation recovery."""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

import database.conversations as conversations_db
from ella.services.hermes_session import canonical_omi_session_key
from ella.services.memory_artwork_recovery import claim_memory_artwork_enrichment_recovery
from ella.services.runtime_resolver import (
    CloudRuntimeAuthorityIdentity,
    cloud_runtime_authority_identity,
    revalidate_cloud_runtime_authority,
    resolve_isolated_runtime,
)
from ella.services.summary_writeback import (
    CanonicalSummaryRepairExhaustedError,
    CanonicalSummaryRetryReceiptUnconfirmedError,
    CanonicalSummaryWriteUnconfirmedError,
    is_current_summary_request_fingerprint_input,
    write_conversation_summary,
)
from models.conversation import CategoryEnum, Conversation, ConversationStatus
from models.conversation_integrity import transcript_grounding_hash
from utils.conversations.generic_summary import generate_stock_conversation_summary
from utils.conversations.vector import refresh_structured_summary_vector

logger = logging.getLogger(__name__)

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
SUMMARY_RECOVERY_FAILED = 'conversation_summary_recovery_failed'
LEGACY_GENERIC_SUMMARY_BASELINE = 'legacy-unversioned-summary'


def _transcript_metadata(conversation: dict[str, Any]) -> tuple[int, int]:
    segments = conversation.get('transcript_segments') or []
    return len(segments), sum(len(str(segment.get('text') or '')) for segment in segments)


def _active_summary_version(conversation: dict[str, Any]) -> dict[str, Any]:
    versions = conversation.get('summary_versions') or []
    active_id = conversation.get('active_summary_version_id')
    if active_id:
        for version in versions:
            if str(version.get('id') or '') == str(active_id):
                return version
    return next((version for version in reversed(versions) if version.get('is_active')), {})


def _conversation_vector_present(uid: str, conversation_id: str) -> bool:
    from database import vector_db

    return conversation_id in vector_db.fetch_existing_conversation_vector_ids(uid, [conversation_id])


def _conversation_vector_metadata(uid: str, conversation_id: str) -> Optional[dict[str, Any]]:
    from database import vector_db

    return vector_db.fetch_conversation_vector_metadata(uid, conversation_id)


def _summary_content_sha256(conversation: dict[str, Any]) -> str:
    payload = json.dumps(
        conversation.get('structured') or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def build_conversation_processing_retry_plan(uid: str, conversation_id: str) -> Optional[dict[str, Any]]:
    """Inspect one UID-owned record without writing or returning transcript content."""

    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        return None
    mode, reason = conversations_db.conversation_processing_recovery_mode(conversation)
    segment_count, transcript_character_count = _transcript_metadata(conversation)
    _, transcript_sha256 = build_hermes_recovery_source(conversation)
    active_version = _active_summary_version(conversation)
    vector_present = _conversation_vector_present(uid, conversation_id)
    vector_metadata = _conversation_vector_metadata(uid, conversation_id) if vector_present else None
    active_summary_version_id = conversation.get('active_summary_version_id')
    active_summary_sha256 = _summary_content_sha256(conversation) if active_summary_version_id else None
    enrichment_state = conversation.get('enrichment_state') or {}
    canonical_session = canonical_omi_session_key(uid)
    provider_path = ['hermes_api_canonical_session', 'omi_enriched_summary_writeback', 'canonical_ledger', 'vector']
    if mode == 'full':
        provider_path.insert(0, 'omi_stock_summary_provider')

    status = getattr(conversation.get('status'), 'value', conversation.get('status'))
    return {
        'conversation_id': conversation_id,
        'status': status,
        'discarded': bool(conversation.get('discarded')),
        'processing_error': conversation.get('processing_error'),
        'started_at': conversation.get('started_at'),
        'finished_at': conversation.get('finished_at'),
        'transcript_segment_count': segment_count,
        'transcript_character_count': transcript_character_count,
        'transcript_sha256': transcript_sha256,
        'structured_summary_present': conversations_db.has_usable_conversation_summary(conversation),
        'structured_summary_sha256': (
            _summary_content_sha256(conversation)
            if conversations_db.has_usable_conversation_summary(conversation)
            else None
        ),
        'active_summary_version_id': active_summary_version_id,
        'active_summary_source': active_version.get('source'),
        'active_summary_kind': active_version.get('kind'),
        'enriched_summary_present': conversations_db.has_enriched_conversation_summary(conversation),
        'canonical_provenance_confirmed': enrichment_state.get('canonical_status') == 'completed',
        'vector_present': vector_present,
        'vector_active_summary_version_id': (vector_metadata or {}).get('active_summary_version_id'),
        'vector_content_sha256': (vector_metadata or {}).get('summary_content_sha256'),
        'vector_matches_active_summary': bool(
            vector_metadata
            and active_summary_version_id
            and (vector_metadata or {}).get('active_summary_version_id') == active_summary_version_id
            and (vector_metadata or {}).get('summary_content_sha256') == active_summary_sha256
        ),
        'recovery_mode': mode or 'none',
        'retryable': mode is not None,
        'reason': reason,
        'profile_scope_sha256': hashlib.sha256(uid.encode('utf-8')).hexdigest(),
        'canonical_session_scope_sha256': hashlib.sha256(canonical_session.encode('utf-8')).hexdigest(),
        'provider_path': provider_path,
        'zero_writes': True,
    }


@dataclass(frozen=True)
class SummaryProviderConfig:
    provider: str
    hermes_url: str
    hermes_model: str
    hermes_api_key: str
    legacy_url: str
    legacy_model: str
    legacy_api_key: str
    timeout_seconds: float
    cloud_authority: Optional[CloudRuntimeAuthorityIdentity] = None


class ConcurrentConversationRecoveryChangeError(RuntimeError):
    pass


def default_summary_provider_config() -> SummaryProviderConfig:
    return SummaryProviderConfig(
        provider=os.getenv('ELLA_SUMMARY_RECOVERY_PROVIDER', 'hermes-api').strip().lower() or 'hermes-api',
        hermes_url=os.getenv(
            'ELLA_CORRECTION_HERMES_CHAT_URL',
            os.getenv('HERMES_CHAT_COMPLETIONS_URL', 'http://100.76.138.56:8642/v1/chat/completions'),
        ),
        hermes_model=os.getenv(
            'ELLA_CORRECTION_HERMES_MODEL',
            os.getenv('HERMES_CORRECTION_MODEL', 'ella-plato-hermes-eval'),
        ),
        hermes_api_key=(
            os.getenv('ELLA_CORRECTION_HERMES_API_KEY')
            or os.getenv('API_SERVER_KEY')
            or os.getenv('HERMES_API_KEY')
            or ''
        ),
        legacy_url=os.getenv('ELLA_CORRECTION_API_URL', 'https://api.x.ai/v1/chat/completions'),
        legacy_model=os.getenv('ELLA_CORRECTION_MODEL', 'grok-4.3'),
        legacy_api_key=(
            os.getenv('ELLA_CORRECTION_API_KEY') or os.getenv('XAI_API_KEY') or os.getenv('HERMES_API_KEY') or ''
        ),
        timeout_seconds=float(os.getenv('ELLA_CORRECTION_TIMEOUT_SECONDS', '45')),
    )


async def summary_provider_config_for_uid(
    uid: str,
    config: Optional[SummaryProviderConfig] = None,
) -> SummaryProviderConfig:
    """Bind Hermes summary work to the active isolated runtime when selected."""
    selected = config or default_summary_provider_config()
    runtime = await resolve_isolated_runtime(uid, target_mode="hermes-cloud-transcript")
    if runtime is None:
        return selected
    if runtime.provider != 'hermes_cloud':
        return replace(
            selected,
            provider='hermes-api',
            hermes_url=f"{runtime.gateway_url.rstrip('/')}/v1/chat/completions",
            hermes_model=runtime.agent_id,
            hermes_api_key=runtime.gateway_token,
            legacy_api_key='',
            cloud_authority=None,
        )
    return replace(
        selected,
        provider='hermes-api',
        hermes_url='',
        hermes_model='',
        hermes_api_key='',
        legacy_api_key='',
        cloud_authority=cloud_runtime_authority_identity(runtime),
    )


async def resolve_summary_provider_send(
    config: SummaryProviderConfig,
) -> tuple[str, str, str]:
    """Return current send material after exact Cloud authority revalidation."""
    if config.cloud_authority is None:
        return config.hermes_url, config.hermes_model, config.hermes_api_key
    current = await revalidate_cloud_runtime_authority(config.cloud_authority)
    return (
        f"{current.gateway_url.rstrip('/')}/v1/chat/completions",
        current.agent_id,
        current.gateway_token,
    )


def build_hermes_recovery_source(
    conversation: dict[str, Any],
) -> tuple[str, str]:
    """Return a lossless canonical transcript payload and its SHA-256."""

    source_document = json.dumps(
        conversation.get('transcript_segments') or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )
    return source_document, hashlib.sha256(source_document.encode('utf-8')).hexdigest()


def extract_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = JSON_OBJECT_RE.search(content)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError('Summary model response was not a JSON object')
    return parsed


def normalize_summary(
    result: dict[str, Any],
    fallback: dict[str, Any],
    required_tags: tuple[str, ...] = ('omi',),
) -> dict[str, Any]:
    title = str(result.get('title') or fallback.get('title') or 'Recovered Conversation').strip()
    overview = str(result.get('overview') or fallback.get('overview') or '').strip()
    if not overview:
        raise ValueError('Summary model response missing overview')
    if not overview.startswith('[Ella] '):
        overview = '[Ella] ' + overview.removeprefix('[Ella]').strip()

    emoji = str(result.get('emoji') or fallback.get('emoji') or '\U0001fab6').strip()[:4] or '\U0001fab6'
    category = str(result.get('category') or fallback.get('category') or 'other').strip().lower()
    category = {'media': CategoryEnum.entertainment.value, 'romance': CategoryEnum.romance.value}.get(
        category,
        category,
    )
    if category not in {item.value for item in CategoryEnum}:
        category = CategoryEnum.other.value
    tags = result.get('ella_tags')
    if not isinstance(tags, list):
        tags = []
    tags = [str(tag).strip().lower() for tag in tags if str(tag or '').strip()]
    for tag in reversed(required_tags):
        if tag not in tags:
            tags.insert(0, tag)

    signal = result.get('ella_signal')
    if not isinstance(signal, dict):
        signal = {}

    return {
        'title': title,
        'overview': overview,
        'emoji': emoji,
        'category': category,
        'ella_tags': tags[:12],
        'ella_signal': {
            'salience': str(signal.get('salience') or 'medium'),
            'memory_promotion': str(signal.get('memory_promotion') or 'none'),
            'noise_level': str(signal.get('noise_level') or 'low'),
            'contains_media': bool(signal.get('contains_media', False)),
            'contains_user_speech': bool(signal.get('contains_user_speech', True)),
            'guardian_relevant': bool(signal.get('guardian_relevant', False)),
        },
    }


async def generate_summary_from_prompt(
    *,
    prompt: str,
    fallback: dict[str, Any],
    session_id: str,
    session_key: str,
    trace_id: str,
    required_tags: tuple[str, ...],
    config: SummaryProviderConfig,
    async_client_factory: Any = httpx.AsyncClient,
) -> dict[str, Any]:
    if config.provider == 'hermes-api':
        if config.cloud_authority is None and not config.hermes_api_key:
            raise RuntimeError('No Hermes summary API key configured')
        url = config.hermes_url
        model = config.hermes_model
        api_key = config.hermes_api_key
        max_tokens = 900
    else:
        if not config.legacy_api_key:
            raise RuntimeError('No legacy summary API key configured')
        url = config.legacy_url
        model = config.legacy_model
        api_key = config.legacy_api_key
        max_tokens = 800

    async with async_client_factory(timeout=config.timeout_seconds) as client:
        if config.provider == 'hermes-api' and config.cloud_authority is not None:
            url, model, api_key = await resolve_summary_provider_send(config)
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        if config.provider == 'hermes-api':
            headers.update(
                {
                    'X-Hermes-Session-Id': session_id,
                    'X-Hermes-Session-Key': session_key,
                    'X-Trace-Id': trace_id,
                }
            )
        response = await client.post(
            url,
            headers=headers,
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': max_tokens,
            },
        )
    response.raise_for_status()
    body = response.json()
    content = body['choices'][0]['message']['content']
    return normalize_summary(extract_json_object(content), fallback, required_tags=required_tags)


async def apply_summary_update(
    *,
    uid: str,
    conversation_id: str,
    trace_id: str,
    active_summary_version_id: Optional[str],
    summary: dict[str, Any],
    summary_kind: str,
    summary_source: str = 'observer',
    correction_id: Optional[str] = None,
    require_canonical: bool = False,
    require_based_on_match: bool = False,
    expected_transcript_hash: Optional[str] = None,
    require_source_match: bool = False,
    preserve_generated_results: bool = False,
    today_card_grounding: Optional[dict[str, Any]] = None,
    replay_request_fingerprint_input: Optional[dict[str, Any]] = None,
    canonical_retry_recorder: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> dict[str, Any]:
    return await write_conversation_summary(
        uid=uid,
        conversation_id=conversation_id,
        title=summary.get('title'),
        overview=summary.get('overview'),
        emoji=summary.get('emoji'),
        category=summary.get('category'),
        summary_source=summary_source,
        summary_kind=summary_kind,
        correction_id=correction_id,
        based_on_version_id=active_summary_version_id,
        set_active=True,
        trace_id=trace_id,
        ella_tags=summary.get('ella_tags') or ['omi'],
        ella_signal=summary.get('ella_signal') or {},
        require_canonical=require_canonical,
        require_based_on_match=require_based_on_match,
        expected_transcript_hash=expected_transcript_hash,
        require_source_match=require_source_match,
        preserve_generated_results=preserve_generated_results,
        today_card_grounding=today_card_grounding,
        replay_request_fingerprint_input=replay_request_fingerprint_input,
        canonical_retry_recorder=canonical_retry_recorder,
    )


def _processing_retry_canonical_recorder(
    *,
    uid: str,
    conversation_id: str,
    request_id: str,
    attempt_count: Optional[int],
) -> Callable[[str], Awaitable[bool]]:
    async def record(_error: str) -> bool:
        return bool(
            await asyncio.to_thread(
                conversations_db.record_conversation_processing_retry_enrichment,
                uid,
                conversation_id,
                request_id,
                'canonical_failed',
                attempt_count=attempt_count,
            )
        )

    return record


def _structured_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    structured = conversation.get('structured') or {}
    return {
        'title': structured.get('title') or conversation.get('title') or '',
        'overview': structured.get('overview') or conversation.get('overview') or '',
        'emoji': structured.get('emoji') or conversation.get('emoji') or '',
        'category': str(structured.get('category') or conversation.get('category') or 'other'),
    }


def _build_recovery_prompt(conversation: dict[str, Any], client_context: Optional[str]) -> str:
    structured = _structured_summary(conversation)
    transcript_source, transcript_sha256 = build_hermes_recovery_source(conversation)
    return f"""You are Ella, the user's companion summary writer.

Recover an OMI conversation whose first summary attempt failed. Use the complete transcript and durable companion context available in this Hermes session. Return JSON only.

Rules:
- Produce one accurate, warm, specific summary grounded in the transcript.
- overview must start with "[Ella] ".
- Do not invent details or expose raw speaker labels when a natural description is available.
- title must be short and contain no markdown.
- category should be one of: personal, education, health, finance, legal, philosophy, spiritual, science, entrepreneurship, parenting, romantic, travel, inspiration, technology, business, social, work, sports, politics, literature, history, architecture, music, weather, news, entertainment, psychology, real, design, family, economics, environment, other.
- Include ella_tags and ella_signal for downstream ranking.

Optional user context:
{client_context or '[none]'}

Existing partial summary:
{json.dumps(structured, ensure_ascii=False, indent=2)}

Authoritative transcript SHA-256: {transcript_sha256}

Authoritative transcript segments (lossless JSON):
{transcript_source}

Return exactly:
{{
  "title": "short title",
  "overview": "[Ella] recovered warm summary",
  "emoji": "one emoji",
  "category": "category",
  "ella_tags": ["omi", "recovery"],
  "ella_signal": {{
    "salience": "low|medium|high",
    "memory_promotion": "none|candidate|promoted",
    "noise_level": "none|low|medium|high",
    "contains_media": false,
    "contains_user_speech": true,
    "guardian_relevant": false
  }}
}}
"""


async def invoke_hermes_recovery(
    *,
    uid: str,
    conversation: dict[str, Any],
    request_id: str,
    attempt_count: Optional[int] = None,
    client_context: Optional[str] = None,
    config: Optional[SummaryProviderConfig] = None,
    async_client_factory: Any = httpx.AsyncClient,
    trace_id_override: Optional[str] = None,
) -> dict[str, Any]:
    """Generate and strictly apply enrichment through the canonical Hermes API session."""

    config = config or default_summary_provider_config()
    if config.provider != 'hermes-api':
        raise RuntimeError('Hermes API is required for historical enrichment recovery')
    conversation_id = str(conversation['id'])
    _, source_sha256 = build_hermes_recovery_source(conversation)
    expected_transcript_hash = transcript_grounding_hash(conversation.get('transcript_segments') or [])
    source_summary_sha256 = _summary_content_sha256(conversation)
    default_trace_id = f'summary-retry:{conversation_id}:{request_id}:hermes'
    trace_id = str(trace_id_override or default_trace_id)
    session_id = f'summary-recovery:{conversation_id}:{request_id}'
    if trace_id != default_trace_id:
        session_id = f'{session_id}:{hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:16]}'
    summary = await generate_summary_from_prompt(
        prompt=_build_recovery_prompt(conversation, client_context),
        fallback=_structured_summary(conversation),
        session_id=session_id,
        session_key=canonical_omi_session_key(uid),
        trace_id=trace_id,
        required_tags=('omi', 'recovery'),
        config=config,
        async_client_factory=async_client_factory,
    )
    current = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if current is None:
        raise ConcurrentConversationRecoveryChangeError('conversation_removed_before_recovery_apply')
    _, current_source_sha256 = build_hermes_recovery_source(current)
    if current_source_sha256 != source_sha256:
        raise ConcurrentConversationRecoveryChangeError('conversation_transcript_changed_before_recovery_apply')
    if _summary_content_sha256(current) != source_summary_sha256:
        raise ConcurrentConversationRecoveryChangeError('conversation_summary_changed_before_recovery_apply')
    if not _is_current_retry(current, request_id, attempt_count):
        raise ConcurrentConversationRecoveryChangeError('conversation_recovery_attempt_superseded')
    if current.get('active_summary_version_id') != conversation.get('active_summary_version_id'):
        raise ConcurrentConversationRecoveryChangeError('conversation_summary_changed_before_recovery_apply')
    apply_result = await apply_summary_update(
        uid=uid,
        conversation_id=conversation_id,
        trace_id=trace_id,
        active_summary_version_id=current.get('active_summary_version_id'),
        summary=summary,
        summary_kind='recovered_enriched',
        require_canonical=True,
        require_based_on_match=True,
        expected_transcript_hash=expected_transcript_hash,
        require_source_match=True,
        preserve_generated_results=True,
        canonical_retry_recorder=_processing_retry_canonical_recorder(
            uid=uid,
            conversation_id=conversation_id,
            request_id=request_id,
            attempt_count=attempt_count,
        ),
    )
    version_id = apply_result.get('active_summary_version_id')
    if not version_id or apply_result.get('canonical_confirmed') is not True:
        raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed')
    return {
        'active_summary_version_id': version_id,
        'canonical_confirmed': True,
        'source_sha256': source_sha256,
        'session_scope_sha256': hashlib.sha256(canonical_omi_session_key(uid).encode('utf-8')).hexdigest(),
    }


def _pending_canonical_rerun_trace_id(
    *,
    conversation_id: str,
    request_id: str,
    active_summary_version_id: Any,
    pending_state: dict[str, Any],
) -> str:
    prior_trace_id = str(pending_state.get('trace_id') or 'missing')
    prior_fingerprint = str(pending_state.get('request_fingerprint') or 'missing')
    digest = hashlib.sha256(
        f'{prior_trace_id}|{prior_fingerprint}|{active_summary_version_id or "missing"}'.encode('utf-8')
    ).hexdigest()
    return f'summary-retry:{conversation_id}:{request_id}:hermes:pending-rerun:{digest[:16]}'


def _existing_recovered_version_id(conversation: dict[str, Any]) -> Optional[str]:
    retry_version_id = conversation.get('processing_retry_enriched_version_id')
    if retry_version_id and str(conversation.get('active_summary_version_id') or '') == str(retry_version_id):
        return str(retry_version_id)
    enrichment_state = conversation.get('enrichment_state') or {}
    if (
        enrichment_state.get('status') == 'writeback_applied'
        and enrichment_state.get('kind') == 'recovered_enriched'
        and enrichment_state.get('canonical_status') == 'completed'
    ):
        active_version_id = conversation.get('active_summary_version_id')
        return str(active_version_id) if active_version_id else None
    return None


def _existing_generic_version_id(conversation: dict[str, Any]) -> Optional[str]:
    retry_version_id = conversation.get('processing_retry_summary_version_id')
    if retry_version_id:
        return str(retry_version_id)
    if conversation.get('processing_retry_mode') == 'enrichment_only':
        active_version_id = conversation.get('active_summary_version_id')
        return str(active_version_id) if active_version_id else LEGACY_GENERIC_SUMMARY_BASELINE
    enrichment_state = conversation.get('enrichment_state') or {}
    if enrichment_state.get('status') == 'writeback_applied' and enrichment_state.get('kind') in {
        'generic_recovered',
        'recovered_enriched',
    }:
        active_version_id = conversation.get('active_summary_version_id')
        return str(active_version_id) if active_version_id else None
    return None


def _stock_summary_payload(structured: Any) -> dict[str, Any]:
    if hasattr(structured, 'model_dump'):
        payload = structured.model_dump(mode='json')
    elif isinstance(structured, dict):
        payload = dict(structured)
    else:
        raise ValueError('Stock OMI summary provider returned an invalid result')
    return normalize_summary(payload, payload, required_tags=('omi', 'recovery', 'generic'))


def _is_current_retry(
    conversation: Optional[dict[str, Any]],
    request_id: str,
    attempt_count: Optional[int] = None,
) -> bool:
    if not conversation or conversation.get('processing_retry_id') != request_id:
        return False
    return attempt_count is None or conversation.get('processing_retry_attempt_count') == attempt_count


async def _ensure_conversation_vector(uid: str, conversation: dict[str, Any]) -> None:
    conversation_id = str(conversation['id'])
    summary_version_id = str(conversation.get('active_summary_version_id') or '')
    if not summary_version_id:
        if await asyncio.to_thread(_conversation_vector_present, uid, conversation_id):
            return
        raise RuntimeError('conversation_summary_version_missing_for_vector_refresh')

    content_sha256 = _summary_content_sha256(conversation)
    metadata = await asyncio.to_thread(_conversation_vector_metadata, uid, conversation_id)
    if (
        metadata
        and metadata.get('active_summary_version_id') == summary_version_id
        and metadata.get('summary_content_sha256') == content_sha256
    ):
        return
    write_result = await asyncio.to_thread(
        refresh_structured_summary_vector,
        uid,
        Conversation(**conversation),
        summary_version_id=summary_version_id,
        summary_content_sha256=content_sha256,
    )
    if write_result is None:
        raise RuntimeError('conversation_vector_write_unconfirmed')
    for delay_seconds in (0, 0.2, 0.5):
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        metadata = await asyncio.to_thread(_conversation_vector_metadata, uid, conversation_id)
        if (
            metadata
            and metadata.get('active_summary_version_id') == summary_version_id
            and metadata.get('summary_content_sha256') == content_sha256
        ):
            return
    raise RuntimeError('conversation_vector_metadata_unconfirmed')


async def _ensure_generic_phase_vector(
    uid: str,
    conversation: dict[str, Any],
    recovery_mode: Optional[str],
) -> None:
    conversation_id = str(conversation['id'])
    if recovery_mode == 'enrichment_only' and await asyncio.to_thread(
        _conversation_vector_present,
        uid,
        conversation_id,
    ):
        return
    await _ensure_conversation_vector(uid, conversation)


async def _write_and_confirm_enriched_vector(
    uid: str,
    conversation: dict[str, Any],
    summary_version_id: str,
) -> str:
    current = await asyncio.to_thread(
        conversations_db.get_conversation,
        uid,
        str(conversation['id']),
    )
    if not current or str(current.get('active_summary_version_id') or '') != str(summary_version_id):
        raise ConcurrentConversationRecoveryChangeError('active_summary_changed_before_vector_write')
    content_sha256 = _summary_content_sha256(current)
    write_result = await asyncio.to_thread(
        refresh_structured_summary_vector,
        uid,
        Conversation(**current),
        summary_version_id=summary_version_id,
        summary_content_sha256=content_sha256,
    )
    if write_result is None:
        raise RuntimeError('conversation_vector_write_unconfirmed')
    for delay_seconds in (0, 0.2, 0.5):
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        metadata = await asyncio.to_thread(_conversation_vector_metadata, uid, str(conversation['id']))
        if (
            metadata
            and metadata.get('active_summary_version_id') == summary_version_id
            and metadata.get('summary_content_sha256') == content_sha256
        ):
            current = await asyncio.to_thread(
                conversations_db.get_conversation,
                uid,
                str(conversation['id']),
            )
            if not current or str(current.get('active_summary_version_id') or '') != str(summary_version_id):
                raise ConcurrentConversationRecoveryChangeError('active_summary_changed_after_vector_write')
            return content_sha256
    raise RuntimeError('conversation_enriched_vector_metadata_unconfirmed')


async def recover_failed_conversation_summary(
    *,
    uid: str,
    conversation_id: str,
    request_id: str,
    client_context: Optional[str] = None,
    attempt_count: Optional[int] = None,
    config: Optional[SummaryProviderConfig] = None,
) -> str:
    conversation = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if not _is_current_retry(conversation, request_id, attempt_count):
        return 'superseded'
    status = getattr(conversation.get('status'), 'value', conversation.get('status'))
    if status not in {ConversationStatus.processing.value, ConversationStatus.completed.value}:
        return status or 'superseded'

    _, transcript_sha256 = build_hermes_recovery_source(conversation)
    expected_transcript_hash = transcript_grounding_hash(conversation.get('transcript_segments') or [])
    legacy_generic_summary_sha256 = (
        _summary_content_sha256(conversation)
        if conversation.get('processing_retry_mode') == 'enrichment_only'
        and not conversation.get('active_summary_version_id')
        and conversations_db.has_usable_conversation_summary(conversation)
        else None
    )
    if (
        conversation.get('processing_retry_source_request_id') == request_id
        and conversation.get('processing_retry_transcript_sha256')
        and conversation.get('processing_retry_transcript_sha256') != transcript_sha256
    ):
        await asyncio.to_thread(
            conversations_db.finish_conversation_processing_retry,
            uid,
            conversation_id,
            request_id,
            ConversationStatus.failed.value,
            error_code='conversation_transcript_changed',
            preserve_completed_summary=conversations_db.has_usable_conversation_summary(conversation),
            attempt_count=attempt_count,
        )
        return ConversationStatus.failed.value
    source_recorded = await asyncio.to_thread(
        conversations_db.record_conversation_processing_retry_source,
        uid,
        conversation_id,
        request_id,
        transcript_sha256,
        len(conversation.get('transcript_segments') or []),
        attempt_count=attempt_count,
        generic_summary_sha256=legacy_generic_summary_sha256,
    )
    if not source_recorded:
        return 'superseded'

    generic_version_id = _existing_generic_version_id(conversation)
    generic_applied = generic_version_id is not None
    recovery_mode = conversation.get('processing_retry_mode')
    try:
        if not generic_version_id:
            stock_structured = await asyncio.to_thread(
                generate_stock_conversation_summary,
                uid,
                Conversation(**conversation),
            )
            current_before_generic_apply = await asyncio.to_thread(
                conversations_db.get_conversation,
                uid,
                conversation_id,
            )
            if not _is_current_retry(current_before_generic_apply, request_id, attempt_count):
                return 'superseded'
            if current_before_generic_apply.get('active_summary_version_id') != conversation.get(
                'active_summary_version_id'
            ):
                raise ConcurrentConversationRecoveryChangeError('conversation_summary_changed_before_generic_apply')
            generic_summary = _stock_summary_payload(stock_structured)
            generic_trace_id = f'summary-retry:{conversation_id}:{request_id}:generic'
            apply_result = await apply_summary_update(
                uid=uid,
                conversation_id=conversation_id,
                trace_id=generic_trace_id,
                active_summary_version_id=conversation.get('active_summary_version_id'),
                summary=generic_summary,
                summary_kind='generic_recovered',
                summary_source='omi',
                require_based_on_match=True,
                expected_transcript_hash=expected_transcript_hash,
                require_source_match=True,
            )
            generic_version_id = apply_result.get('active_summary_version_id')
            if not generic_version_id:
                raise RuntimeError('Generic summary recovery did not return an active version')
            generic_applied = True

            recorded = await asyncio.to_thread(
                conversations_db.record_conversation_processing_retry_summary_applied,
                uid,
                conversation_id,
                request_id,
                generic_version_id,
                attempt_count=attempt_count,
            )
            if not recorded:
                return 'superseded'

        latest = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
        if not _is_current_retry(latest, request_id, attempt_count):
            return 'superseded'
        await _ensure_generic_phase_vector(uid, latest, recovery_mode)
        recorded = await asyncio.to_thread(
            conversations_db.record_conversation_processing_retry_generic_vector,
            uid,
            conversation_id,
            request_id,
            'completed',
            attempt_count=attempt_count,
        )
        if not recorded:
            return 'superseded'
    except Exception as error:
        error_code = SUMMARY_RECOVERY_FAILED if generic_applied else 'conversation_summary_failed'
        logger.exception(
            'Failed to recover generic conversation summary',
            extra={
                'uid': uid,
                'conversation_id': conversation_id,
                'request_id': request_id,
                'error_type': type(error).__name__,
            },
        )
        if generic_applied:
            await asyncio.to_thread(
                conversations_db.record_conversation_processing_retry_generic_vector,
                uid,
                conversation_id,
                request_id,
                'failed',
                attempt_count=attempt_count,
            )
        else:
            await asyncio.to_thread(
                conversations_db.finish_conversation_processing_retry,
                uid,
                conversation_id,
                request_id,
                ConversationStatus.failed.value,
                error_code=error_code,
                attempt_count=attempt_count,
            )
        return ConversationStatus.failed.value

    latest = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if not _is_current_retry(latest, request_id, attempt_count):
        return 'superseded'
    _, latest_transcript_sha256 = build_hermes_recovery_source(latest)
    if latest_transcript_sha256 != transcript_sha256:
        await asyncio.to_thread(
            conversations_db.finish_conversation_processing_retry,
            uid,
            conversation_id,
            request_id,
            ConversationStatus.failed.value,
            error_code='conversation_transcript_changed',
            preserve_completed_summary=conversations_db.has_usable_conversation_summary(latest),
            attempt_count=attempt_count,
        )
        return ConversationStatus.failed.value
    lease_renewed = await asyncio.to_thread(
        conversations_db.record_conversation_processing_retry_source,
        uid,
        conversation_id,
        request_id,
        transcript_sha256,
        len(latest.get('transcript_segments') or []),
        attempt_count=attempt_count,
        generic_summary_sha256=legacy_generic_summary_sha256,
    )
    if not lease_renewed:
        return 'superseded'
    latest_enrichment_state = latest.get('enrichment_state') or {}
    legacy_generic_baseline_changed = bool(
        generic_version_id == LEGACY_GENERIC_SUMMARY_BASELINE
        and (
            latest.get('active_summary_version_id') is not None
            or _summary_content_sha256(latest) != legacy_generic_summary_sha256
        )
    )
    versioned_generic_baseline_changed = bool(
        generic_version_id
        and generic_version_id != LEGACY_GENERIC_SUMMARY_BASELINE
        and str(latest.get('active_summary_version_id') or '') != str(generic_version_id)
        and latest_enrichment_state.get('kind') != 'recovered_enriched'
    )
    if legacy_generic_baseline_changed or versioned_generic_baseline_changed:
        await asyncio.to_thread(
            conversations_db.finish_conversation_processing_retry,
            uid,
            conversation_id,
            request_id,
            ConversationStatus.failed.value,
            error_code='conversation_summary_changed',
            preserve_completed_summary=True,
            attempt_count=attempt_count,
        )
        return ConversationStatus.failed.value
    enriched_version_id = _existing_recovered_version_id(latest)
    try:
        pending_state = latest.get('enrichment_state') or {}
        if enriched_version_id:
            pass
        elif (
            pending_state.get('status') == 'writeback_pending_canonical'
            and pending_state.get('kind') == 'recovered_enriched'
            and pending_state.get('trace_id')
            and str(pending_state.get('request_fingerprint') or '').strip()
            and is_current_summary_request_fingerprint_input(pending_state.get('request_fingerprint_input'))
            and latest.get('active_summary_version_id')
        ):
            stored_request_input = pending_state.get('request_fingerprint_input')
            apply_result = await apply_summary_update(
                uid=uid,
                conversation_id=conversation_id,
                trace_id=str(pending_state['trace_id']),
                active_summary_version_id=latest.get('active_summary_version_id'),
                summary={},
                summary_kind='recovered_enriched',
                require_canonical=True,
                expected_transcript_hash=expected_transcript_hash,
                require_source_match=True,
                replay_request_fingerprint_input=stored_request_input,
                canonical_retry_recorder=_processing_retry_canonical_recorder(
                    uid=uid,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    attempt_count=attempt_count,
                ),
            )
            enriched_version_id = apply_result.get('active_summary_version_id')
            if not enriched_version_id or apply_result.get('canonical_confirmed') is not True:
                raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed')
        else:
            isolated_config = await summary_provider_config_for_uid(uid, config)
            rerun_trace_id = None
            if (
                pending_state.get('status') == 'writeback_pending_canonical'
                and pending_state.get('kind') == 'recovered_enriched'
                and pending_state.get('trace_id')
            ):
                rerun_trace_id = _pending_canonical_rerun_trace_id(
                    conversation_id=conversation_id,
                    request_id=request_id,
                    active_summary_version_id=latest.get('active_summary_version_id'),
                    pending_state=pending_state,
                )
            apply_result = await invoke_hermes_recovery(
                uid=uid,
                conversation=latest,
                request_id=request_id,
                attempt_count=attempt_count,
                client_context=client_context,
                config=isolated_config,
                trace_id_override=rerun_trace_id,
            )
            enriched_version_id = apply_result.get('active_summary_version_id')
            if not enriched_version_id:
                raise RuntimeError('Hermes summary recovery did not return an active version')
            if apply_result.get('canonical_confirmed') is not True:
                raise CanonicalSummaryWriteUnconfirmedError('canonical_write_unconfirmed')
    except Exception as error:
        logger.exception(
            'Hermes enrichment failed after generic summary recovery',
            extra={
                'uid': uid,
                'conversation_id': conversation_id,
                'request_id': request_id,
                'error_type': type(error).__name__,
            },
        )
        try:
            after_error = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
            enriched_version_id = _existing_recovered_version_id(after_error or {})
            if not _is_current_retry(after_error, request_id, attempt_count):
                return 'superseded'
            if isinstance(
                error,
                (CanonicalSummaryRepairExhaustedError, CanonicalSummaryRetryReceiptUnconfirmedError),
            ):
                return ConversationStatus.failed.value
            if not enriched_version_id:
                after_error_state = (after_error or {}).get('enrichment_state') or {}
                canonical_pending = bool(
                    after_error_state.get('status') == 'writeback_pending_canonical'
                    and after_error_state.get('kind') == 'recovered_enriched'
                )
                await asyncio.to_thread(
                    conversations_db.record_conversation_processing_retry_enrichment,
                    uid,
                    conversation_id,
                    request_id,
                    (
                        'canonical_failed'
                        if isinstance(error, CanonicalSummaryWriteUnconfirmedError) or canonical_pending
                        else 'failed'
                    ),
                    attempt_count=attempt_count,
                )
                return ConversationStatus.failed.value
        except Exception:
            logger.exception(
                'Failed to persist retryable Hermes enrichment state',
                extra={'uid': uid, 'conversation_id': conversation_id, 'request_id': request_id},
            )
            return ConversationStatus.failed.value

    enriched = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if not _is_current_retry(enriched, request_id, attempt_count):
        return 'superseded'
    recorded = await asyncio.to_thread(
        conversations_db.record_conversation_processing_retry_enrichment,
        uid,
        conversation_id,
        request_id,
        'canonical_completed',
        summary_version_id=enriched_version_id,
        attempt_count=attempt_count,
    )
    if not recorded:
        return 'superseded'

    try:
        enriched_vector_sha256 = await _write_and_confirm_enriched_vector(
            uid,
            enriched,
            str(enriched_version_id),
        )
    except Exception as error:
        logger.exception(
            'Failed to confirm enriched conversation vector',
            extra={
                'uid': uid,
                'conversation_id': conversation_id,
                'request_id': request_id,
                'error_type': type(error).__name__,
            },
        )
        await asyncio.to_thread(
            conversations_db.record_conversation_processing_retry_enrichment,
            uid,
            conversation_id,
            request_id,
            'vector_failed',
            summary_version_id=enriched_version_id,
            vector_content_sha256=_summary_content_sha256(enriched),
            attempt_count=attempt_count,
        )
        return ConversationStatus.failed.value

    recorded = await asyncio.to_thread(
        conversations_db.record_conversation_processing_retry_enrichment,
        uid,
        conversation_id,
        request_id,
        'completed',
        summary_version_id=enriched_version_id,
        vector_content_sha256=enriched_vector_sha256,
        attempt_count=attempt_count,
    )
    if not recorded:
        return 'superseded'
    await enqueue_after_terminal_enrichment(uid, conversation_id)
    return ConversationStatus.completed.value


async def recover_memory_artwork_enrichment(uid: str, memory_id: str) -> dict[str, Any]:
    """Run or observe canonical enrichment recovery for durable artwork reconciliation."""

    claim = await claim_memory_artwork_enrichment_recovery(uid, memory_id)
    outcome = str(claim.get('outcome') or '')
    if outcome != 'claimed':
        return {'outcome': outcome}
    status = await recover_failed_conversation_summary(
        uid=uid,
        conversation_id=memory_id,
        request_id=str(claim['request_id']),
        attempt_count=int(claim.get('attempt_count') or 1),
    )
    return {'outcome': 'completed' if status == ConversationStatus.completed.value else status}


from ella.services.memory_artwork import (  # noqa: E402
    enqueue_after_terminal_enrichment,
    register_memory_artwork_enrichment_recovery,
)

register_memory_artwork_enrichment_recovery(recover_memory_artwork_enrichment)
