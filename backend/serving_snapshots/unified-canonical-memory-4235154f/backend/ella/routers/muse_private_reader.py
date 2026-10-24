"""Candidate private GET routes. Not mounted in the serving application."""
import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from ella.services.ai_consent import require_current_ai_consent
from ella.services.muse_private_reader import Dependencies, PrivateReader, ReaderError, digest

router = APIRouter(prefix='/v1/ella/muse', tags=['Private Muse source reads'])


async def production_reader(source_channel=None):
    if source_channel not in (None, 'muse_fictional_test'):
        raise ReaderError('reader_scope_unavailable',403)
    from ella.services.ai_consent import get_ai_consent_service
    from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_authority_identity
    from database.ella_postgres import get_ella_postgres_pool
    from database.muse_private_reader import SourceStore, capture_observer
    async def guard(uid):
        status = await asyncio.wait_for(asyncio.to_thread(get_ai_consent_service().status, uid), 4)
        consent = status.get('consent') or {}
        if status.get('subject_uid') != uid or status.get('authorized') is not True:
            raise ReaderError('current_consent_required', 403)
        runtime = await resolve_isolated_runtime(uid, target_mode='hermes-cloud-transcript')
        if runtime is None or runtime.uid != uid or runtime.status != 'active':
            raise ReaderError('runtime_unavailable', 503)
        identity = runtime_authority_identity(runtime)
        return digest({'runtime': identity.digest, 'receipt': consent.get('receipt_id'),
                       'profile': consent.get('profile_binding_id'), 'scope': consent.get('scope_hash'),
                       'policy': consent.get('policy_version'), 'decision': consent.get('decision'),
                       'reader_source_scope':source_channel or 'all_current_owner_sources'})
    pool = await get_ella_postgres_pool()
    # Reuses the existing history-cursor signing secret privately; no credential
    # fetch, token issuance, app principal substitution, or new grant.
    return PrivateReader(Dependencies(guard, SourceStore(pool, capture_observer, source_channel),
                                     os.environ.get('GUARDIAN_WEBHOOK_KEY', '')))


async def call_reader(operation):
    try:
        async with asyncio.timeout(10):
            return await operation()
    except ReaderError as error:
        raise HTTPException(error.status, detail={'code': error.code}) from None
    except (TimeoutError, Exception):
        raise HTTPException(503, detail={'code': 'private_reader_unavailable'}) from None


@router.get('/sources')
async def transcripts_list(response: Response, cursor: str = None, limit: int = Query(20, ge=1, le=50),
                           uid: str = Depends(require_current_ai_consent)):
    response.headers['Cache-Control'] = 'private, no-store'
    async def operation():
        reader = await production_reader()
        return await reader.sources(uid, cursor=cursor, limit=limit)
    return await call_reader(operation)


@router.get('/sources/{source_id}')
async def transcript_get(source_id: str, revision: str, response: Response,
                         uid: str = Depends(require_current_ai_consent)):
    response.headers['Cache-Control'] = 'private, no-store'
    async def operation():
        reader = await production_reader()
        return await reader.source(uid, source_id, revision)
    return await call_reader(operation)


@router.get('/necklace-status')
async def necklace_status(response: Response, uid: str = Depends(require_current_ai_consent)):
    response.headers['Cache-Control'] = 'private, no-store'
    async def operation():
        reader = await production_reader()
        return await reader.necklace_status(uid)
    return await call_reader(operation)


@router.get('/memory/{conversation_id}')
async def source_cited_memory(conversation_id: str, expected_summary_version: str, response: Response,
                             uid: str = Depends(require_current_ai_consent)):
    response.headers['Cache-Control'] = 'private, no-store'
    from ella.services.canonical_memory import read_memory
    async def operation():
        return await read_memory(uid, target_mode='hermes-cloud-chat',
            conversation_id=conversation_id, expected_summary_version=expected_summary_version)
    value = await call_reader(operation)
    return {'contract': 'ella.muse.source-cited-memory.v1', 'canonical_memory': value,
            'citation': {'conversation_id': conversation_id, 'summary_version': expected_summary_version},
            'raw_transcript_revision_not_proven': True}
