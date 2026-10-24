"""Normal per-account routing. No network, SDK or provisioning work on import."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
import os
import re
from uuid import UUID, uuid4
from ella.services.runtime_errors import ProvisioningError


def admitted_subject(uid):
    # This is a routing candidate, NEVER authentication or publication authority.
    return isinstance(uid, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,128}', uid)) and uid not in {'.', '..'}


def normal_capture_admission_enabled():
    return os.getenv('ELLA_NORMAL_ACCOUNT_ENRICHMENT_ENABLED', 'false').strip().lower() == 'true'


def activated_subject(uid):
    # Normal admission creates contract-1 pending sources; it does not start a
    # worker, grant consent, provision an agent, or claim a queue item.
    return admitted_subject(uid) and (
        normal_capture_admission_enabled()
        or os.getenv('ELLA_HERMES_PROVISIONING_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    )


def canonical_owner(challenge, uid):
    if not isinstance(challenge, dict) or challenge.get('firebase_uid') != uid:
        raise ProvisioningError('provision_owner_mismatch', retryable=False)
    owner = challenge.get('account_owner_id')
    try:
        if not isinstance(owner, str) or str(UUID(owner)) != owner:
            raise ValueError()
    except ValueError:
        raise ProvisioningError('provision_canonical_owner_required', retryable=False) from None
    return owner


async def consent(uid):
    from ella.services.ai_consent import assert_current_ai_consent

    try:
        result = await asyncio.to_thread(assert_current_ai_consent, uid)
    except Exception:
        raise ProvisioningError('account_consent_required', retryable=False) from None
    if result != uid:
        raise ProvisioningError('account_consent_required', retryable=False)


async def resolve_route(uid, repository, *, resolve=None, semantic=None, require_consent=None):
    if not admitted_subject(uid):
        raise ProvisioningError('account_subject_invalid', retryable=False)
    if resolve is None:
        from ella.services.runtime_resolver import resolve_isolated_runtime

        resolve = resolve_isolated_runtime
    if semantic is None:
        from ella.services.semantic_runtime_authority import resolve_semantic_runtime

        semantic = resolve_semantic_runtime
    require_consent = require_consent or consent
    await require_consent(uid)
    runtime = await resolve(uid, repository=repository, target_mode='hermes-cloud-transcript')
    if runtime is None or runtime.uid != uid or runtime.status != 'active':
        raise ProvisioningError('account_runtime_not_ready', retryable=True)
    provider = runtime.provider
    if provider == 'hermes':
        runtime = await semantic(uid, repository=repository, target_mode='hermes-cloud-transcript')
        if runtime.uid != uid or runtime.status != 'active' or runtime.provider != provider:
            raise ProvisioningError('account_runtime_changed', retryable=False)
        # Validated host policy supplies the model; never infer it from an alias.
        canonical_owner({'firebase_uid': uid, 'account_owner_id': runtime.account_user_id}, uid)
        if runtime.profile_user_id != runtime.account_user_id:
            raise ProvisioningError('account_owner_mismatch', retryable=False)
    elif provider != 'hermes_cloud':
        raise ProvisioningError('account_provider_unsupported', retryable=False)
    if not isinstance(runtime.expected_model, str) or not runtime.expected_model.strip():
        raise ProvisioningError('account_model_unavailable', retryable=True)
    for field in (
        'binding_id',
        'profile_name',
        'agent_id',
        'workspace_root',
        'honcho_workspace',
        'observed_peer',
        'observer_peer',
    ):
        if not getattr(runtime, field, None):
            raise ProvisioningError('account_capability_incomplete', retryable=True)
    await require_consent(uid)
    # Existing account validation stays usable when processing is not configured.
    # If a lifecycle record exists, its original destination must still match.
    if normal_capture_admission_enabled():
        from database._client import db
        from database.normal_account_lifecycle import assert_existing_mapping

        await asyncio.to_thread(assert_existing_mapping, db, uid, runtime)
    return runtime


def worker_ready():
    from ella.services.hermes_cloud_enrichment_outbox import normal_account_worker_ready

    return normal_account_worker_ready()


processing_status_reader = None


def _observe_account_route(uid, receipt, *, runtime=None, error=None, code=None):
    """Request-local account validation metadata; never exception text or locals."""
    try:

        def bounded_hash(value):
            return (
                hashlib.sha256(value.encode("utf-8")).hexdigest()
                if type(value) is str and 0 < len(value) <= 512
                else None
            )

        support = receipt.get("support_code")
        frames = []
        trace = error.__traceback__ if error is not None else None
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep
        while trace is not None:
            filename = trace.tb_frame.f_code.co_filename
            if type(filename) is str and filename.startswith(root):
                frames.append(
                    {
                        "module": filename[len(root) :].replace(os.sep, "/"),
                        "function": trace.tb_frame.f_code.co_name,
                        "line": trace.tb_lineno,
                    }
                )
            trace = trace.tb_next
        name = type(error).__name__ if error is not None else None
        row = {
            "contract": "ella-main-account-route-observation-v1",
            "correlation_id": uuid4().hex,
            "observed_utc": datetime.now(timezone.utc).isoformat(),
            "stage": "account_route_validation",
            "outcome": "error" if error is not None else "account_route_valid",
            "owner_ref_sha256": bounded_hash(uid),
            "job_id_sha256": bounded_hash(receipt.get("job_id")),
            "support_code": support if type(support) is str and re.fullmatch(r"ELLA-[0-9A-F]{8}", support) else None,
            "public_error_code": code if type(code) is str and re.fullmatch(r"[a-z_]{1,100}", code) else None,
            "cause_class": name if type(name) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else None,
            "application_frames": frames[-3:],
            "runtime_owner_matches": (getattr(runtime, "uid", None) == uid) if runtime is not None else None,
            "runtime_binding_sha256": (
                bounded_hash(getattr(runtime, "binding_id", None)) if runtime is not None else None
            ),
        }
        print("[MAIN-ACCOUNT-ROUTE] " + json.dumps(row, sort_keys=True, separators=(",", ":")), flush=True)
    except BaseException:
        # Correlation cannot replace original result, exception or cancellation.
        pass


async def qualify_receipt(uid, receipt, repository):
    if receipt.get('state') != 'ready':
        return receipt
    # Account authority is mandatory. Enrichment availability cannot repair it.
    try:
        runtime = await resolve_route(uid, repository)
    except Exception as exc:
        code = exc.code if isinstance(exc, ProvisioningError) else 'account_readiness_unavailable'
        retryable = exc.retryable if isinstance(exc, ProvisioningError) else True
        _observe_account_route(uid, receipt, error=exc, code=code)
        return {
            **receipt,
            'state': 'retryable' if retryable else 'blocked',
            'stage': 'validating',
            'retryable': retryable,
            'retry_after_ms': 2000 if retryable else None,
            'error_code': code,
            'account_ready': False,
            'enrichment_ready': False,
        }
    _observe_account_route(uid, receipt, runtime=runtime)
    account = {**receipt, 'account_ready': True, 'runtime_provider': runtime.provider}
    # Preserve a validated account's lifecycle when background processing pauses.
    # Chat, history and Journal still enforce their own ordinary route authority.
    try:
        if runtime.provider in {'hermes', 'hermes_cloud'}:
            dispatch_enabled = os.getenv('ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED', 'false').strip().lower() in {
                'true',
                '1',
                'yes',
                'on',
            }
            worker_enabled = os.getenv('ELLA_HERMES_CLOUD_ENRICHMENT_WORKER_ENABLED', 'false').strip().lower() in {
                'true',
                '1',
                'yes',
                'on',
            }
            if normal_capture_admission_enabled() and not dispatch_enabled and not worker_enabled:
                if worker_ready():
                    raise ProvisioningError('account_enrichment_worker_state_conflict', retryable=True)
                return {
                    **account,
                    'capture_ready': True,
                    'enrichment_ready': False,
                    'background_processing_paused': True,
                    'enrichment_processing_mode': 'controlled',
                }
            for key in ('ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED', 'ELLA_HERMES_CLOUD_ENRICHMENT_WORKER_ENABLED'):
                if os.getenv(key, 'false').strip().lower() not in {'true', '1', 'yes', 'on'}:
                    raise ProvisioningError('account_enrichment_dispatch_paused', retryable=True)
            if not worker_ready():
                raise ProvisioningError('account_enrichment_worker_unavailable', retryable=True)
            if normal_capture_admission_enabled():
                if processing_status_reader is None:
                    raise ProvisioningError('account_processing_state_unavailable', retryable=True)
                from database._client import db
                from database.normal_account_lifecycle import policy_shape, processing_status
                from database.normal_enrichment_activation import read_policy

                policy = await asyncio.to_thread(read_policy, db)
                processing = (
                    await asyncio.to_thread(processing_status, db, uid)
                    if policy_shape(policy)
                    else await asyncio.to_thread(processing_status_reader, uid)
                )
                return {
                    **account,
                    'capture_ready': True,
                    'enrichment_ready': processing['state'] in {'available', 'processing'},
                    'background_processing_paused': processing['state'] == 'paused',
                    'enrichment_processing_mode': 'operational',
                    'enrichment_processing_state': processing['state'],
                    **({'enrichment_queue_state': processing['queue_state']} if 'queue_state' in processing else {}),
                    'enrichment_error_code': processing.get('error_code'),
                    'enrichment_operator_action': processing.get('operator_action'),
                    'enrichment_retry_after_ms': processing.get('retry_after_ms'),
                }
    except Exception as exc:
        code = exc.code if isinstance(exc, ProvisioningError) else 'account_processing_state_unavailable'
        retryable = exc.retryable if isinstance(exc, ProvisioningError) else True
        return {
            **account,
            'enrichment_ready': False,
            'background_processing_paused': True,
            'enrichment_processing_state': 'paused',
            'enrichment_error_code': code,
            'enrichment_retryable': retryable,
            'enrichment_retry_after_ms': 2000 if retryable else None,
        }
    return {**account, 'enrichment_ready': True}


async def qualify_ensure_receipt(uid, receipt, repository, *, observation_id=None):
    """Existing authenticated /ensure only; status GET never enrolls.

    Default-off server processing configuration is distinct from user consent.
    Enabling it requires actual verified account scope and ongoing processing
    authorization. No UID exceptions, caller destinations or model operations.
    """
    qualified = await qualify_receipt(uid, receipt, repository)
    if not qualified.get('account_ready') or qualified.get('state') != 'ready':
        observe_enrollment_stage(uid, observation_id, 'non_ready', receipt=qualified)
        return qualified
    # qualify_receipt has JUST revalidated current consent/owned runtime and
    # processing_status/read_account (current policy, account epoch, route and
    # quarantine preimages). An already-active account needs no enrollment
    # transaction or two more full resolver passes. Never use a cached/client
    # receipt here, nor bypass the initial qualification above.
    if (
        qualified.get('enrichment_ready') is True
        and qualified.get('enrichment_processing_mode') == 'operational'
        and qualified.get('enrichment_processing_state') in {'available', 'processing'}
    ):
        observe_enrollment_stage(uid, observation_id, 'enroll_existing_validated', receipt=qualified)
        return qualified
    from database._client import db
    from database.normal_enrichment_activation import read_policy
    from database.normal_account_lifecycle import valid_policy, MODE, enroll_authenticated_account

    try:
        policy = await asyncio.to_thread(read_policy, db)
        if (
            policy.get('mode') != MODE
            or not valid_policy(policy, datetime.now(timezone.utc))
            or policy.get('enrollment_enabled') is not True
        ):
            observe_enrollment_stage(uid, observation_id, 'policy_held', receipt=qualified)
            return qualified
        await enroll_authenticated_account(
            uid, repository, authorization_ref=policy['lifecycle_authorization_ref'], expected_epoch=policy['epoch']
        )
        observe_enrollment_stage(uid, observation_id, 'enroll_commit', receipt=qualified)
        return await qualify_receipt(uid, receipt, repository)
    except Exception as exc:
        observe_enrollment_stage(uid, observation_id, 'enroll_exception', error=exc)
        # Never echo private identity/store errors or substitute another context.
        return {
            **qualified,
            'enrichment_ready': False,
            'background_processing_paused': True,
            'enrichment_processing_state': 'blocked',
            'enrichment_error_code': 'account_processing_enrollment_held',
            'enrichment_retryable': False,
        }


_ENROLLMENT_STAGES = frozenset(
    {
        'entry',
        'response',
        'ensure_error',
        'non_ready',
        'policy_held',
        'enroll_commit',
        'enroll_exception',
        'followup_deferred',
        'followup_terminal',
        'followup_error',
        'followup_exhausted',
        'pause_read_retry',
        'pause_read_error',
        'pause_read_recovered',
        'pause_control_error',
        'enroll_existing_validated',
    }
)
_ENROLLMENT_PUBLIC_CODES = frozenset(
    {
        'account_consent_required',
        'ai_consent_required',
        'ai_consent_authority_unavailable',
        'account_processing_not_configured',
        'account_processing_enrollment_held',
        'account_processing_existing_mapping_changed',
        'account_processing_owner_invalid',
        'account_processing_route_owner_mismatch',
        'account_processing_route_incomplete',
        'account_processing_enrollment_authority_changed',
        'account_processing_not_quiet',
        'account_processing_inventory_bound',
        'account_processing_inventory_owner_invalid',
        'account_processing_hold_unresolved_or_changed',
        'account_processing_hold_invalid',
        'account_processing_tenant_owner_mismatch',
        'account_processing_verified_enrollment_required',
        'account_runtime_not_ready',
        'account_owner_mismatch',
        'account_runtime_changed',
        'account_model_unavailable',
        'account_capability_incomplete',
        'account_processing_mapping_changed',
        'account_readiness_unavailable',
        'account_processing_state_unavailable',
        'provisioning_unavailable',
        'provisioning_schema_not_ready',
        'provisioning_disabled',
        'identity_missing_email',
        'auth_required',
        'onboarding_pause_state_unavailable',
        'invitation_authority_required',
        'self_hosted_invitation_authority_unavailable',
        'self_hosted_invitation_runtime_disabled',
        'invalid_target_schema_version',
        'onboarding_control_snapshot_invalid',
        'onboarding_control_read_budget_exhausted',
    }
)


def observe_enrollment_stage(uid, correlation, stage, *, receipt=None, error=None):
    """Content-free authenticated lifecycle telemetry; failure changes no result."""
    try:
        if stage not in _ENROLLMENT_STAGES:
            return
        if type(correlation) is not str or re.fullmatch('[a-f0-9]{32}', correlation) is None:
            return
        if not admitted_subject(uid):
            return
        detail = getattr(error, 'detail', None)
        code = detail.get('code') if type(detail) is dict else getattr(error, 'code', None)
        # Only literal reviewed identifiers may leave this boundary; regex alone
        # cannot distinguish a secret/text marker from a public error code.
        if code is None and type(error) is ValueError and len(error.args) == 1:
            code = error.args[0]
        if type(code) is not str or code not in _ENROLLMENT_PUBLIC_CODES:
            code = None
        name = type(error).__name__ if error is not None else None
        if name is not None and not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', name):
            name = None
        state = receipt.get('state') if type(receipt) is dict else None
        if state not in {
            'ready',
            'checking',
            'queued',
            'provisioning',
            'retryable',
            'rolling_back',
            'blocked',
            'manual_intervention',
        }:
            state = None
        row = {
            'contract': 'ella-authenticated-enrollment-observation-v1',
            'correlation_id': correlation,
            'observed_utc': datetime.now(timezone.utc).isoformat(),
            'owner_ref_sha256': hashlib.sha256(uid.encode()).hexdigest(),
            'stage': stage,
            'state': state,
            'account_ready': receipt.get('account_ready') is True if type(receipt) is dict else None,
            'exception_type': name,
            'public_error_code': code,
        }
        print('[MAIN-ENROLLMENT] ' + json.dumps(row, sort_keys=True, separators=(',', ':')), flush=True)
    except BaseException:
        pass
