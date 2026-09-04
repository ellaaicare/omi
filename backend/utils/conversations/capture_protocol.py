import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from google.cloud.firestore_v1 import transactional

from database._client import db

CAPTURE_PROTOCOL_VERSION = 2
CAPTURE_PROTOCOL_UPGRADE_CLOSE_CODE = 1008
CAPTURE_PROTOCOL_UPGRADE_REASON = 'This app version can no longer start captures. Please update the app.'
CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_CLOSE_CODE = 1013
CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_REASON = 'Capture protocol v2 rollout drain is not complete'
CAPTURE_AUTHORITY_COLLECTION = 'capture_authority'
CAPTURE_AUTHORITY_DOCUMENT = 'current'
CAPTURE_AUTHORITY_LEASE_SECONDS = 30
CAPTURE_FINALIZATION_LEASE_SECONDS = 30


def capture_protocol_v2_rollout_enabled() -> bool:
    return os.getenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', '').strip() == 'legacy_workers_drained'


def capture_protocol_accepted(protocol_version: int) -> bool:
    return protocol_version == CAPTURE_PROTOCOL_VERSION


async def require_capture_protocol_before_creation(websocket, protocol_version: int) -> bool:
    if not capture_protocol_accepted(protocol_version):
        await websocket.close(
            code=CAPTURE_PROTOCOL_UPGRADE_CLOSE_CODE,
            reason=CAPTURE_PROTOCOL_UPGRADE_REASON,
        )
        return False
    if not capture_protocol_v2_rollout_enabled():
        await websocket.close(
            code=CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_CLOSE_CODE,
            reason=CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_REASON,
        )
        return False
    return True


def valid_capture_drain_body(
    body: dict,
    conversation_id: str,
    generation: str,
    owner_token: str,
) -> bool:
    return bool(
        body.get('type') == 'capture_drain'
        and body.get('protocol_version') == CAPTURE_PROTOCOL_VERSION
        and body.get('conversation_id') == conversation_id
        and body.get('generation') == generation
        and body.get('owner_token') == owner_token
    )


async def flush_capture_before_drained(
    finish_stt_inputs: Callable[[], Optional[Awaitable[None]]],
    persistence_tasks: Set[asyncio.Task],
    buffers_drained: asyncio.Event,
    *,
    timeout: float = 10.0,
) -> bool:
    """Wait for provider shutdown, late callbacks, and durable capture buffers."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    buffers_drained.clear()
    result = finish_stt_inputs()
    if result is not None:
        await result
    while persistence_tasks:
        snapshot = tuple(persistence_tasks)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(
                asyncio.gather(*snapshot, return_exceptions=True),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return False
        for task in snapshot:
            if task.done():
                persistence_tasks.discard(task)
        await asyncio.sleep(0)
    remaining = deadline - loop.time()
    if remaining <= 0:
        return False
    try:
        await asyncio.wait_for(buffers_drained.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        return False
    return True


def _authority_ref(uid: str):
    return (
        db.collection('users')
        .document(uid)
        .collection(CAPTURE_AUTHORITY_COLLECTION)
        .document(CAPTURE_AUTHORITY_DOCUMENT)
    )


def _conversation_ref(uid: str, conversation_id: str):
    return db.collection('users').document(uid).collection('conversations').document(conversation_id)


def _status_value(value: Any) -> str:
    return str(getattr(value, 'value', value) or '')


def _authority_tuple_matches(data: Dict[str, Any], conversation_id: str, generation: str, owner_token: str) -> bool:
    return bool(
        data.get('protocol_version') == CAPTURE_PROTOCOL_VERSION
        and data.get('conversation_id') == conversation_id
        and data.get('generation') == generation
        and data.get('owner_token') == owner_token
    )


def _conversation_tuple_matches(data: Dict[str, Any], conversation_id: str, generation: str, owner_token: str) -> bool:
    return bool(
        str(data.get('id') or '') == conversation_id
        and data.get('capture_protocol_version') == CAPTURE_PROTOCOL_VERSION
        and data.get('capture_generation') == generation
        and data.get('capture_owner_token') == owner_token
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _lease_expired(data: Dict[str, Any], now: datetime, field: str = 'lease_expires_at') -> bool:
    expires_at = data.get(field)
    return not isinstance(expires_at, datetime) or _aware(expires_at) <= now


def _authority_live_for_reconnect(authority: Dict[str, Any], now: datetime) -> bool:
    state = str(authority.get('state') or '')
    if state in {'drained', 'terminal'}:
        return False
    if state == 'finalizing':
        return not _lease_expired(authority, now, field='finalization_lease_expires_at')
    return not _lease_expired(authority, now)


def _finalization_claim_is_live(
    authority: Dict[str, Any],
    conversation: Dict[str, Any],
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    now: datetime,
) -> bool:
    return bool(
        _authority_tuple_matches(authority, conversation_id, generation, owner_token)
        and _conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
        and authority.get('state') == 'finalizing'
        and authority.get('finalization_claim_token') == claim_token
        and not _lease_expired(authority, now, field='finalization_lease_expires_at')
        and conversation.get('capture_state') == 'finalizing'
        and conversation.get('capture_finalization_claim_token') == claim_token
        and not _lease_expired(conversation, now, field='capture_finalization_lease_expires_at')
    )


def capture_finalization_effect_operation_token(
    conversation_id: str,
    generation: str,
    owner_token: str,
    effect_id: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f'omi:capture-finalization:{conversation_id}:{generation}:{owner_token}:{effect_id}',
        )
    )


@transactional
def _claim_reconnect_authority_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    expected_owner_token: Optional[str],
    owner_token: str,
    now: datetime,
) -> bool:
    """Atomically fence an expired capture generation before reconnect publication."""
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not conversation_snapshot.exists:
        return False
    conversation = conversation_snapshot.to_dict() or {}
    if (
        _status_value(conversation.get('status')) != 'in_progress'
        or str(conversation.get('id') or '') != conversation_id
        or str(conversation.get('capture_owner_id') or '') != str(expected_owner_token or '')
    ):
        return False

    authority_snapshot = authority_ref.get(transaction=transaction)
    authority = authority_snapshot.to_dict() if authority_snapshot.exists else {}
    exact_claim = bool(
        authority.get('state') == 'active'
        and _authority_tuple_matches(authority, conversation_id, generation, owner_token)
        and _conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
        and str(conversation.get('capture_owner_id') or '') == owner_token
    )
    if exact_claim:
        return True
    if authority and _authority_live_for_reconnect(authority, now):
        return False

    lease_expires_at = now + timedelta(seconds=CAPTURE_AUTHORITY_LEASE_SECONDS)
    transaction.set(
        authority_ref,
        {
            'protocol_version': CAPTURE_PROTOCOL_VERSION,
            'conversation_id': conversation_id,
            'generation': generation,
            'owner_token': owner_token,
            'state': 'active',
            'lease_expires_at': lease_expires_at,
            'updated_at': now,
        },
    )
    transaction.update(
        conversation_ref,
        {
            'capture_owner_id': owner_token,
            'capture_protocol_version': CAPTURE_PROTOCOL_VERSION,
            'capture_generation': generation,
            'capture_owner_token': owner_token,
            'capture_state': 'active',
            'capture_lease_expires_at': lease_expires_at,
            'capture_drained_at': None,
            'capture_finalization_claim_token': None,
        },
    )
    return True


def claim_capture_authority_for_reconnect(
    uid: str,
    conversation_id: str,
    generation: str,
    expected_owner_token: Optional[str],
    owner_token: str,
) -> bool:
    return _claim_reconnect_authority_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        expected_owner_token,
        owner_token,
        datetime.now(timezone.utc),
    )


@transactional
def _install_authority_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    now: datetime,
    expected_conversation_id: Optional[str],
    predecessor_ref,
    adopt: bool,
) -> bool:
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not conversation_snapshot.exists:
        return False
    conversation = conversation_snapshot.to_dict() or {}
    if _status_value(conversation.get('status')) != 'in_progress':
        return False
    if str(conversation.get('capture_owner_id') or '') != owner_token:
        return False

    authority_snapshot = authority_ref.get(transaction=transaction)
    authority = authority_snapshot.to_dict() if authority_snapshot.exists else {}
    already_installed = bool(
        authority.get('state') == 'active'
        and conversation.get('capture_state') == 'active'
        and _authority_tuple_matches(authority, conversation_id, generation, owner_token)
        and _conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
    )
    if already_installed:
        return True

    predecessor = None
    if adopt:
        if str(conversation.get('id') or '') != conversation_id:
            return False
        if authority:
            same_authority = _authority_tuple_matches(authority, conversation_id, generation, owner_token)
            authority_is_live = authority.get('state') not in {'drained', 'terminal'} and not _lease_expired(
                authority,
                now,
            )
            if authority_is_live and not same_authority:
                return False
    elif expected_conversation_id:
        if predecessor_ref is None:
            return False
        predecessor_snapshot = predecessor_ref.get(transaction=transaction)
        if not predecessor_snapshot.exists:
            return False
        predecessor = predecessor_snapshot.to_dict() or {}
        predecessor_status = _status_value(predecessor.get('status'))
        predecessor_is_v2 = predecessor.get('capture_protocol_version') == CAPTURE_PROTOCOL_VERSION
        authority_generation = str(authority.get('generation') or '')
        authority_owner_token = str(authority.get('owner_token') or '')
        predecessor_matches_authority = bool(
            authority_generation
            and authority_owner_token
            and _authority_tuple_matches(
                authority,
                expected_conversation_id,
                authority_generation,
                authority_owner_token,
            )
            and _conversation_tuple_matches(
                predecessor,
                expected_conversation_id,
                authority_generation,
                authority_owner_token,
            )
        )
        successor_reuses_predecessor_tuple = _authority_tuple_matches(
            authority,
            expected_conversation_id,
            generation,
            owner_token,
        ) and _conversation_tuple_matches(
            predecessor,
            expected_conversation_id,
            generation,
            owner_token,
        )
        authority_lease_expires_at = authority.get('lease_expires_at')
        predecessor_lease_expires_at = predecessor.get('capture_lease_expires_at')
        expired_predecessor_can_handoff = bool(
            predecessor_matches_authority
            and isinstance(authority_lease_expires_at, datetime)
            and isinstance(predecessor_lease_expires_at, datetime)
            and _aware(authority_lease_expires_at) <= now
            and _aware(predecessor_lease_expires_at) <= now
        )
        active_predecessor_can_handoff = bool(
            authority.get('state') == 'active'
            and (successor_reuses_predecessor_tuple or expired_predecessor_can_handoff)
        )
        drained_predecessor_can_handoff = bool(
            authority.get('state') == 'drained'
            and predecessor.get('capture_state') == 'drained'
            and expired_predecessor_can_handoff
        )
        if predecessor_status != 'in_progress':
            return False
        if predecessor_is_v2 and not (active_predecessor_can_handoff or drained_predecessor_can_handoff):
            return False
        if not predecessor_is_v2 and authority:
            legacy_authority_is_live = authority.get('state') not in {'drained', 'terminal'} and not _lease_expired(
                authority,
                now,
            )
            if legacy_authority_is_live:
                return False
    elif authority and authority.get('state') not in {'drained', 'terminal'} and not _lease_expired(authority, now):
        return False

    lease_expires_at = now + timedelta(seconds=CAPTURE_AUTHORITY_LEASE_SECONDS)
    authority_data = {
        'protocol_version': CAPTURE_PROTOCOL_VERSION,
        'conversation_id': conversation_id,
        'generation': generation,
        'owner_token': owner_token,
        'state': 'active',
        'lease_expires_at': lease_expires_at,
        'updated_at': now,
    }
    if predecessor_ref is not None and predecessor is not None:
        transaction.update(
            predecessor_ref,
            {
                'capture_state': 'drained',
                'capture_drained_at': now,
                'capture_lease_expires_at': now,
            },
        )
    transaction.set(authority_ref, authority_data)
    transaction.update(
        conversation_ref,
        {
            'capture_protocol_version': CAPTURE_PROTOCOL_VERSION,
            'capture_generation': generation,
            'capture_owner_token': owner_token,
            'capture_state': 'active',
            'capture_lease_expires_at': lease_expires_at,
            'capture_drained_at': None,
            'capture_finalization_claim_token': None,
        },
    )
    return True


def install_capture_authority(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    *,
    expected_conversation_id: Optional[str] = None,
    adopt: bool = False,
) -> bool:
    predecessor_ref = _conversation_ref(uid, expected_conversation_id) if expected_conversation_id is not None else None
    return _install_authority_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        datetime.now(timezone.utc),
        expected_conversation_id,
        predecessor_ref,
        adopt,
    )


@transactional
def _renew_authority_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if (
        authority.get('state') != 'active'
        or not _authority_tuple_matches(authority, conversation_id, generation, owner_token)
        or not _conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
        or _status_value(conversation.get('status')) != 'in_progress'
        or str(conversation.get('capture_owner_id') or '') != owner_token
    ):
        return False
    lease_expires_at = now + timedelta(seconds=CAPTURE_AUTHORITY_LEASE_SECONDS)
    transaction.update(authority_ref, {'lease_expires_at': lease_expires_at, 'updated_at': now})
    transaction.update(conversation_ref, {'capture_lease_expires_at': lease_expires_at})
    return True


def renew_capture_authority(uid: str, conversation_id: str, generation: str, owner_token: str) -> bool:
    return _renew_authority_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        datetime.now(timezone.utc),
    )


@transactional
def _mark_drained_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _authority_tuple_matches(authority, conversation_id, generation, owner_token):
        return False
    if authority.get('state') == 'drained' and conversation.get('capture_state') == 'drained':
        return _conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
    if authority.get('state') not in {'active', 'drained'}:
        return False
    if str(conversation.get('capture_owner_id') or '') != owner_token:
        return False
    if not _conversation_tuple_matches(conversation, conversation_id, generation, owner_token):
        return False

    transaction.update(
        authority_ref,
        {'state': 'drained', 'lease_expires_at': now, 'updated_at': now, 'drained_at': now},
    )
    transaction.update(
        conversation_ref,
        {
            'capture_state': 'drained',
            'capture_drained_at': now,
            'capture_lease_expires_at': now,
            'capture_owner_id': None,
        },
    )
    return True


def mark_capture_drained(uid: str, conversation_id: str, generation: str, owner_token: str) -> bool:
    return _mark_drained_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        datetime.now(timezone.utc),
    )


@transactional
def _claim_finalization_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    now: datetime,
) -> str:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return 'not_found'
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _authority_tuple_matches(authority, conversation_id, generation, owner_token):
        return 'mismatch'
    if not _conversation_tuple_matches(conversation, conversation_id, generation, owner_token):
        return 'mismatch'

    state = str(authority.get('state') or '')
    if state == 'terminal' and str(conversation.get('capture_state') or '') == 'terminal':
        return 'terminal'
    if state == 'finalizing':
        expires_at = authority.get('finalization_lease_expires_at')
        if isinstance(expires_at, datetime) and _aware(expires_at) > now:
            return 'busy'
    elif state != 'drained':
        return 'not_drained'

    lease_expires_at = now + timedelta(seconds=CAPTURE_FINALIZATION_LEASE_SECONDS)
    transaction.update(
        authority_ref,
        {
            'state': 'finalizing',
            'finalization_claim_token': claim_token,
            'finalization_lease_expires_at': lease_expires_at,
            'updated_at': now,
        },
    )
    transaction.update(
        conversation_ref,
        {
            'capture_state': 'finalizing',
            'capture_finalization_claim_token': claim_token,
            'capture_finalization_lease_expires_at': lease_expires_at,
            'capture_finalization_attempt_count': int(conversation.get('capture_finalization_attempt_count') or 0) + 1,
            'capture_finalization_started_at': conversation.get('capture_finalization_started_at') or now,
        },
    )
    return 'claimed'


def claim_capture_finalization(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: Optional[str] = None,
) -> tuple[str, str]:
    exact_claim_token = claim_token or str(uuid.uuid4())
    outcome = _claim_finalization_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        exact_claim_token,
        datetime.now(timezone.utc),
    )
    return outcome, exact_claim_token


@transactional
def _renew_finalization_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _finalization_claim_is_live(
        authority,
        conversation,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        now,
    ):
        return False
    lease_expires_at = now + timedelta(seconds=CAPTURE_FINALIZATION_LEASE_SECONDS)
    transaction.update(
        authority_ref,
        {'finalization_lease_expires_at': lease_expires_at, 'updated_at': now},
    )
    transaction.update(
        conversation_ref,
        {'capture_finalization_lease_expires_at': lease_expires_at},
    )
    return True


def renew_capture_finalization(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
) -> bool:
    return _renew_finalization_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        claim_token,
        datetime.now(timezone.utc),
    )


@transactional
def _claim_finalization_effect_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    effect_id: str,
    now: datetime,
) -> Dict[str, Any]:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return {'outcome': 'not_found'}
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _finalization_claim_is_live(
        authority,
        conversation,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        now,
    ):
        return {'outcome': 'lost'}
    effects = dict(conversation.get('capture_finalization_effects') or {})
    receipt = dict(effects.get(effect_id) or {})
    if receipt.get('state') == 'completed':
        return {
            'outcome': 'completed',
            'operation_token': receipt.get('operation_token'),
            'result': receipt.get('result'),
        }
    operation_token = receipt.get('operation_token') or capture_finalization_effect_operation_token(
        conversation_id,
        generation,
        owner_token,
        effect_id,
    )
    effects[effect_id] = {
        **receipt,
        'state': 'claimed',
        'operation_token': operation_token,
        'claim_token': claim_token,
        'attempt_count': int(receipt.get('attempt_count') or 0) + 1,
        'claimed_at': now,
    }
    lease_expires_at = now + timedelta(seconds=CAPTURE_FINALIZATION_LEASE_SECONDS)
    transaction.update(
        authority_ref,
        {'finalization_lease_expires_at': lease_expires_at, 'updated_at': now},
    )
    transaction.update(
        conversation_ref,
        {
            'capture_finalization_effects': effects,
            'capture_finalization_lease_expires_at': lease_expires_at,
        },
    )
    return {'outcome': 'claimed', 'operation_token': operation_token}


def claim_capture_finalization_effect(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    effect_id: str,
) -> Dict[str, Any]:
    return _claim_finalization_effect_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        claim_token,
        effect_id,
        datetime.now(timezone.utc),
    )


@transactional
def _complete_finalization_effect_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    effect_id: str,
    operation_token: str,
    result: Any,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _finalization_claim_is_live(
        authority,
        conversation,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        now,
    ):
        return False
    effects = dict(conversation.get('capture_finalization_effects') or {})
    receipt = dict(effects.get(effect_id) or {})
    if (
        receipt.get('state') != 'claimed'
        or receipt.get('claim_token') != claim_token
        or receipt.get('operation_token') != operation_token
    ):
        return False
    effects[effect_id] = {
        **receipt,
        'state': 'completed',
        'result': result,
        'completed_at': now,
    }
    transaction.update(conversation_ref, {'capture_finalization_effects': effects})
    return True


def complete_capture_finalization_effect(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    effect_id: str,
    operation_token: str,
    result: Any,
) -> bool:
    return _complete_finalization_effect_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        claim_token,
        effect_id,
        operation_token,
        result,
        datetime.now(timezone.utc),
    )


@transactional
def _complete_finalization_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _authority_tuple_matches(authority, conversation_id, generation, owner_token):
        return False
    if not _conversation_tuple_matches(conversation, conversation_id, generation, owner_token):
        return False
    if authority.get('state') == 'terminal' and conversation.get('capture_state') == 'terminal':
        return True
    if not _finalization_claim_is_live(
        authority,
        conversation,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        now,
    ):
        return False
    if _status_value(conversation.get('status')) not in {'completed', 'failed'}:
        return False
    effects = conversation.get('capture_finalization_effects') or {}
    integration_receipt = effects.get('integrations:external') or {}
    if integration_receipt.get('state') != 'completed':
        return False
    if any(receipt.get('state') != 'completed' for receipt in effects.values()):
        return False
    compact_effects = dict(effects)
    for receipt in compact_effects.values():
        if receipt.get('state') == 'completed':
            receipt.pop('result', None)

    transaction.update(
        authority_ref,
        {
            'state': 'terminal',
            'finalization_claim_token': None,
            'finalization_lease_expires_at': None,
            'updated_at': now,
            'terminal_at': now,
        },
    )
    transaction.update(
        conversation_ref,
        {
            'capture_state': 'terminal',
            'capture_finalization_claim_token': None,
            'capture_finalization_lease_expires_at': now,
            'capture_finalization_effects': compact_effects,
        },
    )
    return True


def complete_capture_finalization(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
) -> bool:
    return _complete_finalization_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        claim_token,
        datetime.now(timezone.utc),
    )


@transactional
def _release_finalization_transaction(
    transaction,
    authority_ref,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
    now: datetime,
) -> bool:
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not conversation_snapshot.exists:
        return False
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _authority_tuple_matches(authority, conversation_id, generation, owner_token):
        return False
    if authority.get('state') != 'finalizing' or authority.get('finalization_claim_token') != claim_token:
        return False
    if conversation.get('capture_finalization_claim_token') != claim_token:
        return False
    transaction.update(
        authority_ref,
        {
            'state': 'drained',
            'finalization_claim_token': None,
            'finalization_lease_expires_at': None,
            'updated_at': now,
        },
    )
    transaction.update(
        conversation_ref,
        {
            'capture_state': 'drained',
            'capture_finalization_claim_token': None,
        },
    )
    return True


def release_capture_finalization(
    uid: str,
    conversation_id: str,
    generation: str,
    owner_token: str,
    claim_token: str,
) -> bool:
    return _release_finalization_transaction(
        db.transaction(),
        _authority_ref(uid),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        claim_token,
        datetime.now(timezone.utc),
    )


@transactional
def _complete_rotated_capture_transaction(
    transaction,
    conversation_ref,
    conversation_id: str,
    generation: str,
    owner_token: str,
    now: datetime,
) -> bool:
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    if not conversation_snapshot.exists:
        return False
    conversation = conversation_snapshot.to_dict() or {}
    if not _conversation_tuple_matches(conversation, conversation_id, generation, owner_token):
        return False
    if conversation.get('capture_state') == 'terminal':
        return True
    if conversation.get('capture_state') != 'drained':
        return False
    if _status_value(conversation.get('status')) not in {'completed', 'failed'}:
        return False
    transaction.update(conversation_ref, {'capture_state': 'terminal', 'capture_terminal_at': now})
    return True


def complete_rotated_capture(uid: str, conversation_id: str, generation: str, owner_token: str) -> bool:
    return _complete_rotated_capture_transaction(
        db.transaction(),
        _conversation_ref(uid, conversation_id),
        conversation_id,
        generation,
        owner_token,
        datetime.now(timezone.utc),
    )
