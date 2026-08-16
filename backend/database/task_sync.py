import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.cloud.firestore_v1 import transactional

from ._client import db, document_id_from_seed

TASK_SYNC_RECEIPTS_COLLECTION = 'task_sync_receipts'
# Stay below the 15-second capture-finalization lease. A pre-egress claim may be
# reclaimed, but an outbound-started receipt is permanently fail-closed.
TASK_SYNC_LEASE_SECONDS = 5
TASK_SYNC_OUTBOUND_STARTED = 'outbound_started'
TASK_SYNC_RECEIPT_PROTOCOL_VERSION = 2


def task_sync_operation_key(uid: str, finalization_operation_key: str, action_item_id: str) -> str:
    """Return one stable, user-scoped identity for an action-item export."""
    return str(document_id_from_seed(f'omi:task-sync:{uid}:{finalization_operation_key}:action-item:{action_item_id}'))


def task_sync_receipt_id(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()


def provider_request_id(idempotency_key: str) -> str:
    """Return a provider-safe stable UUID for APIs with native idempotency."""
    return str(document_id_from_seed(f'omi:task-provider-request:{idempotency_key}'))


def task_sync_operation_marker(idempotency_key: str) -> str:
    """Return a stable, content-free marker for the durable egress boundary."""
    return str(document_id_from_seed(f'omi:task-sync-operation:{idempotency_key}'))


def _task_sync_receipt_ref(uid: str, idempotency_key: str):
    return (
        db.collection('users')
        .document(uid)
        .collection(TASK_SYNC_RECEIPTS_COLLECTION)
        .document(task_sync_receipt_id(idempotency_key))
    )


def _assert_receipt_identity(receipt: dict, idempotency_key: str, action_item_id: str, platform: str) -> None:
    expected = {
        'idempotency_key': idempotency_key,
        'action_item_id': action_item_id,
        'platform': platform,
    }
    actual = {key: receipt.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError('Task-sync receipt identity mismatch')


def _is_future(value: Optional[datetime], now: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return value > now


def _claim_task_sync_transaction(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
    lease_seconds: int,
) -> dict:
    snapshot = receipt_ref.get(transaction=transaction)
    if snapshot.exists:
        receipt = snapshot.to_dict()
        _assert_receipt_identity(receipt, idempotency_key, action_item_id, platform)
        if receipt.get('state') == 'completed':
            return {'outcome': 'completed', 'result': receipt.get('result')}
        if receipt.get('state') == TASK_SYNC_OUTBOUND_STARTED:
            return {
                'outcome': 'ambiguous',
                'operation_marker': receipt.get('operation_marker') or task_sync_operation_marker(idempotency_key),
            }
        if receipt.get('state') == 'claimed' and _is_future(receipt.get('lease_expires_at'), now):
            return {'outcome': 'busy'}
        if (
            receipt.get('state') != 'claimed'
            or receipt.get('receipt_protocol_version') != TASK_SYNC_RECEIPT_PROTOCOL_VERSION
        ):
            return {
                'outcome': 'ambiguous',
                'operation_marker': receipt.get('operation_marker') or task_sync_operation_marker(idempotency_key),
            }

    transaction.set(
        receipt_ref,
        {
            'idempotency_key': idempotency_key,
            'action_item_id': action_item_id,
            'platform': platform,
            'receipt_protocol_version': TASK_SYNC_RECEIPT_PROTOCOL_VERSION,
            'state': 'claimed',
            'claim_token': claim_token,
            'claimed_at': now,
            'lease_expires_at': now + timedelta(seconds=lease_seconds),
            'updated_at': now,
        },
        merge=True,
    )
    return {'outcome': 'claimed'}


@transactional
def _claim_task_sync(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
    lease_seconds: int,
) -> dict:
    return _claim_task_sync_transaction(
        transaction,
        receipt_ref,
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
        lease_seconds,
    )


def claim_task_sync(
    uid: str,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    *,
    now: Optional[datetime] = None,
    lease_seconds: int = TASK_SYNC_LEASE_SECONDS,
) -> dict:
    now = now or datetime.now(timezone.utc)
    return _claim_task_sync(
        db.transaction(),
        _task_sync_receipt_ref(uid, idempotency_key),
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
        lease_seconds,
    )


def observe_task_sync_claim(
    uid: str,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    *,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    snapshot = _task_sync_receipt_ref(uid, idempotency_key).get()
    if not snapshot.exists:
        return {'outcome': 'lost'}
    receipt = snapshot.to_dict()
    _assert_receipt_identity(receipt, idempotency_key, action_item_id, platform)
    if receipt.get('state') == 'completed':
        return {'outcome': 'completed', 'result': receipt.get('result')}
    if (
        receipt.get('state') == 'claimed'
        and receipt.get('claim_token') == claim_token
        and _is_future(receipt.get('lease_expires_at'), now)
    ):
        return {'outcome': 'claimed'}
    return {'outcome': 'lost'}


def _begin_task_sync_egress_transaction(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
) -> dict:
    """Fence the final pre-egress observation with a durable receipt transition."""
    snapshot = receipt_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {'outcome': 'lost'}
    receipt = snapshot.to_dict()
    _assert_receipt_identity(receipt, idempotency_key, action_item_id, platform)
    if receipt.get('state') == 'completed':
        return {'outcome': 'completed', 'result': receipt.get('result')}
    if receipt.get('state') == TASK_SYNC_OUTBOUND_STARTED:
        return {
            'outcome': 'ambiguous',
            'operation_marker': receipt.get('operation_marker') or task_sync_operation_marker(idempotency_key),
        }
    if (
        receipt.get('state') != 'claimed'
        or receipt.get('receipt_protocol_version') != TASK_SYNC_RECEIPT_PROTOCOL_VERSION
        or receipt.get('claim_token') != claim_token
        or not _is_future(receipt.get('lease_expires_at'), now)
    ):
        return {'outcome': 'lost'}

    operation_marker = task_sync_operation_marker(idempotency_key)
    transaction.set(
        receipt_ref,
        {
            'state': TASK_SYNC_OUTBOUND_STARTED,
            'operation_marker': operation_marker,
            'automatic_retry_safe': False,
            'reconciliation_status': 'required',
            'outbound_started_at': now,
            'updated_at': now,
        },
        merge=True,
    )
    return {'outcome': TASK_SYNC_OUTBOUND_STARTED, 'operation_marker': operation_marker}


@transactional
def _begin_task_sync_egress(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
) -> dict:
    return _begin_task_sync_egress_transaction(
        transaction,
        receipt_ref,
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
    )


def begin_task_sync_egress(
    uid: str,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    *,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    return _begin_task_sync_egress(
        db.transaction(),
        _task_sync_receipt_ref(uid, idempotency_key),
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
    )


def _complete_task_sync_transaction(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    result: dict,
    now: datetime,
) -> dict:
    snapshot = receipt_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {'outcome': 'lost'}
    receipt = snapshot.to_dict()
    _assert_receipt_identity(receipt, idempotency_key, action_item_id, platform)
    if receipt.get('state') == 'completed':
        return {'outcome': 'completed', 'result': receipt.get('result')}
    if receipt.get('state') not in {'claimed', TASK_SYNC_OUTBOUND_STARTED} or receipt.get('claim_token') != claim_token:
        return {'outcome': 'lost'}
    transaction.set(
        receipt_ref,
        {
            'state': 'completed',
            'result': result,
            'completed_at': now,
            'updated_at': now,
        },
        merge=True,
    )
    return {'outcome': 'completed', 'result': result}


@transactional
def _complete_task_sync(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    result: dict,
    now: datetime,
) -> dict:
    return _complete_task_sync_transaction(
        transaction,
        receipt_ref,
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        result,
        now,
    )


def complete_task_sync(
    uid: str,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    result: dict,
    *,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    return _complete_task_sync(
        db.transaction(),
        _task_sync_receipt_ref(uid, idempotency_key),
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        result,
        now,
    )
