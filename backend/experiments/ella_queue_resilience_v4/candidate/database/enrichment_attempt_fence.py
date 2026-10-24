"""Pure transition core for an opt-in, transactionally fenced worker.

NOT wired into serving code. The caller must read and replace this document in
one serializable transaction, together with the job, admission and resource
reservation. A successful START transaction must precede every external effect.
No clock or lease expiry proves completion of an external effect.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

CONTRACT = 'ella-enrichment-attempt-fence-v1'
IDENTITY = ('uid', 'job_id', 'conversation_id', 'client_interaction_id', 'transcript_sha256', 'route_sha256')
SAFE_PREFLIGHT_CODES = frozenset({'account_runtime_not_ready', 'account_model_unavailable', 'queue_maintenance'})
EFFECT_KINDS = frozenset({'context_sync', 'generator', 'verifier', 'learning_write', 'enrichment_graph'})


class FenceRejected(ValueError):
    pass


@dataclass(frozen=True)
class Attempt:
    uid: str
    job_id: str
    conversation_id: str
    client_interaction_id: str
    transcript_sha256: str
    route_sha256: str
    attempt: int
    lease_token: str


def _sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _key(attempt):
    if (
        type(attempt) is not Attempt
        or type(attempt.attempt) is not int
        or attempt.attempt < 1
        or any(type(getattr(attempt, field)) is not str or not getattr(attempt, field) for field in IDENTITY)
        or not _sha(attempt.transcript_sha256)
        or not _sha(attempt.route_sha256)
        or type(attempt.lease_token) is not str
        or not attempt.lease_token
    ):
        raise FenceRejected('attempt_identity_invalid')
    return {
        **{field: getattr(attempt, field) for field in IDENTITY},
        'attempt': attempt.attempt,
        'lease_sha256': sha256(attempt.lease_token.encode()).hexdigest(),
    }


def _require(document, attempt):
    expected = _key(attempt)
    if type(document) is not dict or document.get('contract') != CONTRACT:
        raise FenceRejected('attempt_identity_mismatch')
    stored = document.get('key')
    if (
        type(stored) is not dict
        or set(stored) != set(expected)
        or type(stored.get('attempt')) is not int
        or any(type(stored.get(key)) is not str for key in expected if key != 'attempt')
        or stored != expected
    ):
        raise FenceRejected('attempt_identity_mismatch')
    if (
        type(document.get('phase')) is not str
        or document['phase'] not in {'reserved', 'started', 'closed'}
        or type(document.get('effects')) is not dict
    ):
        raise FenceRejected('attempt_state_invalid')
    effects = document['effects']
    if len(effects) > 16:
        raise FenceRejected('effect_limit')
    signatures = set()
    for operation, effect in effects.items():
        if (
            type(operation) is not str
            or not operation
            or len(operation) > 200
            or type(effect) is not dict
            or type(effect.get('kind')) is not str
            or effect['kind'] not in EFFECT_KINDS
            or not _sha(effect.get('request_sha256'))
        ):
            raise FenceRejected('persisted_effect_invalid')
        signature = (effect['kind'], effect['request_sha256'])
        if signature in signatures:
            raise FenceRejected('duplicate_effect_identity')
        signatures.add(signature)
        if effect.get('state') == 'started':
            if set(effect) != {'kind', 'request_sha256', 'state'}:
                raise FenceRejected('persisted_effect_invalid')
        elif effect.get('state') == 'completed':
            if set(effect) != {'kind', 'request_sha256', 'state', 'receipt_sha256'} or not _sha(
                effect['receipt_sha256']
            ):
                raise FenceRejected('persisted_effect_invalid')
        else:
            raise FenceRejected('persisted_effect_invalid')
    phase, outcome = document['phase'], document.get('outcome')
    if phase != 'closed' and outcome is not None:
        raise FenceRejected('attempt_outcome_invalid')
    if phase == 'reserved' and effects:
        raise FenceRejected('reserved_effect_invalid')
    if phase == 'closed':
        if type(outcome) is not str or outcome not in {
            'verified_no_dispatch',
            'blocked_no_dispatch',
            'completed',
            'unknown_quarantined',
        }:
            raise FenceRejected('attempt_outcome_invalid')
        if outcome in {'verified_no_dispatch', 'blocked_no_dispatch'} and effects:
            raise FenceRejected('no_dispatch_unproven')
        if outcome == 'completed' and (not effects or any(value['state'] != 'completed' for value in effects.values())):
            raise FenceRejected('completion_unproven')


def reserve(attempt, *, claims_open, previously_quarantined):
    """Only after unchanged consent/owner/route/source/admission checks in caller."""
    if claims_open is not True or previously_quarantined is not False:
        raise FenceRejected('claim_closed_or_quarantined')
    return {'contract': CONTRACT, 'key': _key(attempt), 'phase': 'reserved', 'effects': {}, 'outcome': None}


def begin(document, attempt, *, claims_open):
    _require(document, attempt)
    if claims_open is not True or document['phase'] != 'reserved':
        raise FenceRejected('begin_closed')
    result = deepcopy(document)
    result['phase'] = 'started'
    return result


def start_effect(document, attempt, *, operation_id, kind, request_sha256):
    _require(document, attempt)
    if (
        document['phase'] != 'started'
        or type(operation_id) is not str
        or not operation_id
        or len(operation_id) > 200
        or type(kind) is not str
        or kind not in EFFECT_KINDS
        or not _sha(request_sha256)
    ):
        raise FenceRejected('effect_start_invalid')
    # Even identical START is not permission to send a second request. Reconcile
    # existing receipt separately; never repeat an uncertain network operation.
    if operation_id in document['effects']:
        raise FenceRejected('effect_already_started')
    if any(
        value['kind'] == kind and value['request_sha256'] == request_sha256 for value in document['effects'].values()
    ):
        raise FenceRejected('effect_already_started')
    if len(document['effects']) >= 16:
        raise FenceRejected('effect_limit')
    result = deepcopy(document)
    result['effects'][operation_id] = {'kind': kind, 'request_sha256': request_sha256, 'state': 'started'}
    return result


def complete_effect(document, attempt, *, operation_id, request_sha256, receipt_sha256):
    """Adapter must first validate the actual owner/request-bound remote ACK.

    A receipt hash alone is NOT proof. This pure function is not an API or an
    authorization boundary. Late completion may release the resource fence but
    cannot reopen the attempt or authorize a late canonical writeback.
    """
    _require(document, attempt)
    effect = document['effects'].get(operation_id)
    if (
        not effect
        or effect.get('request_sha256') != request_sha256
        or not _sha(receipt_sha256)
        or effect.get('state') != 'started'
    ):
        raise FenceRejected('effect_completion_invalid')
    result = deepcopy(document)
    result['effects'][operation_id].update(state='completed', receipt_sha256=receipt_sha256)
    return result


def finish(document, attempt, *, outcome, preflight_code=None):
    _require(document, attempt)
    if document['phase'] == 'closed':
        raise FenceRejected('attempt_already_closed')
    if outcome == 'verified_no_dispatch':
        if (
            document['phase'] != 'reserved'
            or document['effects']
            or type(preflight_code) is not str
            or preflight_code not in SAFE_PREFLIGHT_CODES
        ):
            raise FenceRejected('no_dispatch_unproven')
    elif outcome == 'blocked_no_dispatch':
        if document['phase'] != 'reserved' or document['effects']:
            raise FenceRejected('no_dispatch_unproven')
    elif outcome == 'completed':
        if (
            document['phase'] != 'started'
            or not document['effects']
            or any(value['state'] != 'completed' for value in document['effects'].values())
        ):
            raise FenceRejected('completion_unproven')
    elif outcome != 'unknown_quarantined':
        raise FenceRejected('outcome_invalid')
    result = deepcopy(document)
    result.update(phase='closed', outcome=outcome)
    return result


def retry_key(document, attempt, *, next_lease_token, max_attempts):
    _require(document, attempt)
    if (
        document['phase'] != 'closed'
        or document['outcome'] != 'verified_no_dispatch'
        or document['effects']
        or type(max_attempts) is not int
        or not 1 <= max_attempts <= 5
        or attempt.attempt >= max_attempts
        or next_lease_token == attempt.lease_token
    ):
        raise FenceRejected('retry_not_safe')
    successor = Attempt(
        **{field: getattr(attempt, field) for field in IDENTITY},
        attempt=attempt.attempt + 1,
        lease_token=next_lease_token
    )
    _key(successor)
    return successor


def unresolved_effects(document, attempt):
    _require(document, attempt)
    return tuple(sorted(key for key, value in document['effects'].items() if value['state'] != 'completed'))


def callback_allowed(document, attempt):
    _require(document, attempt)
    return document['phase'] == 'started'
