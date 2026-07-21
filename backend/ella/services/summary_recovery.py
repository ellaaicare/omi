"""Shared Hermes summary generation and failed-conversation recovery."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import database.conversations as conversations_db
from ella.services.hermes_session import canonical_omi_session_key, safe_session_component
from ella.services.summary_writeback import write_conversation_summary
from models.conversation import Conversation, ConversationStatus
from utils.conversations.generic_summary import generate_stock_conversation_summary
from utils.conversations.vector import save_structured_vector

logger = logging.getLogger(__name__)

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
SUMMARY_RECOVERY_FAILED = 'conversation_summary_recovery_failed'


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


def compact_text(value: str, limit: int) -> str:
    text = (value or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '\n\n[truncated]'


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
        if not config.hermes_api_key:
            raise RuntimeError('No Hermes summary API key configured')
        headers = {
            'Authorization': f'Bearer {config.hermes_api_key}',
            'Content-Type': 'application/json',
            'X-Hermes-Session-Id': session_id,
            'X-Hermes-Session-Key': session_key,
            'X-Trace-Id': trace_id,
        }
        url = config.hermes_url
        model = config.hermes_model
        max_tokens = 900
    else:
        if not config.legacy_api_key:
            raise RuntimeError('No legacy summary API key configured')
        headers = {'Authorization': f'Bearer {config.legacy_api_key}', 'Content-Type': 'application/json'}
        url = config.legacy_url
        model = config.legacy_model
        max_tokens = 800

    async with async_client_factory(timeout=config.timeout_seconds) as client:
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
) -> dict[str, Any]:
    return await write_conversation_summary(
        uid=uid,
        conversation_id=conversation_id,
        title=summary['title'],
        overview=summary['overview'],
        emoji=summary['emoji'],
        category=summary['category'],
        summary_source=summary_source,
        summary_kind=summary_kind,
        correction_id=correction_id,
        based_on_version_id=active_summary_version_id,
        set_active=True,
        trace_id=trace_id,
        ella_tags=summary.get('ella_tags') or ['omi'],
        ella_signal=summary.get('ella_signal') or {},
    )


def _structured_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    structured = conversation.get('structured') or {}
    return {
        'title': structured.get('title') or conversation.get('title') or '',
        'overview': structured.get('overview') or conversation.get('overview') or '',
        'emoji': structured.get('emoji') or conversation.get('emoji') or '',
        'category': str(structured.get('category') or conversation.get('category') or 'other'),
    }


def _format_transcript(conversation: dict[str, Any]) -> str:
    lines = []
    for segment in conversation.get('transcript_segments') or []:
        text = str(segment.get('text') or '').strip()
        if not text:
            continue
        speaker = segment.get('speaker') or segment.get('speaker_name')
        if not speaker:
            speaker = 'User' if segment.get('is_user') else 'Speaker'
        lines.append(f'{speaker}: {text}')
    return '\n\n'.join(lines)


def _build_recovery_prompt(conversation: dict[str, Any], client_context: Optional[str]) -> str:
    structured = _structured_summary(conversation)
    transcript = _format_transcript(conversation)
    return f"""You are Ella, the user's companion summary writer.

Recover an OMI conversation whose first summary attempt failed. Use the complete transcript and durable companion context available in this Hermes session. Return JSON only.

Rules:
- Produce one accurate, warm, specific summary grounded in the transcript.
- overview must start with "[Ella] ".
- Do not invent details or expose raw speaker labels when a natural description is available.
- title must be short and contain no markdown.
- category should be one of: personal, family, education, health, technology, work, business, finance, legal, media, music, news, travel, other.
- Include ella_tags and ella_signal for downstream ranking.

Optional user context:
{client_context or '[none]'}

Existing partial summary:
{json.dumps(structured, ensure_ascii=False, indent=2)}

Transcript:
{compact_text(transcript, 60000)}

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


def _existing_recovered_version_id(conversation: dict[str, Any]) -> Optional[str]:
    retry_version_id = conversation.get('processing_retry_enriched_version_id')
    if retry_version_id:
        return str(retry_version_id)
    enrichment_state = conversation.get('enrichment_state') or {}
    if enrichment_state.get('status') == 'writeback_applied' and enrichment_state.get('kind') == 'recovered_enriched':
        active_version_id = conversation.get('active_summary_version_id')
        return str(active_version_id) if active_version_id else None
    return None


def _existing_generic_version_id(conversation: dict[str, Any]) -> Optional[str]:
    retry_version_id = conversation.get('processing_retry_summary_version_id')
    if retry_version_id:
        return str(retry_version_id)
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


def _is_current_retry(conversation: Optional[dict[str, Any]], request_id: str) -> bool:
    return bool(conversation and conversation.get('processing_retry_id') == request_id)


async def recover_failed_conversation_summary(
    *,
    uid: str,
    conversation_id: str,
    request_id: str,
    client_context: Optional[str] = None,
) -> str:
    conversation = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if not _is_current_retry(conversation, request_id):
        return 'superseded'
    status = getattr(conversation.get('status'), 'value', conversation.get('status'))
    if status not in {ConversationStatus.processing.value, ConversationStatus.completed.value}:
        return status or 'superseded'

    generic_version_id = _existing_generic_version_id(conversation)
    generic_applied = generic_version_id is not None
    try:
        if not generic_version_id:
            stock_structured = await asyncio.to_thread(
                generate_stock_conversation_summary,
                uid,
                Conversation(**conversation),
            )
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
            )
            if not recorded:
                return 'superseded'

        latest = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
        if not _is_current_retry(latest, request_id):
            return 'superseded'
        await asyncio.to_thread(save_structured_vector, uid, Conversation(**latest))
        finished = await asyncio.to_thread(
            conversations_db.finish_conversation_processing_retry,
            uid,
            conversation_id,
            request_id,
            ConversationStatus.completed.value,
        )
        if not finished:
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
        await asyncio.to_thread(
            conversations_db.finish_conversation_processing_retry,
            uid,
            conversation_id,
            request_id,
            ConversationStatus.failed.value,
            error_code=error_code,
        )
        return ConversationStatus.failed.value

    latest = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
    if not _is_current_retry(latest, request_id):
        return 'superseded'
    existing_enriched_version_id = _existing_recovered_version_id(latest)
    if existing_enriched_version_id:
        await asyncio.to_thread(
            conversations_db.record_conversation_processing_retry_enrichment,
            uid,
            conversation_id,
            request_id,
            'completed',
            summary_version_id=existing_enriched_version_id,
        )
        return ConversationStatus.completed.value

    hermes_trace_id = f'summary-retry:{conversation_id}:{request_id}:hermes'
    try:
        summary = await generate_summary_from_prompt(
            prompt=_build_recovery_prompt(latest, client_context),
            fallback=_structured_summary(latest),
            session_id=':'.join(
                [
                    'summary-recovery',
                    safe_session_component(uid),
                    safe_session_component(conversation_id),
                    safe_session_component(request_id),
                ]
            ),
            session_key=canonical_omi_session_key(uid),
            trace_id=hermes_trace_id,
            required_tags=('omi', 'recovery'),
            config=default_summary_provider_config(),
        )
        apply_result = await apply_summary_update(
            uid=uid,
            conversation_id=conversation_id,
            trace_id=hermes_trace_id,
            active_summary_version_id=latest.get('active_summary_version_id'),
            summary=summary,
            summary_kind='recovered_enriched',
        )
        enriched_version_id = apply_result.get('active_summary_version_id')
        if not enriched_version_id:
            raise RuntimeError('Hermes summary recovery did not return an active version')

        enriched = await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)
        if not _is_current_retry(enriched, request_id):
            return 'superseded'
        await asyncio.to_thread(save_structured_vector, uid, Conversation(**enriched))
        await asyncio.to_thread(
            conversations_db.record_conversation_processing_retry_enrichment,
            uid,
            conversation_id,
            request_id,
            'completed',
            summary_version_id=enriched_version_id,
        )
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
            await asyncio.to_thread(
                conversations_db.record_conversation_processing_retry_enrichment,
                uid,
                conversation_id,
                request_id,
                'failed',
            )
        except Exception:
            logger.exception(
                'Failed to persist retryable Hermes enrichment state',
                extra={'uid': uid, 'conversation_id': conversation_id, 'request_id': request_id},
            )

    return ConversationStatus.completed.value
