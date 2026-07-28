import asyncio
from copy import deepcopy

import pytest
import requests

from ella.services.hermes_cloud_enrichment_outbox import (
    DeliveryResult,
    HermesCloudEnrichmentOutboxWorker,
    deliver_enrichment_job,
)


def _job():
    return {
        "job_id": "hce_" + ("a" * 64),
        "uid": "synthetic-user",
        "conversation_id": "conversation-a",
        "client_interaction_id": "omi-enrichment:" + ("b" * 64),
        "transcript_sha256": "c" * 64,
        "status": "pending",
        "attempt_count": 0,
    }


class FakeOutbox:
    def __init__(self):
        self.job = _job()
        self.completed = []
        self.failures = []
        self.lease_number = 0

    def claim_next(self, *, lease_seconds, scan_limit=50):
        if self.job["status"] not in {"pending", "retryable", "running"}:
            return None
        if self.job["status"] == "running" and not self.job.get("lease_expired"):
            return None
        self.lease_number += 1
        self.job.update(
            status="running",
            lease_token=f"lease-{self.lease_number}",
            attempt_count=self.job["attempt_count"] + 1,
            lease_expired=False,
        )
        return deepcopy(self.job)

    def complete(self, **kwargs):
        assert kwargs["lease_token"] == self.job["lease_token"]
        self.job["status"] = "completed"
        self.completed.append(kwargs)
        return True

    def fail(self, **kwargs):
        assert kwargs["lease_token"] == self.job["lease_token"]
        self.job["status"] = "retryable" if kwargs["retryable"] else "blocked"
        self.failures.append(kwargs)
        return True


@pytest.mark.parametrize(
    ("enabled", "token", "expected_code"),
    [
        (
            "false",
            "",
            "hermes_cloud_enrichment_disabled",
        ),
        (
            "true",
            "",
            "hermes_cloud_enrichment_auth_not_configured",
        ),
    ],
)
def test_missing_or_disabled_auth_is_durably_retryable(
    monkeypatch,
    enabled,
    token,
    expected_code,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", token)
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", enabled)
    repository = FakeOutbox()
    worker = HermesCloudEnrichmentOutboxWorker(repository)

    asyncio.run(worker.run_once())

    assert repository.job["status"] == "retryable"
    assert repository.failures[0]["error_code"] == expected_code
    assert repository.completed == []


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("timeout", "hermes_cloud_enrichment_timeout"),
        ("503", "hermes_cloud_enrichment_http_503"),
    ],
)
def test_timeout_and_503_are_retryable(
    monkeypatch,
    failure,
    expected_code,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN",
        "x" * 32,
    )

    def fake_post(*args, **kwargs):
        if failure == "timeout":
            raise requests.Timeout()
        return type(
            "Response",
            (),
            {
                "status_code": 503,
                "json": lambda self: {},
            },
        )()

    monkeypatch.setattr(requests, "post", fake_post)
    result = deliver_enrichment_job(_job())

    assert result == DeliveryResult(
        False,
        expected_code,
        retryable=True,
    )


def test_401_blocks_until_operator_repairs_loopback_token(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN",
        "x" * 32,
    )

    class UnauthorizedResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"detail": {"code": "hermes_cloud_enrichment_auth_failed"}}

    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: UnauthorizedResponse(),
    )
    result = deliver_enrichment_job(_job())
    repository = FakeOutbox()
    worker = HermesCloudEnrichmentOutboxWorker(
        repository,
        deliver=lambda job: result,
    )

    asyncio.run(worker.run_once())

    assert result.retryable is False
    assert repository.job["status"] == "blocked"
    assert repository.failures[0]["error_code"] == ("hermes_cloud_enrichment_auth_failed")


def test_expired_lease_reclaims_after_process_interruption():
    repository = FakeOutbox()
    first_claim = repository.claim_next(lease_seconds=240)
    assert first_claim["lease_token"] == "lease-1"

    repository.job["lease_expired"] = True
    worker = HermesCloudEnrichmentOutboxWorker(
        repository,
        deliver=lambda job: DeliveryResult(
            True,
            receipt={"content_free": True},
        ),
    )
    asyncio.run(worker.run_once())

    assert repository.job["status"] == "completed"
    assert repository.completed[0]["lease_token"] == "lease-2"


def test_successful_replay_is_idempotent():
    repository = FakeOutbox()
    deliveries = []
    worker = HermesCloudEnrichmentOutboxWorker(
        repository,
        deliver=lambda job: deliveries.append(job["job_id"])
        or DeliveryResult(
            True,
            receipt={"content_free": True, "duplicate": True},
        ),
    )

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    assert deliveries == [_job()["job_id"]]
