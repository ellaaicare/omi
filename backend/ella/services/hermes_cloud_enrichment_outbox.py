"""Retryable delivery worker for the durable Hermes Cloud enrichment outbox."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit

import requests

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
            retryable=(response.status_code in {401, 408, 425, 429} or response.status_code >= 500),
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
            retryable=result.retryable,
            retry_after_seconds=retry_after,
        )
        if not failed:
            raise RuntimeError("hermes_cloud_enrichment_lease_lost")
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "[HERMES_CLOUD_ENRICHMENT_OUTBOX] worker error " f"type={type(exc).__name__}",
                    flush=True,
                )
                worked = False
            if not worked:
                await asyncio.sleep(self.poll_seconds)


_worker_task: Optional[asyncio.Task] = None


async def start_worker(
    worker: Optional[HermesCloudEnrichmentOutboxWorker] = None,
) -> None:
    global _worker_task
    selected = {
        value.strip()
        for value in os.getenv(
            "ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS",
            "",
        ).split(",")
        if value.strip()
    }
    if not selected or (_worker_task and not _worker_task.done()):
        return
    active_worker = worker or HermesCloudEnrichmentOutboxWorker(FirestoreHermesCloudEnrichmentOutbox())
    _worker_task = asyncio.create_task(active_worker.run_forever())


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
