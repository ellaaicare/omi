"""Owner-scoped, inference-free retention of a completed short discarded capture.

One metadata field/one document transaction. Never calls processing, provisioning,
enrichment, a model, or a queue. The source and its original discard history stay
unchanged. Idempotency is scoped to the authenticated UID and exact source CID.
"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

FIELD = 'user_keep_v1'
CONTRACT = 'ella-user-explicit-keep-v1'
OUTBOX = 'ella_hermes_cloud_enrichment_outbox'
NATIVE_SOURCES = frozenset({'omi', 'friend', 'fieldy', 'bee', 'plaud', 'frame',
    'friend_com', 'apple_watch', 'phone', 'desktop', 'openglass', 'sdcard', 'limitless'})
REQUEST_KEYS = {'contract', 'operation_id', 'expected_source_snapshot_sha256',
                'expected_transcript_sha256', 'expected_active_summary_version_id'}
MARKER_KEYS = {'contract', 'conversation_id', 'owner_ref_sha256', 'operation_id',
               'request_sha256', 'source_snapshot_sha256', 'transcript_sha256',
               'original_discard_reason', 'kept_at', 'summary_status',
               'provider_dispatched', 'canonical_write'}
PROTECTED_FIELDS = ('thin_cloudbot_source_binding', 'semantic_publication_policy',
    'semantic_producer_executions_v1', 'semantic_publication_operations',
    'semantic_publication_receipts', 'semantic_retained_completion_jobs_v1',
    'summary_enrichment', 'enrichment_state', 'processing_retry_id',
    'processing_retry_state', 'processing_error', 'correction_state')

class KeepError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)

def require(ok, code, status=409):
    if not ok:
        raise KeepError(code, status)

def _json_default(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    raise ValueError('noncanonical_source')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False, default=_json_default).encode()).hexdigest()

def owner_hash(uid):
    return hashlib.sha256(uid.encode()).hexdigest()

def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False

def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

def validate_ids(uid, cid):
    require(type(uid) is str and 0 < len(uid) <= 128 and '/' not in uid,
            'user_keep_identity_invalid', 400)
    require(_uuid(cid), 'user_keep_conversation_id_invalid', 400)

def validate_request(body):
    require(type(body) is dict and set(body) == REQUEST_KEYS,
            'user_keep_request_shape', 400)
    require(body['contract'] == CONTRACT and _uuid(body['operation_id']),
            'user_keep_request_contract', 400)
    require(_hash(body['expected_source_snapshot_sha256'])
            and _hash(body['expected_transcript_sha256'])
            and body['expected_active_summary_version_id'] is None,
            'user_keep_precondition_shape', 400)

def _source_image(raw, uid, cid, decode):
    require(type(raw) is dict and raw.get('id') == cid, 'user_keep_source_missing', 404)
    require('uid' not in raw or raw['uid'] == uid, 'user_keep_source_owner_mismatch', 403)
    require(raw.get('is_locked') is not True, 'user_keep_source_locked', 402)
    require(all(raw.get(k) is None for k in PROTECTED_FIELDS),
            'user_keep_protected_source')
    require(type(raw.get('source')) is str and raw['source'] in NATIVE_SOURCES,
            'user_keep_native_source_required')
    require(raw.get('summary_versions') is None
            or (type(raw['summary_versions']) is list and not raw['summary_versions']),
            'user_keep_summary_history_present')
    require(raw.get('status') == 'completed' and raw.get('capture_state') == 'terminal'
            and type(raw.get('capture_protocol_version')) is int
            and raw['capture_protocol_version'] == 2, 'user_keep_source_not_terminal')
    require(raw.get('discarded') is True and raw.get('discard_reason') == 'discarded_trivial'
            and raw.get('active_summary_version_id') is None,
            'user_keep_source_not_short_discard')
    structured = raw.get('structured')
    require(type(structured) is dict and not structured.get('title')
            and not structured.get('overview'), 'user_keep_summary_already_present')
    decoded = decode(raw, uid)
    require(type(decoded) is dict and type(decoded.get('transcript_segments')) is list,
            'user_keep_transcript_unavailable')
    segments = decoded['transcript_segments']
    require(0 < len(segments) <= 10000 and all(type(s) is dict
            and type(s.get('text')) is str for s in segments),
            'user_keep_transcript_invalid')
    require(any(s['text'].strip() for s in segments), 'user_keep_transcript_empty')
    require(sum(len(s['text']) for s in segments) <= 100000,
            'user_keep_transcript_bound')
    try:
        transcript_hash = digest(segments)
        image = {k: raw.get(k) for k in ('id', 'status', 'source', 'discarded',
            'discard_reason', 'discard_classifier_provenance', 'created_at',
            'started_at', 'finished_at', 'capture_protocol_version',
            'capture_generation', 'capture_state', 'active_summary_version_id')}
        image.update(owner_ref_sha256=owner_hash(uid), transcript_sha256=transcript_hash)
        snapshot = digest(image)
    except (ValueError, TypeError, OverflowError):
        raise KeepError('user_keep_source_uncanonical') from None
    return {'source_snapshot_sha256': snapshot, 'transcript_sha256': transcript_hash,
            'active_summary_version_id': None}

def _marker(raw, uid, cid):
    value = raw.get(FIELD)
    if value is None:
        return None
    require(type(value) is dict and set(value) == MARKER_KEYS,
            'user_keep_stored_receipt_shape')
    require(value['contract'] == CONTRACT and value['conversation_id'] == cid
        and value['owner_ref_sha256'] == owner_hash(uid)
        and _uuid(value['operation_id']) and _hash(value['request_sha256'])
        and _hash(value['source_snapshot_sha256']) and _hash(value['transcript_sha256'])
        and value['original_discard_reason'] == 'discarded_trivial'
        and isinstance(value['kept_at'], datetime) and value['kept_at'].tzinfo is not None
        and value['summary_status'] == 'not_generated'
        and value['provider_dispatched'] is False and value['canonical_write'] is False,
        'user_keep_stored_receipt_binding')
    return value

def _public(marker):
    if marker is None:
        return None
    return {k: (v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v)
            for k, v in marker.items() if k != 'owner_ref_sha256'}

def _ref(db, uid, cid):
    return db.collection('users').document(uid).collection('conversations').document(cid)

def _read(ref, tx):
    snap = ref.get(transaction=tx, timeout=3, retry=None)
    return snap.to_dict() if snap.exists else None

def _no_prior_enrichment(db, tx, uid, cid):
    # Exact owner+CID equality, bounded metadata-only query. Any previous job,
    # not just known quarantine flags, excludes this retention recovery branch.
    query = db.collection(OUTBOX).where(filter=FieldFilter('uid', '==', uid)).where(
        filter=FieldFilter('conversation_id', '==', cid)).select(
        ['uid', 'conversation_id', 'status']).limit(1)
    require(not list(query.stream(transaction=tx, timeout=3, retry=None)),
            'user_keep_existing_enrichment_job')

def _processing(db, tx):
    snap = db.collection('ella_normal_enrichment_activation').document(
        'continuation-v2').get(transaction=tx, timeout=3, retry=None)
    policy = snap.to_dict() if snap.exists else {}
    return 'paused' if policy.get('state') == 'paused' else 'not_requested'

def _view(cid, image, marker, processing, **extra):
    return {'contract': CONTRACT, 'conversation_id': cid,
        'preconditions': {'expected_source_snapshot_sha256': image['source_snapshot_sha256'],
            'expected_transcript_sha256': image['transcript_sha256'],
            'expected_active_summary_version_id': None},
        'state': 'kept' if marker else 'not_kept', 'receipt': _public(marker),
        'summary_status': 'not_generated', 'processing_state': processing,
        'provider_dispatched': False, 'canonical_write': False, **extra}

@firestore.transactional
def _read_transaction(tx, db, uid, cid, decode, authorize):
    authorize(uid)
    raw = _read(_ref(db, uid, cid), tx)
    image = _source_image(raw, uid, cid, decode)
    _no_prior_enrichment(db, tx, uid, cid)
    marker = _marker(raw, uid, cid)
    if marker:
        require(marker['source_snapshot_sha256'] == image['source_snapshot_sha256']
            and marker['transcript_sha256'] == image['transcript_sha256'],
            'user_keep_source_changed_after_save')
    processing = _processing(db, tx)
    authorize(uid)
    return _view(cid, image, marker, processing)

@firestore.transactional
def _save_transaction(tx, db, uid, cid, body, decode, authorize):
    authorize(uid)
    ref = _ref(db, uid, cid)
    raw = _read(ref, tx)
    image = _source_image(raw, uid, cid, decode)
    _no_prior_enrichment(db, tx, uid, cid)
    marker = _marker(raw, uid, cid)
    require(body['expected_source_snapshot_sha256'] == image['source_snapshot_sha256']
        and body['expected_transcript_sha256'] == image['transcript_sha256'],
        'user_keep_source_precondition_changed')
    fingerprint = digest({'uid': uid, 'conversation_id': cid, 'request': body})
    processing = _processing(db, tx)
    authorize(uid)
    if marker:
        require(marker['source_snapshot_sha256'] == image['source_snapshot_sha256']
            and marker['transcript_sha256'] == image['transcript_sha256'],
            'user_keep_source_changed_after_save')
        if marker['operation_id'] == body['operation_id']:
            require(marker['request_sha256'] == fingerprint, 'user_keep_operation_conflict')
        return _view(cid, image, marker, processing, created=False, already_kept=True,
            requested_operation_id=body['operation_id'])
    marker = {'contract': CONTRACT, 'conversation_id': cid,
        'owner_ref_sha256': owner_hash(uid), 'operation_id': body['operation_id'],
        'request_sha256': fingerprint, 'source_snapshot_sha256': image['source_snapshot_sha256'],
        'transcript_sha256': image['transcript_sha256'],
        'original_discard_reason': raw['discard_reason'],
        'kept_at': datetime.now(timezone.utc), 'summary_status': 'not_generated',
        'provider_dispatched': False, 'canonical_write': False}
    tx.update(ref, {FIELD: marker})
    return _view(cid, image, marker, processing, created=True, already_kept=False,
        requested_operation_id=body['operation_id'])

def read_keep(db, uid, cid, *, decode, authorize):
    validate_ids(uid, cid)
    return _read_transaction(db.transaction(max_attempts=3), db, uid, cid, decode, authorize)

def save_keep(db, uid, cid, body, *, decode, authorize):
    validate_ids(uid, cid)
    validate_request(body)
    return _save_transaction(db.transaction(max_attempts=3), db, uid, cid, body, decode, authorize)

def list_kept(db, uid, *, limit=20, authorize):
    require(type(uid) is str and 0 < len(uid) <= 128 and '/' not in uid,
            'user_keep_identity_invalid', 400)
    require(type(limit) is int and 1 <= limit <= 20, 'user_keep_list_bound', 400)
    authorize(uid)
    query = db.collection('users').document(uid).collection('conversations').order_by(
        FIELD + '.kept_at', direction=firestore.Query.DESCENDING).select(
        ['id', FIELD, 'thin_cloudbot_source_binding']).limit(limit + 1)
    rows = list(query.stream(timeout=3, retry=None))
    items = []
    for snap in rows[:limit]:
        raw = snap.to_dict()
        require(type(raw) is dict and raw.get('id') == snap.id
            and raw.get('thin_cloudbot_source_binding') is None,
            'user_keep_list_source_invalid')
        marker = _marker(raw, uid, snap.id)
        require(marker is not None, 'user_keep_list_marker_missing')
        items.append({'conversation_id': snap.id, 'receipt': _public(marker),
                      'needs_current_read': True})
    authorize(uid)
    return {'contract': CONTRACT, 'items': items, 'limit': limit,
        'more_available': len(rows) > limit, 'bounded_recent_index': True,
        'provider_dispatched': False, 'canonical_write': False}
