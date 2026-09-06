import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.cloud.firestore_v1 import transactional

from ._client import db, document_id_from_seed

TASK_SYNC_RECEIPTS_COLLECTION = 'task_sync_receipts'
# Stay below the 15-second capture-finalization lease. When a finalizer can be
# reclaimed, any sink claim left by its stopped predecessor is reclaimable too.
TASK_SYNC_LEASE_SECONDS = 5


def task_sync_operation_key(uid: str, finalization_operation_key: str, action_item_id: str) -> str:
    """Return one stable, user-scoped identity for an action-item export."""
    return str(document_id_from_seed(f'omi:task-sync:{uid}:{finalization_operation_key}:action-item:{action_item_id}'))


def task_sync_receipt_id(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()


def provider_request_id(idempotency_key: str) -> str:
    """Return a provider-safe stable UUID for APIs with native idempotency."""
    return str(document_id_from_seed(f'omi:task-provider-request:{idempotency_key}'))


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
        if receipt.get('state') == 'claimed' and _is_future(receipt.get('lease_expires_at'), now):
            return {'outcome': 'busy'}

    transaction.set(
        receipt_ref,
        {
            'idempotency_key': idempotency_key,
            'action_item_id': action_item_id,
            'platform': platform,
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
    if receipt.get('state') != 'claimed' or receipt.get('claim_token') != claim_token:
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


def _release_task_sync_transaction(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
) -> dict:
    snapshot = receipt_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {'outcome': 'lost'}
    receipt = snapshot.to_dict()
    _assert_receipt_identity(receipt, idempotency_key, action_item_id, platform)
    if receipt.get('state') == 'completed':
        return {'outcome': 'completed', 'result': receipt.get('result')}
    if receipt.get('state') != 'claimed' or receipt.get('claim_token') != claim_token:
        return {'outcome': 'lost'}
    transaction.set(
        receipt_ref,
        {
            'state': 'retryable',
            'claim_token': None,
            'lease_expires_at': now,
            'updated_at': now,
        },
        merge=True,
    )
    return {'outcome': 'released'}


@transactional
def _release_task_sync(
    transaction,
    receipt_ref,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    now: datetime,
) -> dict:
    return _release_task_sync_transaction(
        transaction,
        receipt_ref,
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
    )


def release_task_sync(
    uid: str,
    idempotency_key: str,
    action_item_id: str,
    platform: str,
    claim_token: str,
    *,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    return _release_task_sync(
        db.transaction(),
        _task_sync_receipt_ref(uid, idempotency_key),
        idempotency_key,
        action_item_id,
        platform,
        claim_token,
        now,
    )
