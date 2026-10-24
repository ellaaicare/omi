"""Retryable delivery worker for the durable Hermes Cloud enrichment outbox."""

from __future__ import annotations
from ella.services.semantic_operation_budget import operation_budget_scope

import asyncio
import math
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit

import requests

from ella.services.normal_account_routing import resolve_route
from ella.services import normal_account_routing
from ella.services.runtime_errors import ProvisioningError
from ella.services.causal_diagnostics import emit as emit_causal_diagnostic

from database.hermes_cloud_enrichment_outbox import (
    FirestoreHermesCloudEnrichmentOutbox,
)

HERMES_CLOUD_ENRICHMENT_PATH = "/v1/ella/internal/hermes-cloud/enrichment/run"
DEFAULT_URL = "http://127.0.0.1:8000" "/v1/ella/internal/hermes-cloud/enrichment/run"


class EnrichmentOutboxRepository(Protocol):
    def claim_next(
        self,
        *,
        lease_seconds: int,
        scan_limit: int = 50,
    ) -> Optional[dict[str, Any]]: ...

    def complete(
        self,
        *,
        job_id: str,
        lease_token: str,
        receipt: dict[str, Any],
    ) -> bool: ...

    def fail(
        self,
        *,
        job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_after_seconds: int,
    ) -> bool: ...


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error_code: str = ""
    retryable: bool = True
    receipt: Optional[dict[str, Any]] = None


def is_safe_enrichment_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        and parsed.path == HERMES_CLOUD_ENRICHMENT_PATH
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _error_code(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "hermes_cloud_enrichment_invalid_error_receipt"
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return str(code)[:120] if isinstance(code, str) and code else f"hermes_cloud_enrichment_http_{response.status_code}"


def deliver_enrichment_job(job: dict[str, Any]) -> DeliveryResult:
    if os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", "false").lower() != "true":
        return DeliveryResult(False, "hermes_cloud_enrichment_disabled")
    token = os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", "")
    if len(token) < 32:
        return DeliveryResult(
            False,
            "hermes_cloud_enrichment_auth_not_configured",
        )
    url = os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_URL", DEFAULT_URL)
    if not is_safe_enrichment_url(url):
        return DeliveryResult(False, "hermes_cloud_enrichment_url_invalid")
    try:
        timeout = float(os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_TIMEOUT", "180"))
    except ValueError:
        return DeliveryResult(False, "hermes_cloud_enrichment_timeout_invalid")
    if timeout <= 0 or timeout > 600:
        return DeliveryResult(False, "hermes_cloud_enrichment_timeout_invalid")

    payload = {
        "uid": job["uid"],
        "conversation_id": job["conversation_id"],
        "outbox_job_id": job["job_id"],
        "client_interaction_id": job["client_interaction_id"],
        "transcript_sha256": job["transcript_sha256"],
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Ella-Hermes-Cloud-Enrichment-Token": token,
                "X-Ella-Subject-Uid": str(job["uid"]),
            },
            timeout=timeout,
        )
    except requests.Timeout:
        return DeliveryResult(False, "hermes_cloud_enrichment_timeout")
    except requests.RequestException:
        return DeliveryResult(False, "hermes_cloud_enrichment_unavailable")
    if response.status_code != 200:
        return DeliveryResult(
            False,
            _error_code(response),
            retryable=(response.status_code in {408, 425, 429} or response.status_code >= 500),
        )
    try:
        body = response.json()
    except ValueError:
        return DeliveryResult(False, "hermes_cloud_enrichment_invalid_receipt")
    valid = (
        isinstance(body, dict)
        and body.get("ok") is True
        and body.get("status") == "applied"
        and body.get("content_free") is True
        and body.get("outbox_job_id") == job["job_id"]
        and body.get("client_interaction_id") == job["client_interaction_id"]
        and body.get("transcript_sha256") == job["transcript_sha256"]
    )
    if not valid:
        return DeliveryResult(False, "hermes_cloud_enrichment_invalid_receipt")
    return DeliveryResult(
        True,
        receipt={
            "runtime_interaction_id": body.get("runtime_interaction_id"),
            "active_summary_version_id": body.get("active_summary_version_id"),
            "duplicate": bool(body.get("duplicate")),
            "content_free": True,
        },
    )


class HermesCloudEnrichmentOutboxWorker:
    def __init__(
        self,
        repository: EnrichmentOutboxRepository,
        *,
        deliver=deliver_enrichment_job,
        lease_seconds: int = 240,
        retry_base_seconds: int = 10,
        poll_seconds: float = 2.0,
    ):
        self.repository = repository
        self.deliver = deliver
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.poll_seconds = poll_seconds

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(
            self.repository.claim_next,
            lease_seconds=self.lease_seconds,
        )
        if not job:
            return False
        if getattr(self.repository, "policy", None) is not None:
            print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=claimed", flush=True)
        result = await asyncio.to_thread(self.deliver, job)
        if result.ok:
            completed = await asyncio.to_thread(
                self.repository.complete,
                job_id=job["job_id"],
                lease_token=job["lease_token"],
                receipt=result.receipt or {"content_free": True},
            )
            if not completed:
                raise RuntimeError("hermes_cloud_enrichment_lease_lost")
            if getattr(self.repository, "policy", None) is not None:
                print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=completed", flush=True)
            return True
        retry_after = min(
            900,
            self.retry_base_seconds * (2 ** min(int(job.get("attempt_count") or 1) - 1, 6)),
        )
        failed = await asyncio.to_thread(
            self.repository.fail,
            job_id=job["job_id"],
            lease_token=job["lease_token"],
            error_code=result.error_code,
            retryable=result.retryable and getattr(self.repository, "automatic_retry_allowed", True),
            retry_after_seconds=retry_after,
        )
        if not failed:
            raise RuntimeError("hermes_cloud_enrichment_lease_lost")
        if getattr(self.repository, "policy", None) is not None:
            print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=blocked", flush=True)
        return True

    async def run_forever(self) -> None:
        waiting_reported = False
        while True:
            policy = getattr(self.repository, "policy", None)
            if policy is not None:
                from datetime import datetime, timezone

                if datetime.now(timezone.utc) >= policy.end:
                    print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=window_closed", flush=True)
                    return
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "[HERMES_CLOUD_ENRICHMENT_OUTBOX] worker error " f"type={type(exc).__name__}",
                    flush=True,
                )
                if policy is not None:
                    print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=hold", flush=True)
                    return  # Unknown outcome retains its budget/START; no retry.
                worked = False
            if worked and policy is not None:
                return  # One canary outcome. Its budget remains spent durably.
            if not worked:
                if policy is not None and not waiting_reported:
                    print("[HERMES_CLOUD_ENRICHMENT_CANARY] state=waiting", flush=True)
                    waiting_reported = True
                await asyncio.sleep(self.poll_seconds)


_worker_task: Optional[asyncio.Task] = None


async def start_worker(
    worker: Optional[HermesCloudEnrichmentOutboxWorker] = None,
) -> None:
    """Opt-in one next-item canary; no default or injected rearming."""
    global _worker_task
    if os.getenv('ELLA_NORMAL_ACCOUNT_ENRICHMENT_ENABLED', 'false').lower() == 'true':
        return await start_normal_account_worker(worker)
    if os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", "false").lower() != "true":
        return
    if os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_WORKER_ENABLED", "false").lower() != "true":
        return
    if _worker_task is not None:
        raise RuntimeError("dispatch_worker_already_owned")
    from datetime import datetime, timezone
    from database.bounded_hermes_enrichment_outbox import CanaryPolicy, BoundedFirestoreEnrichmentOutbox
    from ella.services.runtime_resolver import retained_owner_uid_configured

    selected = frozenset(
        u.strip() for u in os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS", "").split(",") if u.strip()
    )
    if len(selected) != 1 or any(not retained_owner_uid_configured(u) for u in selected):
        raise ValueError("dispatch_exact_retained_canary_owner_required")
    policy = CanaryPolicy(
        epoch=os.environ["ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_EPOCH"],
        owners=selected,
        start=datetime.fromisoformat(os.environ["ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_START_UTC"]),
        end=datetime.fromisoformat(os.environ["ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_END_UTC"]),
    )
    now = datetime.now(timezone.utc)
    if not policy.start <= now < policy.end:
        return  # Expired arm holds dispatch without breaking ordinary app startup.
    if len(os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", "")) < 32:
        raise ValueError("dispatch_auth_not_configured")
    if not is_safe_enrichment_url(os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_URL", DEFAULT_URL)):
        raise ValueError("dispatch_url_invalid")
    timeout = float(os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_TIMEOUT", "180"))
    if not 0 < timeout <= 600:
        raise ValueError("dispatch_timeout_invalid")
    if worker is not None:
        if (
            not isinstance(worker.repository, BoundedFirestoreEnrichmentOutbox)
            or worker.repository.policy != policy
            or worker.lease_seconds < 900
        ):
            raise ValueError("dispatch_injected_worker_not_bounded")
        active = worker
    else:
        active = HermesCloudEnrichmentOutboxWorker(BoundedFirestoreEnrichmentOutbox(policy=policy), lease_seconds=900)
    _worker_task = asyncio.create_task(active.run_forever())


async def attest_paused_worker_startup() -> None:
    """Witness actual startup state; durable claim budget is enforced in Firestore."""
    import datetime
    import json
    import stat

    if os.geteuid() != 0:
        raise RuntimeError("enrichment_paused_witness_requires_root")
    pid = os.getpid()
    payload = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pid": pid,
        "automatic_claim_budget": 1 if _worker_task is not None else 0,
        "worker_task_present": _worker_task is not None,
        "paused": _worker_task is None,
    }
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    directory = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = os.open(
            f"ella-personal-enrichment-paused-{pid}.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("enrichment_paused_witness_identity")
        written = 0
        while written < len(raw):
            n = os.write(fd, raw[written:])
            if n <= 0:
                raise RuntimeError("enrichment_paused_witness_write")
            written += n
        os.fsync(fd)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


async def stop_worker() -> None:
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None


_normal_worker_live = False
_normal_worker_repository = None


def normal_account_worker_ready():
    if not (
        _normal_worker_live
        and _worker_task is not None
        and not _worker_task.done()
        and _normal_worker_repository is not None
    ):
        return False
    try:
        return _normal_worker_repository.dispatch_ready() is True
    except Exception:
        return False


async def start_normal_account_worker(worker=None):
    global _worker_task, _normal_worker_live, _normal_worker_repository
    if worker is not None:
        raise ValueError('normal_account_worker_injection_forbidden')
    if _worker_task is not None:
        raise RuntimeError('dispatch_worker_already_owned')
    for key in ('ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED', 'ELLA_HERMES_CLOUD_ENRICHMENT_WORKER_ENABLED'):
        if os.getenv(key, 'false').lower() != 'true':
            return
    if any(
        os.getenv(key, '').strip()
        for key in (
            'ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_EPOCH',
            'ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_START_UTC',
            'ELLA_HERMES_CLOUD_ENRICHMENT_DISPATCH_END_UTC',
        )
    ):
        raise ValueError('normal_account_worker_consumed_canary_scope_present')
    try:
        timeout = float(os.getenv('ELLA_HERMES_CLOUD_ENRICHMENT_TIMEOUT', '180'))
    except ValueError:
        raise ValueError('dispatch_timeout_invalid') from None
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError('dispatch_timeout_invalid')
    from database.hermes_cloud_enrichment_outbox import NormalAccountEnrichmentOutbox

    repository = NormalAccountEnrichmentOutbox()
    if not await asyncio.wait_for(asyncio.to_thread(repository.validate_readonly), timeout=10):
        return  # Missing/stale policy holds dispatch without breaking app startup.
    from ella.services.hermes_cloud_enrichment_dependencies import create_default_hermes_cloud_enrichment_service

    service = await asyncio.wait_for(create_default_hermes_cloud_enrichment_service(), timeout=30)
    active = NormalAccountInProcessWorker(repository, service, lease_seconds=900)
    _worker_task = asyncio.create_task(active.run_forever())
    _normal_worker_repository = repository
    normal_account_routing.processing_status_reader = repository.tenant_processing_state
    _normal_worker_live = True


from database.normal_future_source_disposition import worker_context


@worker_context
async def direct_enrichment(service, job):
    """Trusted normal backend worker, exact existing factory; no HTTP auth bypass.

    The private library entry requires source/owner/consent/model/context gates.
    It is not exposed as a new endpoint and never invokes a revoked HTTP route.
    """
    from ella.services.runtime_errors import ProvisioningError

    try:
        with operation_budget_scope(job, 600):
            async with asyncio.timeout(600):
                result = await service.enrich(
                    uid=job['uid'],
                    conversation_id=job['conversation_id'],
                    allow_shadow=False,
                    expected_client_interaction_id=job['client_interaction_id'],
                    expected_transcript_sha256=job['transcript_sha256'],
                )
        if (
            result.conversation_id != job['conversation_id']
            or result.client_interaction_id != job['client_interaction_id']
            or result.transcript_sha256 != job['transcript_sha256']
            or result.provider_response_present is not True
        ):
            return DeliveryResult(False, 'normal_enrichment_receipt_mismatch', retryable=False)
        return DeliveryResult(
            True,
            receipt={
                'runtime_interaction_id': result.runtime_interaction_id,
                'active_summary_version_id': result.active_summary_version_id,
                'duplicate': bool(result.duplicate),
                'content_free': True,
            },
        )
    except ProvisioningError as exc:
        # No unknown START/model response is replayed. Durable provisioning retry
        # is separate from semantic-source no-rearm; holds require reconciliation.
        codes = {
            'account_consent_required',
            'account_runtime_not_ready',
            'account_model_unavailable',
            'account_capability_incomplete',
            'account_owner_mismatch',
            'account_runtime_changed',
            'account_provider_unsupported',
            'retained_completion_rejected',
        }
        diagnostic = emit_causal_diagnostic(exc, 'normal_enrichment_dispatch')
        return DeliveryResult(
            False,
            exc.code if exc.code in codes else 'normal_enrichment_held',
            retryable=False,
            receipt={'failure_diagnostic': diagnostic},
        )
    except TimeoutError as error:
        diagnostic = emit_causal_diagnostic(error, 'normal_enrichment_dispatch')
        return DeliveryResult(
            False, 'normal_enrichment_timeout_unknown', retryable=False, receipt={'failure_diagnostic': diagnostic}
        )
    except asyncio.CancelledError:
        raise  # Leave owned running lease unresolved; never make it pending again.
    except Exception as error:
        diagnostic = emit_causal_diagnostic(error, 'normal_enrichment_dispatch')
        return DeliveryResult(
            False, 'normal_enrichment_unknown', retryable=False, receipt={'failure_diagnostic': diagnostic}
        )


async def normal_preflight(service, job):
    # Read-only consent/tenant/runtime checks; NO service.enrich/model dispatch.
    route = await resolve_route(job['uid'], service.repository)
    from database.normal_future_source_disposition import require_route

    require_route(job, route)


class NormalAccountInProcessWorker(HermesCloudEnrichmentOutboxWorker):
    def __init__(self, repository, service, **kwargs):
        super().__init__(repository, **kwargs)
        self.service = service

    async def run_once(self):
        job = await asyncio.to_thread(self.repository.claim_next, lease_seconds=self.lease_seconds)
        if not job:
            return False
        args = {'job_id': job['job_id'], 'lease_token': job['lease_token']}
        try:
            try:
                async with asyncio.timeout(30):
                    await normal_preflight(self.service, job)
            except ProvisioningError as exc:
                ok = await asyncio.to_thread(
                    self.repository.finish_dispatch, **args, outcome='preflight', error_code=exc.code
                )
                if not ok:
                    raise RuntimeError('normal_enrichment_lease_lost')
                return True
            except Exception:
                ok = await asyncio.to_thread(
                    self.repository.finish_dispatch,
                    **args,
                    outcome='preflight',
                    error_code='normal_preflight_unavailable',
                )
                if not ok:
                    raise RuntimeError('normal_enrichment_lease_lost')
                return True
            if not await asyncio.to_thread(self.repository.begin_dispatch, **args):
                # Policy stop/race: no model called. Conservative exact-job hold.
                await asyncio.to_thread(
                    self.repository.finish_dispatch,
                    **args,
                    outcome='preflight',
                    error_code=(
                        'queue_maintenance'
                        if await asyncio.to_thread(self.repository.maintenance_closed)
                        else 'normal_dispatch_admission_changed'
                    ),
                )
                return True
            result = await direct_enrichment(self.service, job)
            ok = await asyncio.to_thread(
                self.repository.finish_dispatch,
                **args,
                outcome='success' if result.ok else 'unknown',
                error_code=result.error_code,
                receipt=result.receipt,
            )
            if not ok:
                raise RuntimeError('normal_enrichment_lease_lost')
            return True
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self.repository.finish_dispatch,
                    **args,
                    outcome='unknown',
                    error_code='normal_enrichment_cancelled_unknown',
                )
            )
            raise

    async def run_forever(self):
        while True:
            try:
                if not await asyncio.to_thread(self.repository.dispatch_ready):
                    return
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reservation/START is durable. Expiry sweeper quarantines only
                # that job/tenant; transient persistence failures do not rearm it.
                worked = False
                print('[NORMAL_ENRICHMENT] state=storage_or_owned_job_hold', flush=True)
            if not worked:
                await asyncio.sleep(self.poll_seconds)
