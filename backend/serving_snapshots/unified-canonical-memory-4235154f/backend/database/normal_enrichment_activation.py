"""Operational admission and bounded ownership for NEW normal completions.

No startup activation, old-source backfill, unknown semantic retry, or quarantine
clear API. Tenant quarantine requires reviewed operation/readback and cleanup.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from google.cloud.firestore_v1 import transactional
from database import normal_source_disposition as disposition

COLLECTION = 'ella_normal_enrichment_activation'
DOCUMENT = 'continuation-v2'
TENANTS = 'ella_normal_enrichment_tenant_dispatch'
JOBS = 'ella_hermes_cloud_enrichment_outbox'
DEFAULT_QUOTAS = {
    'max_concurrency': 2,
    'max_claims_per_hour': 20,
    'max_tenant_claims_per_hour': 5,
    'max_attempts': 3,
    'retry_delay_seconds': 60,
    'max_quarantined_tenants': 8,
}
LIMITS = {
    'max_concurrency': (1, 4),
    'max_claims_per_hour': (1, 100),
    'max_tenant_claims_per_hour': (1, 20),
    'max_attempts': (1, 5),
    'retry_delay_seconds': (10, 600),
    'max_quarantined_tenants': (2, 16),
}
SAFE_RETRY_CODES = frozenset({'account_runtime_not_ready', 'account_model_unavailable'})
IDENTITY = ('job_id', 'uid', 'conversation_id', 'client_interaction_id', 'transcript_sha256')


def reference(db):
    return db.collection(COLLECTION).document(DOCUMENT)


def tenant_ref(db, uid):
    return db.collection(TENANTS).document(hashlib.sha256(uid.encode()).hexdigest())


def quotas_valid(quotas):
    return (
        isinstance(quotas, dict)
        and set(quotas) == set(LIMITS)
        and all(type(quotas[k]) is int and lo <= quotas[k] <= hi for k, (lo, hi) in LIMITS.items())
    )


def owner_scope_valid(policy):
    """Optional operational scope restricts new admission/dispatch, never consent."""
    if 'owner_scope_sha256' not in policy:
        return True  # Preserve existing policy semantics; no implicit new scope.
    owners = policy['owner_scope_sha256']
    return (type(owners) is list and 1 <= len(owners) <= 16
            and all(type(owner) is str and len(owner) == 64
                    and all(c in '0123456789abcdef' for c in owner) for owner in owners)
            and len(set(owners)) == len(owners))


def owner_allowed(policy, uid):
    if not owner_scope_valid(policy):
        return False
    return ('owner_scope_sha256' not in policy
            or (type(uid) is str and hashlib.sha256(uid.encode()).hexdigest()
                in policy['owner_scope_sha256']))


def native_scope_valid(policy):
    if 'native_source_once' not in policy:
        return True
    from database.normal_native_source_once import scope_shape
    return scope_shape(policy)


def ordinary_scope_valid(policy):
    if 'ordinary_source_restoration_v2' not in policy:return True
    from database.normal_ordinary_source_disposition import policy_shape
    return policy_shape(policy)


def valid_policy(policy, now):
    if type(policy) is dict and policy.get('mode') == 'per_account_lifecycle_v1':
        from database.normal_account_lifecycle import valid_policy as lifecycle_valid
        return lifecycle_valid(policy, now)
    try:
        start, end = policy['source_completion_boundary'], policy['expires_at']
        return (
            type(policy['version']) is int
            and policy['version'] == 2
            and str(UUID(policy['epoch'])) == policy['epoch']
            and isinstance(start, datetime)
            and start.tzinfo is not None
            and isinstance(end, datetime)
            and end.tzinfo is not None
            and start <= now < end
            and end - start <= timedelta(days=7)
            and policy['state'] == 'active'
            and owner_scope_valid(policy)
            and native_scope_valid(policy)
            and ordinary_scope_valid(policy)
            and quotas_valid(policy['quotas'])
            and isinstance(policy['active'], list)
            and len(policy['active']) <= 4
            and isinstance(policy['quarantined_tenants'], list)
            and len(policy['quarantined_tenants']) <= 16
            and type(policy['hour']) is int
            and type(policy['hour_claims']) is int
            and 0 <= policy['hour_claims'] <= 100
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


@transactional
def _prepare(transaction, ref, *, now, window_seconds, quotas):
    if ref.get(transaction=transaction).exists:
        raise ValueError('normal_activation_existing_disposition_required')
    if type(window_seconds) is not int or not 0 < window_seconds <= 604800 or not quotas_valid(quotas):
        raise ValueError('normal_activation_configuration_invalid')
    policy = {
        'version': 2,
        'epoch': str(uuid4()),
        'source_completion_boundary': now,
        'expires_at': now + timedelta(seconds=window_seconds),
        'state': 'active',
        'quotas': quotas,
        'hour': int(now.timestamp()) // 3600,
        'hour_claims': 0,
        'active': [],
        'quarantined_tenants': [],
    }
    transaction.set(ref, policy)
    return policy


def prepare_activation(db, *, window_seconds, quotas=None):
    return _prepare(
        db.transaction(),
        reference(db),
        now=datetime.now(timezone.utc),
        window_seconds=window_seconds,
        quotas=dict(DEFAULT_QUOTAS if quotas is None else quotas),
    )


def read_policy(db):
    snapshot = reference(db).get(timeout=5, retry=None)
    return snapshot.to_dict() if snapshot.exists else {}


def admission_in_transaction(transaction, db, payload, *, source_completed_at, now, trusted_first_terminal=None):
    if (
        type(payload.get('account_routing_contract')) is not int
        or payload['account_routing_contract'] != 1
        or not isinstance(source_completed_at, datetime)
        or source_completed_at.tzinfo is None
    ):
        return {}, None
    snapshot = reference(db).get(transaction=transaction)
    policy = snapshot.to_dict() if snapshot.exists else {}
    if (not valid_policy(policy, now) or not owner_allowed(policy, payload.get('uid'))
            or not policy['source_completion_boundary'] <= source_completed_at <= now):
        return {}, None
    qualification=policy.get('one_source_qualification')
    if qualification is not None:
        from database.normal_future_source_disposition import current_policy, OWNER_HASH, string_hash
        if not current_policy(policy,now) or string_hash(payload.get('uid'))!=OWNER_HASH or now>=qualification['admission_cutoff']:
            return {},None
    ts = tenant_ref(db, payload['uid']).get(transaction=transaction)
    tenant = ts.to_dict() if ts.exists else {}
    if policy.get('mode') == 'per_account_lifecycle_v1':
        from database import normal_account_lifecycle as lifecycle
        created=lifecycle.admission(transaction,db,payload,policy,tenant,now,trusted_first_terminal)
        if created is None:
            return {'admission_epoch':policy['epoch'],'source_completed_at':source_completed_at,
                'status':'blocked','dispatch_state':'account_processing_held',
                'operator_action':'account_processing_authority_or_new_source_required',
                'admission_rejection_stage': ('normal_first_terminal_provenance_required'
                    if trusted_first_terminal is None else 'normal_account_or_source_authority_rejected')},None
        tag,marker,record=created
        transaction.create(marker,record)
        return {'admission_epoch':policy['epoch'],'source_completed_at':source_completed_at,
            'dispatch_state':'never_started','retry_safety':None,lifecycle.TAG:tag},None
    if 'ordinary_source_restoration_v2' in policy:
        from database import normal_ordinary_source_disposition as ordinary
        created=ordinary.admission(transaction,db,payload,policy,tenant,now,trusted_first_terminal)
        if created is None:
            return {'admission_epoch':policy['epoch'],'source_completed_at':source_completed_at,
                'status':'blocked','dispatch_state':'tenant_quarantined',
                'operator_action':'ordinary_source_or_preserved_scope_not_eligible'},None
        tag,marker,record=created
        transaction.create(marker,record)
        return {'admission_epoch':policy['epoch'],'source_completed_at':source_completed_at,
            'dispatch_state':'never_started','retry_safety':None,ordinary.TAG:tag},None
    if 'native_source_once' in policy:
        from database import normal_native_source_once as native
        created = native.admission(transaction, db, payload, policy, tenant, now, trusted_first_terminal)
        if created is None:
            return {'admission_epoch': policy['epoch'], 'source_completed_at': source_completed_at,
                    'status': 'blocked', 'dispatch_state': 'tenant_quarantined',
                    'operator_action': 'native_once_source_or_hold_not_eligible'}, None
        tag, marker, record = created
        transaction.create(marker, record)
        return {'admission_epoch': policy['epoch'], 'source_completed_at': source_completed_at,
                'dispatch_state': 'never_started', 'retry_safety': None, native.TAG: tag}, None
    approved = None if qualification is not None else (_qualified_disposition(transaction, db, {**payload, 'source_completed_at': source_completed_at}, policy, tenant, now, admission=True) if tenant.get('quarantined_job_id') else None)
    future = None
    if (qualification is not None or tenant.get('quarantined_job_id')) and approved is None and trusted_first_terminal is not None:
        from database.normal_future_source_disposition import admission
        future = admission(transaction, db, payload, policy, tenant, now, trusted_first_terminal)
    if qualification is not None and future is None:
        return {},None
    if tenant.get('quarantined_job_id') and approved is None and future is None:
        return {
            'admission_epoch': policy['epoch'],
            'source_completed_at': source_completed_at,
            'status': 'blocked',
            'dispatch_state': 'tenant_quarantined',
            'operator_action': 'reconcile_tenant_quarantined_operation',
        }, None
    result = {
        'admission_epoch': policy['epoch'],
        'source_completed_at': source_completed_at,
        'dispatch_state': 'never_started',
        'retry_safety': None,
    }
    if future is not None:
        from database.normal_future_source_disposition import TAG
        tag, ref, record = future
        # All transaction reads completed; source/outbox writes follow this.
        transaction.create(ref, record)
        result[TAG] = tag
    return result, None


@transactional
def claim(transaction, db, ref, *, now, lease_seconds):
    snap = ref.get(transaction=transaction)
    current = snap.to_dict() if snap.exists else {}
    ps = reference(db).get(transaction=transaction)
    policy = ps.to_dict() if ps.exists else {}
    if (not valid_policy(policy, now) or not owner_allowed(policy, current.get('uid'))
            or lease_seconds < 900):
        return None
    if any(not isinstance(current.get(k), str) or not current[k] for k in IDENTITY):
        return None
    if policy.get('one_source_qualification') is not None:
        from database.normal_future_source_disposition import qualification_gate
        if not qualification_gate(policy,current,now):return None
    tenant = tenant_ref(db, current['uid'])
    ts = tenant.get(transaction=transaction)
    state = ts.to_dict() if ts.exists else {}
    if state and state.get('uid') != current['uid']:
        return None
    completed = current.get('source_completed_at')
    attempt = current.get('attempt_count')
    if (
        type(current.get('account_routing_contract')) is not int
        or current['account_routing_contract'] != 1
        or current.get('admission_epoch') != policy['epoch']
        or not isinstance(completed, datetime)
        or completed.tzinfo is None
        or not policy['source_completion_boundary'] <= completed <= now
        or type(attempt) is not int
        or not 0 <= attempt < policy['quotas']['max_attempts']
        or current.get('status') not in {'pending', 'retryable'}
        or current.get('lease_token') is not None
        or current.get('lease_expires_at') is not None
        or any(
            current.get(k)
            for k in (
                'dispatch_epoch',
                'canary_epoch',
                'dispatch_budget',
                'completed_at',
                'receipt',
                'semantic_start',
                'source_consumed',
            )
        )
        or current.get('dispatch_state') not in {'never_started', 'verified_no_dispatch'}
        or (
            attempt > 0
            and (
                current.get('retry_safety') != 'verified_no_dispatch'
                or current.get('last_error_code') not in SAFE_RETRY_CODES
            )
        )
        or (attempt == 0 and current.get('status') != 'pending')
    ):
        return None
    approved = _qualified_disposition(transaction, db, current, policy, state, now) if (state.get('quarantined_job_id') or policy.get('one_source_qualification') is not None or 'native_source_once' in policy or 'ordinary_source_restoration_v2' in policy or policy.get('mode') == 'per_account_lifecycle_v1') else None
    if policy.get('one_source_qualification') is not None and approved is None:return None
    if (state.get('quarantined_job_id') or 'native_source_once' in policy or 'ordinary_source_restoration_v2' in policy or policy.get('mode') == 'per_account_lifecycle_v1') and approved is None:
        transaction.update(
            ref,
            {
                'status': 'blocked',
                'dispatch_state': 'tenant_quarantined',
                'operator_action': 'reconcile_tenant_quarantined_operation',
                'updated_at': now,
            },
        )
        return None
    if state.get('active_job_id'):
        return None
    due = current.get('next_attempt_at')
    if not isinstance(due, datetime) or due.tzinfo is None or due > now:
        return None
    q = policy['quotas']
    hour = int(now.timestamp()) // 3600
    global_count = policy['hour_claims'] if policy['hour'] == hour else 0
    tenant_count = state.get('hour_claims', 0) if state.get('hour') == hour else 0
    if (
        type(tenant_count) is not int
        or tenant_count < 0
        or global_count >= q['max_claims_per_hour']
        or len(policy['active']) >= q['max_concurrency']
        or len(policy['quarantined_tenants']) + len(policy['active']) >= q['max_quarantined_tenants']
    ):
        return None
    if tenant_count >= q['max_tenant_claims_per_hour']:
        transaction.update(
            ref,
            {
                'next_attempt_at': datetime.fromtimestamp((hour + 1) * 3600, timezone.utc),
                'throttle_reason': 'normal_tenant_rate_limited',
                'updated_at': now,
            },
        )
        return None
    token = str(uuid4())
    expires = now + timedelta(seconds=lease_seconds)
    operation = {k: current[k] for k in IDENTITY}
    operation.update(lease_token=token, lease_expires_at=expires)
    update = {
        'status': 'running',
        'attempt_count': attempt + 1,
        'lease_token': token,
        'lease_expires_at': expires,
        'dispatch_state': 'reserved',
        'throttle_reason': None,
        'retry_safety': None,
        'operation': operation,
        'updated_at': now,
    }
    if approved is not None:
        transaction.update(approved, {'state': 'consumed', 'job_id': current['job_id'], 'lease_token': token, 'consumed_at': now})
    transaction.update(
        reference(db), {'active': policy['active'] + [operation], 'hour': hour, 'hour_claims': global_count + 1,
                        **({'one_source_qualification':{**policy['one_source_qualification'],'claims_consumed':1}} if policy.get('one_source_qualification') is not None else {})}
    )
    transaction.set(
        tenant,
        {
            **state,
            'uid': current['uid'],
            'active_job_id': current['job_id'],
            'hour': hour,
            'hour_claims': tenant_count + 1,
        },
    )
    transaction.update(ref, update)
    return {**current, **update}


@transactional
def begin(transaction, db, ref, *, lease_token, now):
    snapshot = ref.get(transaction=transaction)
    job = snapshot.to_dict() if snapshot.exists else {}
    ps = reference(db).get(transaction=transaction)
    policy = ps.to_dict() if ps.exists else {}
    if not job:
        return False
    if policy.get('one_source_qualification') is not None:
        from database.normal_future_source_disposition import qualification_gate
        if not qualification_gate(policy,job,now,consumed=True):return False
    ts = tenant_ref(db, job['uid']).get(transaction=transaction)
    tenant = ts.to_dict() if ts.exists else {}
    approved = _qualified_disposition(transaction, db, job, policy, tenant, now, consumed=True) if (tenant.get('quarantined_job_id') or policy.get('one_source_qualification') is not None or 'native_source_once' in policy or 'ordinary_source_restoration_v2' in policy or policy.get('mode') == 'per_account_lifecycle_v1') else None
    if (
        not valid_policy(policy, now)
        or not owner_allowed(policy, job.get('uid'))
        or job.get('admission_epoch') != policy['epoch']
        or job.get('status') != 'running'
        or job.get('dispatch_state') != 'reserved'
        or job.get('lease_token') != lease_token
        or job['lease_expires_at'] <= now
        or tenant.get('active_job_id') != job['job_id']
        or ((tenant.get('quarantined_job_id') or policy.get('one_source_qualification') is not None or 'native_source_once' in policy or 'ordinary_source_restoration_v2' in policy or policy.get('mode') == 'per_account_lifecycle_v1') and approved is None)
        or not any(a.get('job_id') == job['job_id'] and a.get('lease_token') == lease_token for a in policy['active'])
    ):
        return False
    transaction.update(ref, {'dispatch_state': 'started_unknown', 'dispatch_started_at': now, 'updated_at': now})
    return True


@transactional
def finish(transaction, db, ref, *, lease_token, now, outcome, error_code='', receipt=None):
    snapshot = ref.get(transaction=transaction)
    job = snapshot.to_dict() if snapshot.exists else {}
    ps = reference(db).get(transaction=transaction)
    policy = ps.to_dict() if ps.exists else {}
    if not job:
        return False
    tr = tenant_ref(db, job['uid'])
    ts = tr.get(transaction=transaction)
    tenant = ts.to_dict() if ts.exists else {}
    if (
        job.get('status') != 'running'
        or job.get('lease_token') != lease_token
        or job.get('admission_epoch') != policy.get('epoch')
        or tenant.get('active_job_id') != job['job_id']
    ):
        return False
    active = policy.get('active', [])
    if not any(a.get('job_id') == job['job_id'] and a.get('lease_token') == lease_token for a in active):
        return False
    update = {
        'lease_token': None,
        'lease_expires_at': None,
        'updated_at': now,
        'last_error_code': error_code[:120] or None,
    }
    tu = {**tenant, 'active_job_id': None}
    pu = {'active': [a for a in active if a['job_id'] != job['job_id']]}
    if outcome == 'success' and job.get('dispatch_state') == 'started_unknown' and isinstance(receipt, dict):
        update.update(status='completed', dispatch_state='completed', receipt=receipt, completed_at=now)
    elif outcome == 'preflight' and job.get('dispatch_state') == 'reserved':
        retry = error_code in SAFE_RETRY_CODES and job['attempt_count'] < policy['quotas']['max_attempts']
        update.update(
            status='retryable' if retry else 'blocked',
            dispatch_state='verified_no_dispatch',
            retry_safety='verified_no_dispatch',
            next_attempt_at=now + timedelta(seconds=policy['quotas']['retry_delay_seconds']) if retry else None,
            operator_action=None if retry else 'repair_preflight_then_review_exact_job',
        )
    else:
        update.update(
            status='quarantined',
            dispatch_state='unknown_quarantined',
            next_attempt_at=None,
            operator_action='reconcile_exact_operation_readback_and_owned_provider_cleanup',
        )
        if tenant.get('quarantined_job_id') and tenant['quarantined_job_id'] != job['job_id']:
            prior = tenant.get('additional_quarantined_job_ids', [])
            tu['additional_quarantined_job_ids'] = prior if job['job_id'] in prior else prior + [job['job_id']]
        else:
            tu['quarantined_job_id'] = job['job_id']
        quarantined = policy.get('quarantined_tenants', [])
        pu['quarantined_tenants'] = quarantined if job['uid'] in quarantined else quarantined + [job['uid']]
    transaction.update(reference(db), pu)
    transaction.set(tr, tu)
    transaction.update(ref, update)
    return True


@transactional
def pause(transaction, ref, *, now):
    snapshot = ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    transaction.update(ref, {'state': 'paused', 'paused_at': now})
    return True


def _qualified_disposition(transaction, db, current, policy, tenant, now, **kwargs):
    if policy.get('mode') == 'per_account_lifecycle_v1':
        from database.normal_account_lifecycle import eligible
        return eligible(transaction,db,current,policy,tenant,now,**kwargs)
    if 'ordinary_source_restoration_v2' in policy:
        from database.normal_ordinary_source_disposition import eligible
        return eligible(transaction,db,current,policy,tenant,now,**kwargs)
    if 'native_source_once' in policy:
        from database.normal_native_source_once import eligible
        return eligible(transaction, db, current, policy, tenant, now, **kwargs)
    if policy.get('one_source_qualification') is not None:
        from database.normal_future_source_disposition import eligible
        return eligible(transaction,db,current,policy,tenant,now,**kwargs)
    original = disposition.eligible(transaction, db, current, policy, tenant, now, **kwargs)
    if original is not None:
        return original
    from database.normal_completed_source_recovery import eligible
    recovered = eligible(transaction, db, current, policy, tenant, now, **kwargs)
    if recovered is not None:
        return recovered
    from database.normal_future_source_disposition import eligible as future_eligible
    return future_eligible(transaction, db, current, policy, tenant, now, **kwargs)
