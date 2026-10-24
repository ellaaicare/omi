"""Owner-scoped, read-only Thin pending discovery. NOT registered or enabled.

The authenticated transport supplies RequestAuthority, never tool JSON. Returned
items are hints, not claims, grants or permission to skip the existing exact fetch.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

CONTRACT = 'ella-thin-pending-recordings-v1'
TOOL_NAME = 'get_pending_recordings'
BINDING = 'thin_cloudbot_source_binding'
MARKER = 'mcp_synthetic_pending_v1'
SCAN_LIMIT = 32
TEST_OWNER_SHA256 = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'
LIVE = 'ella-thin-cloudbot-live-capture-v1'
OFFLINE = 'ella-thin-cloudbot-offline-capture-v1'


class DiscoveryHold(ValueError):
    """Closed diagnostic code only."""


def need(ok, code):
    if not ok:
        raise DiscoveryHold(code)


def canonical_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def arguments(value):
    need(type(value) is dict and set(value) <= {'limit', 'cursor'}, 'thin_discovery_arguments')
    need('cursor' not in value or type(value['cursor']) is dict, 'thin_discovery_cursor')
    # Each page deliberately returns at most ONE eligible item.
    need(type(value.get('limit', 1)) is int and value.get('limit', 1) == 1,
         'thin_discovery_limit')
    return 1


def scope_digest(authority):
    return hashlib.sha256(json.dumps({'owner': authority.uid, 'scope': authority.scope},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse_cursor(value, authority):
    if value is None:
        return None
    need(type(value) is dict and set(value) == {'expires_at', 'conversation_id', 'scope_sha256'},
         'thin_discovery_cursor')
    need(value['scope_sha256'] == scope_digest(authority), 'thin_discovery_cursor_scope')
    need(canonical_uuid(value['conversation_id']), 'thin_discovery_cursor')
    stamp = value['expires_at']
    need(type(stamp) is str and len(stamp) <= 40, 'thin_discovery_cursor')
    try:
        expiry = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        raise DiscoveryHold('thin_discovery_cursor') from None
    need(expiry.tzinfo is not None and expiry.utcoffset().total_seconds() == 0,
         'thin_discovery_cursor')
    return {'expires_at': expiry, 'conversation_id': value['conversation_id']}


def export_cursor(position, authority):
    return {'expires_at': position['expires_at'].isoformat(),
            'conversation_id': position['conversation_id'],
            'scope_sha256': scope_digest(authority)}


def production_helpers():
    from ella.services import mcp_synthetic_pending as pending
    from ella.services import mcp_summary_enrichment as summaries
    from ella.services.mcp_source_report import transcript_projection
    return pending.validate, summaries.preconditions, transcript_projection


def projection(uid, cid, document, authority, now, helpers):
    """Never returns raw source, title, transcript, private witness or UID."""
    need(canonical_uuid(cid), 'thin_discovery_source_id')
    need(type(document) is dict and document.get('id') == cid,
         'thin_discovery_source_identity')
    for field in ('uid', 'user_id'):
        need(document.get(field) in (None, uid), 'thin_discovery_source_owner')
    binding = document.get(BINDING)
    need(type(binding) is dict and binding.get('contract') in {LIVE, OFFLINE}
         and binding.get('source_id') == cid
         and binding.get('consent_scope') == authority.scope
         and binding.get('authority') == authority.witness, 'thin_discovery_source_scope')
    validate, preconditions, transcript_projection = helpers
    marker = validate(document.get(MARKER), uid, cid)
    need(marker['state'] == 'pending' and marker['ready_at'] <= now < marker['expires_at'],
         'thin_discovery_pending_closed')
    admission = document.get('mcp_enrichment_admission')
    need(type(admission) is dict and admission.get('consumer') == 'muse'
         and admission.get('job_id') == marker['operation_id']
         and admission.get('status') == 'pending', 'thin_discovery_consumer')
    ledger = document.get('mcp_summary_enrichment_operations_v1', {})
    need(type(ledger) is dict and not ledger, 'thin_discovery_already_effected')
    pre = preconditions(uid, cid, document, contract='omi-mcp-summary-enrichment-v2')
    need(pre['expected_snapshot_sha256'] == marker['expected_snapshot_sha256']
         and pre['expected_active_summary_version_id'] == marker['expected_active_summary_version_id']
         and transcript_projection(document)['sha256'] == marker['source_sha256'],
         'thin_discovery_source_changed')
    return {
        'conversation_id': cid, 'available': True, 'run_id': marker['run_id'],
        'operation_id': marker['operation_id'],
        'source_sha256': marker['source_sha256'],
        'expected_snapshot_sha256': marker['expected_snapshot_sha256'],
        'expected_active_summary_version_id': marker['expected_active_summary_version_id'],
        'ready_at': marker['ready_at'].isoformat(),
        'expires_at': marker['expires_at'].isoformat(),
    }


class FirestorePendingRepository:
    def __init__(self, db):
        self.db = db

    def candidate_page(self, authority, now, after=None):
        from google.cloud.firestore_v1 import FieldFilter
        scope = authority.scope
        collection = self.db.collection('users').document(authority.uid).collection('conversations')
        # Exact authenticated owner path, current dedicated scope, still-unexpired
        # pending only. Requires the included COLLECTION-scope composite index.
        query = collection
        for key, value in (
            (BINDING + '.consent_scope.receipt_id', scope['receipt_id']),
            (BINDING + '.consent_scope.account_epoch_hash', scope['account_epoch_hash']),
            (MARKER + '.state', 'pending'),
        ):
            query = query.where(filter=FieldFilter(key, '==', value))
        query = (query.where(filter=FieldFilter(MARKER + '.expires_at', '>', now))
                 .order_by(MARKER + '.expires_at').order_by('__name__')
                 .select(['id', MARKER + '.expires_at']).limit(SCAN_LIMIT))
        if after is not None:
            query = query.start_after({MARKER + '.expires_at': after['expires_at'],
                                       '__name__': collection.document(after['conversation_id'])})
        result = list(query.stream(timeout=5, retry=None))
        for row in result:
            need(row.reference.parent.path == collection.path, 'thin_discovery_query_owner')
        return [{'conversation_id': row.id, 'expires_at': row.to_dict()[MARKER]['expires_at']}
                for row in result]

    def read_current(self, authority, cid, clock):
        from google.cloud.firestore_v1 import transactional
        from database.conversations import _prepare_conversation_for_read
        ref = self.db.collection('users').document(authority.uid).collection('conversations').document(cid)

        @transactional
        def read(tx):
            # These reads remain in the same transaction; no writes or claims.
            authority.revalidate_in_transaction(tx, now=clock())
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return None
            return _prepare_conversation_for_read(snap.to_dict(), authority.uid)

        return read(self.db.transaction(max_attempts=1))


def discover(authority, value, repository, *, clock=None, helpers=None):
    arguments(value)
    need(authority is not None and type(authority.uid) is str and bool(authority.uid)
         and '/' not in authority.uid, 'thin_discovery_authenticated_owner')
    clock = clock or (lambda: datetime.now(timezone.utc))
    helpers = helpers or production_helpers()
    after = parse_cursor(value.get('cursor'), authority)
    authority.revalidate()
    page = repository.candidate_page(authority, clock(), after)
    need(type(page) is list and len(page) <= SCAN_LIMIT, 'thin_discovery_scan_bound')
    item = None
    last = None
    inspected = 0
    for position in page:
        need(type(position) is dict and set(position) == {'conversation_id', 'expires_at'},
             'thin_discovery_page')
        cid = position['conversation_id']
        expiry = position['expires_at']
        need(canonical_uuid(cid) and isinstance(expiry, datetime) and expiry.tzinfo is not None
             and expiry.utcoffset().total_seconds() == 0, 'thin_discovery_page')
        key = (expiry, cid)
        previous = last or after
        need(previous is None or key > (previous['expires_at'], previous['conversation_id']),
             'thin_discovery_page_order')
        last = position
        inspected += 1
        document = repository.read_current(authority, cid, clock)
        if document is None:
            continue
        try:
            item = projection(authority.uid, cid, document, authority, clock(), helpers)
        except ValueError:
            # Malformed/stale source never turns into permission or raw error text.
            continue
        break
    # Check again after all repository awaits/RPCs; expiry is not refreshed.
    authority.revalidate()
    now = clock()
    if item is not None and not (
        datetime.fromisoformat(item['ready_at']) <= now < datetime.fromisoformat(item['expires_at'])
    ):
        item = None
    return {
        'contract': CONTRACT,
        'owner_ref_sha256': hashlib.sha256(authority.uid.encode()).hexdigest(),
        'consent_scope': deepcopy(authority.scope),
        'observed_at': now.isoformat(),
        'items': [item] if item else [],
        'scan_limit_reached': len(page) == SCAN_LIMIT,
        'next_cursor': export_cursor(last, authority) if last is not None and (
            inspected < len(page) or len(page) == SCAN_LIMIT) else None,
        'claim_created': False,
        'exact_fetch_required': True,
    }


def tool_definition():
    return {
        'name': TOOL_NAME,
        'description': 'Read one bounded page and return at most one eligible Thin recording for the authenticated owner/current dedicated consent. Follow next_cursor even for empty items; null ends a cycle. Hints only; exact fetch and CAS remain required.',
        'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'enum': [1]},
            'cursor': {'type': 'object', 'properties': {
                'expires_at': {'type': 'string', 'maxLength': 40},
                'conversation_id': {'type': 'string', 'format': 'uuid'},
                'scope_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
                'required': ['expires_at', 'conversation_id', 'scope_sha256'],
                'additionalProperties': False}},
                        'additionalProperties': False},
    }


def execute_for_authenticated_owner(db, authenticated_uid, value, *, enabled=False):
    """Future MCP adapter. No route/tool registration is included in this patch.

    enabled is server-only activation, never request JSON or an authority grant.
    Existing MCP authentication MUST establish authenticated_uid first.
    """
    need(enabled is True, 'thin_discovery_not_enabled')
    arguments(value)
    need(type(authenticated_uid) is str and hashlib.sha256(authenticated_uid.encode()).hexdigest() == TEST_OWNER_SHA256,
         'thin_discovery_test_account_only')
    need(type(authenticated_uid) is str and bool(authenticated_uid)
         and '/' not in authenticated_uid, 'thin_discovery_authenticated_owner')
    from ella.services.thin_cloudbot_request_authority import require_mcp_request
    root = db.collection('ella_thin_cloudbot_consent').document(authenticated_uid)
    current = root.get().to_dict() or {}
    state = current.get('current_state') or {}
    scope = {'consent_target': 'thin-cloudbot', 'receipt_id': state.get('receipt_id'),
             'account_epoch_hash': state.get('account_epoch_hash')}
    authority = require_mcp_request(authenticated_uid, scope, db=db)
    need(authority is not None, 'thin_discovery_dedicated_consent_required')
    return discover(authority, value, FirestorePendingRepository(db))
