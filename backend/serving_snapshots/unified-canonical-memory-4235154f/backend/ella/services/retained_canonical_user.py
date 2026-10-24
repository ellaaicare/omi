"""Owned transcript ingestion only; no model calls or invented write receipts."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone

from ella.services.canonical_summary_source import canonical_source_from_conversation, canonical_source_sha256
from models.hermes_cloud_enrichment_contract import build_enrichment_identity, transcript_source


class CanonicalUserUnconfirmed(ValueError):
    pass


async def confirm_retained_canonical_user(*, uid, receipt, expected_transcript, event_store):
    if (not isinstance(receipt, dict) or receipt.get('confirmed') is not True
            or receipt.get('transcript_sha256') != expected_transcript
            or not receipt.get('event_id') or not receipt.get('source_identity')):
        raise CanonicalUserUnconfirmed('canonical_user_receipt_required')
    actual = await event_store.get_event(uid=uid, event_id=receipt['event_id'], source_identity=receipt['source_identity'])
    if (not isinstance(actual, dict) or actual.get('uid') != uid or actual.get('canonical_identity') != uid
            or actual.get('event_id') != receipt['event_id'] or actual.get('source_identity') != receipt['source_identity']
            or actual.get('role') != 'user' or actual.get('channel') != 'omi_transcript'
            or (actual.get('source_ref') or {}).get('transcript_sha256') != expected_transcript
            or (actual.get('source_ref') or {}).get('source_sha256') != receipt.get('source_sha256')
            or hashlib.sha256(str(actual.get('text') or '').encode()).hexdigest() != receipt.get('text_sha256')):
        raise CanonicalUserUnconfirmed('canonical_user_readback_unconfirmed')
    return receipt


async def write_retained_canonical_user(*, uid, conversation_id, conversation,
                                      expected_client, expected_transcript, event_store, event_type):
    """Caller must durably START and check current runtime/consent before and after.

    The event store's exact-owner readback confirms an actual event, not just a
    computed ID or acknowledgement. Unknown writes remain unknown to the caller.
    """
    if (not isinstance(conversation, dict) or conversation.get('id') != conversation_id
            or conversation.get('uid', uid) != uid or conversation.get('status') != 'completed'
            or not conversation.get('active_summary_version_id')):
        raise CanonicalUserUnconfirmed('canonical_user_committed_source_required')
    queued = build_enrichment_identity(uid=uid, conversation_id=conversation_id, conversation=conversation)
    if queued.client_interaction_id != expected_client or transcript_source(conversation)[1] != expected_transcript:
        raise CanonicalUserUnconfirmed('canonical_user_queued_source_changed')
    source = canonical_source_from_conversation(uid=uid, conversation_id=conversation_id, conversation=conversation)
    source_hash = canonical_source_sha256(source)
    material = {'uid': uid, 'conversation_id': conversation_id, 'source_sha256': source_hash,
                'transcript_sha256': expected_transcript, 'client_interaction_id': expected_client}
    key = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    event_id = 'retained-omi-user-' + key
    source_identity = 'hermes:omi_transcript:source:' + key
    event = event_type(uid=uid, canonical_identity=uid, event_id=event_id,
        session_id='retained-omi-' + hashlib.sha256(uid.encode()).hexdigest(),
        channel='omi_transcript', provider='hermes', role='user', text=source['transcript'],
        started_at=source['started_at'], ended_at=source['finished_at'] or None,
        privacy_scope='user_private', scan_policy='none',
        source_ref={'source_identity': source_identity, **material},
        metadata={'adapter': 'retained-omi-transcript', 'event_revision': 1})
    # Validate the real canonical serializer before attempting a durable write.
    expected = event.normalized()
    # Match the canonical PostgreSQL reader's UTC ISO projection exactly.
    # Keep identity, provenance, text, metadata and all other comparisons strict.
    for field in ('started_at', 'ended_at'):
        if isinstance(expected.get(field), datetime):
            expected[field] = expected[field].astimezone(timezone.utc).isoformat()
    await event_store.write_batch([event])
    actual = await event_store.get_event(uid=uid, event_id=event_id, source_identity=source_identity)
    for field in ('uid', 'canonical_identity', 'event_id', 'source_identity', 'session_id', 'channel',
                  'provider', 'role', 'text', 'started_at', 'ended_at', 'privacy_scope', 'scan_policy', 'source_ref', 'metadata'):
        if not isinstance(actual, dict) or actual.get(field) != expected.get(field):
            raise CanonicalUserUnconfirmed('canonical_user_readback_unconfirmed')
    return {'event_id': event_id, 'source_identity': source_identity, 'source_sha256': source_hash,
            'transcript_sha256': expected_transcript,
            'text_sha256': hashlib.sha256(source['transcript'].encode()).hexdigest(), 'confirmed': True}
