import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import os

import pytest
import requests

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "omi-ci")

from database import content_write_fence
from database import hermes_cloud_enrichment_outbox as outbox_db
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

    def peek_next_uid(self, *, scan_limit=50):
        del scan_limit
        if self.job["status"] not in {"pending", "retryable", "running"}:
            return None
        return self.job["uid"]

    def claim_next(self, *, uid, writer_token, lease_seconds, scan_limit=50):
        assert uid == self.job["uid"]
        assert writer_token
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


@pytest.fixture(autouse=True)
def _admitted_worker(monkeypatch):
    class Writer:
        token = "test-writer-token"

        @staticmethod
        def assert_current():
            return None

    @asynccontextmanager
    async def admitted(_uid):
        yield Writer()

    monkeypatch.setattr(content_write_fence, "detached_content_write_fence", admitted)
    monkeypatch.setattr(content_write_fence, "assert_content_writer_admitted", lambda _uid: None)


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
    first_claim = repository.claim_next(
        uid=_job()["uid"],
        writer_token="direct-test-writer",
        lease_seconds=240,
    )
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


def test_stale_claim_fails_closed_at_external_delivery_boundary(monkeypatch):
    repository = FakeOutbox()
    deliveries = []

    class StaleWriter:
        token = "stale-writer-token"

        @staticmethod
        def assert_current():
            raise content_write_fence.ContentWriteFenceError("account_write_forbidden")

    @asynccontextmanager
    async def stale(_uid):
        yield StaleWriter()

    monkeypatch.setattr(content_write_fence, "detached_content_write_fence", stale)
    worker = HermesCloudEnrichmentOutboxWorker(
        repository,
        deliver=lambda job: deliveries.append(job) or DeliveryResult(True),
    )

    assert asyncio.run(worker.run_once()) is False
    assert repository.job["status"] == "running"
    assert deliveries == []
    assert repository.completed == []
    assert repository.failures == []


def test_firestore_repository_rejects_post_tombstone_claim_complete_fail_and_retry():
    class Snapshot:
        def __init__(self, data):
            self.data = data
            self.exists = data is not None

        def to_dict(self):
            return deepcopy(self.data)

    class Reference:
        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            del transaction
            return Snapshot(self.data)

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, reference, values):
            self.updates.append(values)
            reference.data.update(values)

    fence_reference = Reference({"state": content_write_fence.TOMBSTONED, "writers": {}})

    class Collection:
        def document(self, _document_id):
            return fence_reference

    class Database:
        def collection(self, name):
            assert name == content_write_fence.FENCE_COLLECTION
            return Collection()

    now = datetime.now(timezone.utc)
    database = Database()

    pending = Reference({**_job(), "status": "pending"})
    claim_transaction = Transaction()
    assert (
        outbox_db._claim_transaction.to_wrap(
            claim_transaction,
            database,
            pending,
            writer_token="missing-writer",
            now=now,
            lease_seconds=240,
        )
        is None
    )
    assert claim_transaction.updates == []
    assert pending.data["status"] == "pending"

    running = Reference({**_job(), "status": "running", "lease_token": "stale-lease"})
    complete_transaction = Transaction()
    assert (
        outbox_db._complete_transaction.to_wrap(
            complete_transaction,
            database,
            running,
            lease_token="stale-lease",
            receipt={"content_free": True},
            now=now,
        )
        is False
    )
    assert complete_transaction.updates == []

    for retryable in (True, False):
        fail_transaction = Transaction()
        assert (
            outbox_db._fail_transaction.to_wrap(
                fail_transaction,
                database,
                running,
                lease_token="stale-lease",
                error_code="stale_worker",
                retryable=retryable,
                next_attempt_at=now if retryable else None,
                now=now,
            )
            is False
        )
        assert fail_transaction.updates == []

    fence_reference.data = {
        "state": content_write_fence.ACTIVE,
        "writers": {"current-writer": now},
    }
    admitted = Reference({**_job(), "status": "pending"})
    admitted_transaction = Transaction()
    claimed = outbox_db._claim_transaction.to_wrap(
        admitted_transaction,
        database,
        admitted,
        writer_token="current-writer",
        now=now,
        lease_seconds=240,
    )
    assert claimed["status"] == "running"
    assert claimed["content_writer_token"] == "current-writer"

    complete_transaction = Transaction()
    assert outbox_db._complete_transaction.to_wrap(
        complete_transaction,
        database,
        admitted,
        lease_token=claimed["lease_token"],
        receipt={"content_free": True},
        now=now,
    )
    assert admitted.data["status"] == "completed"

    retry_job = Reference(
        {
            **_job(),
            "status": "running",
            "lease_token": "current-lease",
            "content_writer_token": "current-writer",
        }
    )
    retry_transaction = Transaction()
    assert outbox_db._fail_transaction.to_wrap(
        retry_transaction,
        database,
        retry_job,
        lease_token="current-lease",
        error_code="retryable_failure",
        retryable=True,
        next_attempt_at=now,
        now=now,
    )
    assert retry_job.data["status"] == "retryable"


def test_firestore_repository_rejects_post_tombstone_enqueue(monkeypatch):
    def forbidden(uid):
        assert uid == _job()["uid"]
        raise content_write_fence.ContentWriteFenceError("account_write_forbidden")

    monkeypatch.setattr(content_write_fence, "assert_content_writer_admitted", forbidden)
    repository = outbox_db.FirestoreHermesCloudEnrichmentOutbox(firestore_db=object())

    with pytest.raises(content_write_fence.ContentWriteFenceError, match="account_write_forbidden"):
        repository.enqueue(
            job_id=_job()["job_id"],
            uid=_job()["uid"],
            conversation_id=_job()["conversation_id"],
            client_interaction_id=_job()["client_interaction_id"],
            transcript_sha256=_job()["transcript_sha256"],
            policy_version="test-policy",
        )
