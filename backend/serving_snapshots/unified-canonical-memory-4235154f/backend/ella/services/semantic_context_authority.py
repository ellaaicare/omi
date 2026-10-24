"""Private read-only exact-owner authority snapshot for the existing workspace host.

Dependencies are backend-owned functions, never caller JSON or verdicts. No
credentials, provider invocation, policy mutation or canonical write occurs here.
"""
from __future__ import annotations
import asyncio
import json
from dataclasses import asdict

from ella.services.canonical_summary_source import canonical_source_from_conversation, canonical_source_sha256
from models.hermes_cloud_enrichment_contract import transcript_source

from ella.services.transcript_processing_hold import require_transcript_processing_active

CONTRACT = 'ella-semantic-context-authority-v1'
MAX_RESPONSE_BYTES = 196608
RUNTIME_FIELDS = ('uid', 'provider', 'status', 'binding_id', 'revision', 'profile_name',
                  'agent_id', 'workspace_root', 'account_user_id', 'profile_user_id')


class ContextAuthorityRejected(ValueError):
    pass


def reject(code):
    raise ContextAuthorityRejected(code)


async def read_context_authority(*, uid, conversation_id, read_conversation,
                                 consent, resolve, identity, repository=None):
    """Two current backend samples; the host must repeat this around its own work."""
    if (not isinstance(uid, str) or not uid or len(uid) > 256
            or not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 256):
        reject('context_authority_identity_invalid')

    from ella.services.semantic_host_context import HostReadBudget
    host_read_budget=HostReadBudget()
    async def sample():
        await require_transcript_processing_active(uid)
        if await asyncio.to_thread(consent, uid) != uid:
            reject('context_authority_consent_required')
        runtime = await resolve(uid, repository=repository, target_mode='hermes-cloud-transcript',host_read_budget=host_read_budget)
        if (runtime is None or runtime.uid != uid or runtime.provider != 'hermes'
                or runtime.status != 'active' or type(runtime.revision) is not int or runtime.revision < 1
                or any(not getattr(runtime, key, '') for key in
                       ('binding_id', 'profile_name', 'agent_id', 'workspace_root', 'account_user_id', 'profile_user_id'))):
            reject('context_authority_runtime_required')
        await require_transcript_processing_active(uid)
        from database._client import db
        from database.normal_account_lifecycle import assert_existing_mapping
        await asyncio.to_thread(assert_existing_mapping,db,uid,runtime)
        authority = identity(runtime)
        if authority.uid != uid or authority.target_mode not in {'retained', 'hermes-cloud-transcript', 'hermes-chat'}:
            reject('context_authority_runtime_owner_required')
        doc = await asyncio.to_thread(read_conversation, uid, conversation_id)
        if (not isinstance(doc, dict) or doc.get('id') != conversation_id or doc.get('uid', uid) != uid
                or doc.get('status') != 'completed' or not doc.get('active_summary_version_id')):
            reject('context_authority_committed_source_required')
        await require_transcript_processing_active(uid)
        from database._client import db
        from database.normal_account_lifecycle import require_callback_source
        await asyncio.to_thread(require_callback_source,db,uid,conversation_id,doc)
        source = canonical_source_from_conversation(uid=uid, conversation_id=conversation_id, conversation=doc)
        return {'contract': CONTRACT, 'uid': uid, 'conversation_id': conversation_id,
                'canonical_source': source, 'source_sha256': canonical_source_sha256(source),
                'transcript_sha256': transcript_source(doc)[1],
                'source_version': str(source.get('finished_at') or source.get('started_at') or ''),
                'active_summary_version_id': doc['active_summary_version_id'],
                'runtime': {key: getattr(runtime, key) for key in RUNTIME_FIELDS},
                'runtime_identity': asdict(authority)}

    async with asyncio.timeout(20):
        first = await sample()
        encoded = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            reject('context_authority_response_too_large')
        if await sample() != first:
            reject('context_authority_changed')
        # Consent can change during the second source read, so check once more.
        if await asyncio.to_thread(consent, uid) != uid:
            reject('context_authority_consent_changed')
        await require_transcript_processing_active(uid)
        return first
