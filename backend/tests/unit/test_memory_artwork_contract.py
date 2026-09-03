import asyncio
import base64
import copy
import importlib.util
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(autouse=True)
def _current_global_ai_consent(monkeypatch):
    monkeypatch.setattr(artwork, "has_current_global_ai_consent", lambda uid: True)


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _load_service_module():
    database_stub = types.ModuleType("database.memory_artwork")
    for name in (
        "get_preferences",
        "set_preferences",
        "get_conversation",
        "list_recent_conversations",
        "reserve_generation",
        "claim_generation",
        "finalize_generation",
        "mark_generation_unavailable",
        "claim_deletion",
        "list_pending_jobs",
        "claim_job",
        "job_claim_is_current",
        "complete_job",
        "retry_job",
        "fail_job",
        "mark_storage_cleanup_required",
    ):
        setattr(database_stub, name, lambda *args, **kwargs: None)
    database_stub.STORAGE_CLEANUP_REQUIRED_FIELD = "memory_artwork_storage_cleanup_required"
    database_stub.DELETION_PENDING_FIELD = "memory_artwork_deletion_pending"
    runtime_stub = types.ModuleType("ella.services.runtime_resolver")
    runtime_stub.resolve_isolated_runtime = lambda *args, **kwargs: None
    runtime_stub.runtime_authority_identity = lambda runtime: None
    saved = {name: sys.modules.get(name) for name in ("database.memory_artwork", "ella.services.runtime_resolver")}
    sys.modules["database.memory_artwork"] = database_stub
    sys.modules["ella.services.runtime_resolver"] = runtime_stub
    module_name = "ella_memory_artwork_contract_test_module"
    spec = importlib.util.spec_from_file_location(
        module_name,
        BACKEND_ROOT / "ella" / "services" / "memory_artwork.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


artwork = _load_service_module()


def _load_database_module():
    client_stub = types.ModuleType("database._client")
    client_stub.db = None
    original = sys.modules.get("database._client")
    sys.modules["database._client"] = client_stub
    module_name = "database.memory_artwork_contract_test_module"
    spec = importlib.util.spec_from_file_location(
        module_name,
        BACKEND_ROOT / "database" / "memory_artwork.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        if original is None:
            sys.modules.pop("database._client", None)
        else:
            sys.modules["database._client"] = original
    return module


artwork_database = _load_database_module()


def _terminal_memory(memory_id: str, *, created_at: datetime | None = None) -> dict:
    revision = f"summary-{memory_id}"
    return {
        "id": memory_id,
        "status": "completed",
        "created_at": created_at or datetime.now(timezone.utc),
        "active_summary_version_id": revision,
        "enrichment_state": {"status": "writeback_applied", "kind": "hermes_enriched"},
        "structured": {"title": f"Memory {memory_id}", "overview": "A quiet walk near a garden."},
        "ella_tags": [],
        "ella_signal": {"guardian_relevant": False},
        "internal_assessment": {"risk_level": "low"},
    }


class FakeRepository:
    def __init__(self):
        self.preferences_by_uid = {}
        self.conversations = {}
        self.reserve_writes = 0
        self.jobs = {}
        self.storage_cleanup_required = set()
        self.deletion_pending = set()

    def get_preferences(self, uid):
        result = copy.deepcopy(self.preferences_by_uid.get(uid, {}))
        result[artwork.artwork_db.DELETION_PENDING_FIELD] = uid in self.deletion_pending
        return result

    def set_preferences(self, uid, preferences):
        self.preferences_by_uid[uid] = copy.deepcopy(preferences)

    def get_conversation(self, uid, memory_id):
        value = self.conversations.get((uid, memory_id))
        return copy.deepcopy(value) if value is not None else None

    def list_recent_conversations(self, uid, *, limit):
        values = [copy.deepcopy(value) for (owner, _), value in self.conversations.items() if owner == uid]
        return sorted(values, key=lambda value: value["created_at"], reverse=True)[:limit]

    def reserve_generation(
        self,
        uid,
        memory_id,
        *,
        enrichment_revision,
        generation_key,
        artwork_state,
        job_state,
        preserve_job_attempts=False,
    ):
        if uid in self.deletion_pending:
            return {"outcome": "deletion_pending"}
        conversation = self.conversations.get((uid, memory_id))
        if conversation is None:
            return {"outcome": "not_found"}
        if conversation.get("active_summary_version_id") != enrichment_revision:
            return {"outcome": "source_changed"}
        job_key = (uid, memory_id, generation_key)
        existing_job = self.jobs.get(job_key) or {}
        effective_job = copy.deepcopy(job_state)
        effective_job["attempt_count"] = (
            int(existing_job.get("attempt_count") or 0)
            if preserve_job_attempts
            else int(job_state.get("attempt_count") or 0)
        )
        effective_job["created_at"] = existing_job.get("created_at") or effective_job.get("created_at")
        current = conversation.get("artwork") or {}
        current_status = current.get("status")
        ready_object_key = str(current.get("object_key") or "").strip()
        if current.get("generation_key") == generation_key and (
            current_status == "generating" or (current_status == "ready" and ready_object_key)
        ):
            if current_status == "generating" and existing_job.get("status") not in {"pending", "processing"}:
                self.jobs[job_key] = effective_job
            return {"outcome": "existing", "artwork": copy.deepcopy(current)}
        conversation["artwork"] = copy.deepcopy(artwork_state)
        if not (preserve_job_attempts and existing_job.get("status") == "processing"):
            self.jobs[job_key] = effective_job
        self.reserve_writes += 1
        return {"outcome": "reserved", "artwork": copy.deepcopy(artwork_state)}

    def claim_generation(self, uid, memory_id, *, generation_key, lease_token, now, lease_seconds):
        conversation = self.conversations.get((uid, memory_id))
        current = (conversation or {}).get("artwork") or {}
        if current.get("generation_key") != generation_key or current.get("status") != "generating":
            return None
        if current.get("lease_token"):
            return None
        current.update(
            {
                "lease_token": lease_token,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        return copy.deepcopy(current)

    def finalize_generation(
        self,
        uid,
        memory_id,
        *,
        generation_key,
        authority_digest,
        lease_token,
        ready_state,
    ):
        conversation = self.conversations.get((uid, memory_id))
        current = (conversation or {}).get("artwork") or {}
        if (
            current.get("generation_key") != generation_key
            or current.get("authority_digest") != authority_digest
            or current.get("lease_token") != lease_token
        ):
            return False
        conversation["artwork"] = copy.deepcopy(ready_state)
        return True

    def mark_generation_unavailable(
        self,
        uid,
        memory_id,
        *,
        generation_key,
        failure_code,
        lease_token=None,
        expected_artwork=None,
    ):
        conversation = self.conversations.get((uid, memory_id))
        current = (conversation or {}).get("artwork") or {}
        if current.get("generation_key") != generation_key:
            return False
        if lease_token is not None and current.get("lease_token") != lease_token:
            return False
        if expected_artwork is not None and current != expected_artwork:
            return False
        current.update({"status": "unavailable", "failure_code": failure_code})
        current.pop("lease_token", None)
        return True

    def list_pending_jobs(self, *, limit, now):
        pending = []
        for (uid, memory_id, generation_key), job in self.jobs.items():
            if job.get("status") == "processing":
                lease_expires_at = job.get("lease_expires_at")
                if not isinstance(lease_expires_at, datetime) or lease_expires_at > now:
                    continue
            elif job.get("status") == "pending":
                available_at = job.get("available_at")
                if isinstance(available_at, datetime) and available_at > now:
                    continue
            else:
                continue
            pending.append(copy.deepcopy(job))
        return pending[:limit]

    def claim_job(self, uid, memory_id, generation_key, *, lease_token, now, lease_seconds):
        if uid in self.deletion_pending:
            return None
        job = self.jobs.get((uid, memory_id, generation_key))
        if job is None:
            return None
        if job.get("status") == "processing":
            lease_expires_at = job.get("lease_expires_at")
            if not isinstance(lease_expires_at, datetime) or lease_expires_at > now:
                return None
        elif job.get("status") != "pending":
            return None
        job.update(
            {
                "status": "processing",
                "lease_token": lease_token,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
            }
        )
        return copy.deepcopy(job)

    def _finish_job(self, uid, memory_id, generation_key, lease_token, update):
        job = self.jobs.get((uid, memory_id, generation_key))
        if job is None or job.get("status") != "processing" or job.get("lease_token") != lease_token:
            return False
        job.update(update)
        job.pop("lease_token", None)
        job.pop("lease_expires_at", None)
        return True

    def job_claim_is_current(self, uid, memory_id, generation_key, *, lease_token, now=None):
        job = self.jobs.get((uid, memory_id, generation_key))
        current_time = now or datetime.now(timezone.utc)
        lease_expires_at = (job or {}).get("lease_expires_at")
        return bool(
            uid not in self.deletion_pending
            and job
            and job.get("status") == "processing"
            and job.get("lease_token") == lease_token
            and isinstance(lease_expires_at, datetime)
            and lease_expires_at > current_time
        )

    def complete_job(self, uid, memory_id, generation_key, *, lease_token):
        return self._finish_job(uid, memory_id, generation_key, lease_token, {"status": "completed"})

    def retry_job(
        self,
        uid,
        memory_id,
        generation_key,
        *,
        lease_token,
        attempt_count,
        delay_seconds,
        failure_code,
    ):
        return self._finish_job(
            uid,
            memory_id,
            generation_key,
            lease_token,
            {
                "status": "pending",
                "attempt_count": attempt_count,
                "available_at": datetime.now(timezone.utc),
                "failure_code": failure_code,
            },
        )

    def fail_job(self, uid, memory_id, generation_key, *, lease_token, failure_code):
        return self._finish_job(
            uid,
            memory_id,
            generation_key,
            lease_token,
            {"status": "failed", "failure_code": failure_code},
        )

    def mark_storage_cleanup_required(
        self,
        uid,
        memory_id,
        generation_key,
        *,
        generation_lease_token,
        job_lease_token,
    ):
        if uid in self.deletion_pending:
            return False
        conversation = self.get_conversation(uid, memory_id) or {}
        current_artwork = conversation.get("artwork") or {}
        if current_artwork.get("lease_token") != generation_lease_token:
            return False
        if not self.job_claim_is_current(uid, memory_id, generation_key, lease_token=job_lease_token):
            return False
        self.storage_cleanup_required.add(uid)
        return True


def _run_claimed_process(service, repository, uid="owner-a", memory_id="memory-1"):
    conversation = repository.get_conversation(uid, memory_id) or {}
    generation_key = str(((conversation.get("artwork") or {}).get("generation_key") or ""))
    lease_token = "test-job-lease"
    claimed = repository.claim_job(
        uid,
        memory_id,
        generation_key,
        lease_token=lease_token,
        now=datetime.now(timezone.utc),
        lease_seconds=120,
    )
    assert claimed is not None
    return asyncio.run(
        service.process(
            uid,
            memory_id,
            generation_key=generation_key,
            job_lease_token=lease_token,
        )
    )


class FakeProvider:
    def __init__(self, *, failure=None, after_generate=None):
        self.failure = failure
        self.after_generate = after_generate
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        if self.after_generate:
            self.after_generate()
        return artwork.GeneratedArtwork(
            image_bytes=b"private-image",
            content_type="image/png",
            pixel_width=1536,
            pixel_height=1024,
        )


class FakeStore:
    def __init__(self):
        self.puts = []
        self.deletes = []
        self.prefix_deletes = []
        self.signed = []

    def put(self, **kwargs):
        self.puts.append(kwargs)
        return artwork.StoredArtwork(
            object_key=f"users/owner/profiles/profile/memories/{kwargs['memory_id']}/object.png",
            object_generation="7",
            content_type=kwargs["content_type"],
            byte_size=len(kwargs["image_bytes"]),
        )

    def delete(self, **kwargs):
        self.deletes.append(kwargs)

    def delete_memory_prefix(self, **kwargs):
        self.prefix_deletes.append(kwargs)
        return 0

    def signed_get_url(self, **kwargs):
        self.signed.append(kwargs)
        return "https://first-party.invalid/signed"


def _authority(uid="owner-a", digest="digest-a"):
    return artwork.ArtworkRuntimeAuthority(
        uid=uid,
        binding_id=f"binding-{uid}",
        profile_id=f"profile-{uid}",
        authority_digest=digest,
    )


def _enabled_config(*, backfill=True):
    return artwork.MemoryArtworkConfig(
        enabled=True,
        release_enabled=True,
        provider_enabled=True,
        backfill_enabled=backfill,
    )


async def _resolver(uid):
    return _authority(uid)


def _accepted_preferences(authority):
    return {
        "consent": "accepted",
        "consent_version": artwork.ARTWORK_CONSENT_VERSION,
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "binding_id": authority.binding_id,
        "profile_id": authority.profile_id,
        "authority_digest": authority.authority_digest,
    }


def test_disabled_and_declined_states_never_call_provider():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    provider = FakeProvider()
    disabled = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        config=artwork.MemoryArtworkConfig(False, False, False, False),
    )
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    assert asyncio.run(disabled.enqueue("owner-a", "memory-1"))["outcome"] == "disabled"
    repository.preferences_by_uid["owner-a"] = {"consent": "declined"}
    assert asyncio.run(disabled.enqueue("owner-a", "memory-1"))["outcome"] == "declined"
    assert provider.calls == 0


def test_idempotent_generation_and_owner_scoped_signed_url():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    first = asyncio.run(service.enqueue("owner-a", "memory-1"))
    second = asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert first["outcome"] == "reserved"
    assert second["outcome"] == "existing"
    assert repository.reserve_writes == 1
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    signed = asyncio.run(service.signed_url("owner-a", "memory-1"))
    assert signed["status"] == "ready"
    assert signed["url"].startswith("https://first-party.invalid/")
    assert provider.calls == 1
    assert len(store.puts) == 1
    assert repository.storage_cleanup_required == {"owner-a"}

    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-b", "memory-1"))
    assert missing.value.code == "memory_artwork_memory_not_found"
    assert len(store.signed) == 1


def test_objectless_ready_artwork_is_atomically_rereserved_for_generation():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    assert asyncio.run(service.enqueue("owner-a", "memory-1"))["outcome"] == "reserved"
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    conversation = repository.conversations[("owner-a", "memory-1")]
    generation_key = conversation["artwork"]["generation_key"]
    conversation["artwork"].pop("object_key")

    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-a", "memory-1"))
    assert missing.value.code == "memory_artwork_object_missing"

    retry = asyncio.run(service.enqueue("owner-a", "memory-1"))

    assert retry == {"outcome": "reserved", "status": "generating"}
    assert conversation["artwork"]["generation_key"] == generation_key
    assert conversation["artwork"]["status"] == "generating"
    assert repository.jobs[("owner-a", "memory-1", generation_key)]["status"] == "pending"
    assert repository.reserve_writes == 2


def test_gcs_signed_url_rejects_a_missing_object_before_signing():
    from utils.ella import memory_artwork_storage

    class MissingBlob:
        exists_calls = 0

        def exists(self):
            self.exists_calls += 1
            return False

        def generate_signed_url(self, **kwargs):
            raise AssertionError("a missing object must never receive a signed URL")

    missing_blob = MissingBlob()

    class Bucket:
        @staticmethod
        def blob(object_key):
            assert object_key.endswith(".png")
            return missing_blob

    class Client:
        @staticmethod
        def bucket(bucket_name):
            assert bucket_name == "private-artwork"
            return Bucket()

    object_key = memory_artwork_storage.object_key_for(
        uid="owner-a",
        profile_binding_id="binding-owner-a",
        memory_id="memory-1",
        generation_key="a" * 64,
        content_type="image/png",
    )
    store = memory_artwork_storage.GCSMemoryArtworkStore(bucket_name="private-artwork", client=Client())

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as missing:
        store.signed_get_url(uid="owner-a", memory_id="memory-1", object_key=object_key)

    assert str(missing.value) == "memory_artwork_object_missing"
    assert missing_blob.exists_calls == 1


def test_missing_ready_blob_is_demoted_and_can_be_rereserved():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    initial_store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=lambda: initial_store,
        config=_enabled_config(),
    )

    assert asyncio.run(service.enqueue("owner-a", "memory-1"))["outcome"] == "reserved"
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}

    class MissingObjectStore(FakeStore):
        def signed_get_url(self, **kwargs):
            self.signed.append(kwargs)
            raise artwork.MemoryArtworkStorageError("memory_artwork_object_missing")

    service.store_factory = MissingObjectStore
    conversation = repository.conversations[("owner-a", "memory-1")]
    generation_key = conversation["artwork"]["generation_key"]

    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert missing.value.code == "memory_artwork_object_missing"
    assert conversation["artwork"]["status"] == "unavailable"
    assert conversation["artwork"]["failure_code"] == "memory_artwork_object_missing"

    retry = asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert retry == {"outcome": "reserved", "status": "generating"}
    assert conversation["artwork"]["generation_key"] == generation_key
    assert repository.jobs[("owner-a", "memory-1", generation_key)]["status"] == "pending"


def test_stale_missing_blob_read_cannot_demote_a_new_same_key_generation_claim():
    class InterleavingRepository(FakeRepository):
        def mark_generation_unavailable(
            self,
            uid,
            memory_id,
            *,
            generation_key,
            failure_code,
            lease_token=None,
            expected_artwork,
        ):
            assert expected_artwork["status"] == "ready"
            assert expected_artwork["object_key"]
            assert expected_artwork["authority_digest"]
            assert expected_artwork["binding_id"]
            assert expected_artwork["profile_id"]
            current = self.conversations[(uid, memory_id)]["artwork"]
            assert current["generation_key"] == generation_key
            current.update(
                {
                    "status": "generating",
                    "lease_token": "new-generation-lease",
                    "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=120),
                }
            )
            current.pop("object_key", None)
            return super().mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code=failure_code,
                lease_token=lease_token,
                expected_artwork=expected_artwork,
            )

    repository = InterleavingRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    assert asyncio.run(service.enqueue("owner-a", "memory-1"))["outcome"] == "reserved"
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}

    class MissingObjectStore(FakeStore):
        def signed_get_url(self, **kwargs):
            raise artwork.MemoryArtworkStorageError("memory_artwork_object_missing")

    service.store_factory = MissingObjectStore
    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert missing.value.code == "memory_artwork_object_missing"
    current = repository.conversations[("owner-a", "memory-1")]["artwork"]
    assert current["status"] == "generating"
    assert current["lease_token"] == "new-generation-lease"
    assert "failure_code" not in current


def test_process_requires_current_durable_job_claim_before_provider():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    generation_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"]

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(
            service.process(
                "owner-a",
                "memory-1",
                generation_key=generation_key,
                job_lease_token="not-a-current-claim",
            )
        )

    assert failure.value.code == "memory_artwork_job_claim_invalid"
    assert provider.calls == 0


@pytest.mark.parametrize("drift", ["source", "style", "consent"])
def test_stale_source_or_style_fails_before_provider_egress(drift):
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    if drift == "source":
        repository.conversations[("owner-a", "memory-1")]["active_summary_version_id"] = "corrected-version"
    elif drift == "style":
        repository.preferences_by_uid["owner-a"]["style_version"] = "ella.memory_artwork.style.paper-collage.v1"
    else:
        repository.preferences_by_uid["owner-a"]["consent"] = "declined"

    with pytest.raises(artwork.MemoryArtworkError):
        _run_claimed_process(service, repository)
    assert provider.calls == 0
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["status"] == "unavailable"

    if drift == "source":
        discarded_repository = FakeRepository()
        discarded_repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
        discarded_repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
        discarded_provider = FakeProvider()
        discarded_service = artwork.MemoryArtworkService(
            repository=discarded_repository,
            authority_resolver=_resolver,
            provider_factory=lambda: discarded_provider,
            store_factory=FakeStore,
            config=_enabled_config(),
        )
        asyncio.run(discarded_service.enqueue("owner-a", "memory-1"))
        discarded_repository.conversations[("owner-a", "memory-1")]["discarded"] = True
        with pytest.raises(artwork.MemoryArtworkError) as discarded_failure:
            _run_claimed_process(discarded_service, discarded_repository)
        assert discarded_failure.value.code in {
            "memory_artwork_job_claim_invalid",
            "memory_artwork_source_changed",
        }
        assert discarded_provider.calls == 0


def test_global_consent_revocation_at_final_egress_check_blocks_provider():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    consent_checks = iter((True, True, False))
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        global_consent_checker=lambda uid: next(consent_checks),
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_authority_changed"
    assert provider.calls == 0


def test_global_consent_revocation_blocks_signed_url():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["artwork"] = {
        "status": "ready",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "object_key": "private/object/key",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        store_factory=lambda: store,
        global_consent_checker=lambda uid: False,
        config=_enabled_config(),
    )

    result = asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert result["status"] == "unavailable"
    assert result["failure_code"] == "memory_artwork_consent_required"
    assert store.signed == []


def test_provider_failure_is_typed_and_does_not_write_object():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider(failure=RuntimeError("provider detail must not escape"))
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)
    assert failure.value.code == "memory_artwork_provider_failed"
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["failure_code"] == failure.value.code
    assert store.puts == []


def test_failed_storage_write_remains_covered_by_cleanup_marker():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    class FailingStore(FakeStore):
        def put(self, **kwargs):
            raise artwork.MemoryArtworkStorageError("memory_artwork_storage_failed")

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FailingStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_storage_failed"
    assert repository.storage_cleanup_required == {"owner-a"}


def test_cleanup_marker_failure_prevents_object_upload():
    class FailingMarkerRepository(FakeRepository):
        def mark_storage_cleanup_required(
            self,
            uid,
            memory_id,
            generation_key,
            *,
            generation_lease_token,
            job_lease_token,
        ):
            return False

    repository = FailingMarkerRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_deletion_pending"
    assert store.puts == []
    assert store.deletes == []


def test_non_landscape_provider_output_is_rejected_before_object_write():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    class WrongDimensionsProvider(FakeProvider):
        async def generate(self, **kwargs):
            generated = await super().generate(**kwargs)
            return artwork.GeneratedArtwork(
                image_bytes=generated.image_bytes,
                content_type=generated.content_type,
                pixel_width=1024,
                pixel_height=1024,
            )

    provider = WrongDimensionsProvider()
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)
    assert failure.value.code == "memory_artwork_dimensions_invalid"
    assert store.puts == []


def test_authority_drift_after_provider_output_prevents_object_write():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    calls = 0

    async def drifting_resolver(uid):
        nonlocal calls
        calls += 1
        return _authority(uid, "digest-b" if calls >= 4 else "digest-a")

    provider = FakeProvider()
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=drifting_resolver,
        provider_factory=lambda: provider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)
    assert failure.value.code == "memory_artwork_authority_changed"
    assert provider.calls == 1
    assert store.puts == []


def test_expired_job_claim_immediately_before_provider_has_zero_egress():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    resolver_calls = 0

    async def expiring_resolver(uid):
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls == 2:
            next(iter(repository.jobs.values()))["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        return _authority(uid)

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=expiring_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_job_claim_invalid"
    assert provider.calls == 0


def test_expired_job_claim_after_provider_prevents_storage_egress():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()

    def expire_job_claim():
        next(iter(repository.jobs.values()))["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    provider = FakeProvider(after_generate=expire_job_claim)
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_job_claim_invalid"
    assert provider.calls == 1
    assert store.puts == []


def test_sensitive_source_is_excluded_without_provider_egress():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["ella_tags"] = ["caregiver-private"]
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        config=_enabled_config(),
    )

    result = asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert result["outcome"] == "sensitive_source_excluded"
    assert provider.calls == 0
    assert repository.reserve_writes == 0


def test_sensitive_classification_drift_at_egress_recheck_blocks_provider():
    class DriftingRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.process_reads = 0

        def get_conversation(self, uid, memory_id):
            self.process_reads += 1
            if self.process_reads == 4:
                self.conversations[(uid, memory_id)]["internal_assessment"] = {"risk_level": "high"}
            return super().get_conversation(uid, memory_id)

    repository = DriftingRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        _run_claimed_process(service, repository)

    assert failure.value.code == "memory_artwork_sensitive_source_excluded"
    assert provider.calls == 0
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["failure_code"] == ("sensitive_source_excluded")


@pytest.mark.parametrize(
    ("preference_change", "expected"),
    [
        ({"consent": "not_set"}, "unavailable"),
        ({"consent_version": "stale"}, "unavailable"),
        ({"style_version": "ella.memory_artwork.style.paper-collage.v1"}, "stale"),
        ({"binding_id": "other-binding"}, "stale"),
        ({"profile_id": "other-profile"}, "stale"),
        ({"authority_digest": "other-digest"}, "stale"),
    ],
)
def test_signed_url_requires_current_bound_consent(preference_change, expected):
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["artwork"] = {
        "status": "ready",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "object_key": "private/object/key",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    preferences = _accepted_preferences(_authority())
    preferences.update(preference_change)
    repository.preferences_by_uid["owner-a"] = preferences
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    if expected == "unavailable":
        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        assert result == {
            "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
            "status": "unavailable",
            "failure_code": "memory_artwork_consent_required",
        }
    else:
        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        assert result == {
            "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
            "status": "unavailable",
            "failure_code": "memory_artwork_preference_authority_stale",
        }
    assert store.signed == []


def test_signed_url_rechecks_sensitive_source_before_release():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["ella_tags"] = ["caregiver-private"]
    memory["artwork"] = {
        "status": "ready",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "object_key": "private/object/key",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    result = asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert result == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "unavailable",
        "failure_code": "memory_artwork_sensitive_source_excluded",
    }
    assert store.signed == []

    for drift in (
        "enrichment_revision",
        "prompt",
        "missing_enrichment_revision",
        "missing_prompt_sha256",
        "missing_style_version",
        "discarded",
    ):
        repository = FakeRepository()
        repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
        repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
        store = FakeStore()
        service = artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=FakeProvider,
            store_factory=lambda: store,
            config=_enabled_config(),
        )
        asyncio.run(service.enqueue("owner-a", "memory-1"))
        assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}

        conversation = repository.conversations[("owner-a", "memory-1")]
        if drift == "enrichment_revision":
            conversation["active_summary_version_id"] = "summary-corrected"
        elif drift == "prompt":
            conversation["structured"]["title"] = "A corrected memory title"
        elif drift == "missing_enrichment_revision":
            conversation["artwork"].pop("enrichment_revision")
        elif drift == "missing_prompt_sha256":
            conversation["artwork"].pop("prompt_sha256")
        elif drift == "missing_style_version":
            conversation["artwork"].pop("style_version")
        else:
            conversation["discarded"] = True

        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        expected_failure_code = "memory_artwork_source_stale"
        if drift == "discarded":
            expected_failure_code = "memory_artwork_discarded"
        elif drift == "missing_style_version":
            expected_failure_code = "memory_artwork_preference_authority_stale"
        assert result == {
            "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
            "status": "unavailable",
            "failure_code": expected_failure_code,
        }
        assert store.signed == []


def test_signed_url_rereads_consent_after_awaited_authority_resolution():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["artwork"] = {
        "status": "ready",
        "generation_key": "a" * 64,
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "object_key": "private/object/key",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()

    async def declining_resolver(uid):
        await asyncio.sleep(0)
        repository.preferences_by_uid[uid]["consent"] = "declined"
        return _authority(uid)

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=declining_resolver,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    assert asyncio.run(service.signed_url("owner-a", "memory-1")) == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "declined",
    }
    assert store.signed == []


def test_release_off_is_a_signed_url_kill_switch():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["artwork"] = {
        "status": "ready",
        "generation_key": "a" * 64,
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "object_key": "private/object/key",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()

    async def never_resolve(uid):
        raise AssertionError("release-off must stop before authority or storage work")

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=never_resolve,
        store_factory=lambda: store,
        config=artwork.MemoryArtworkConfig(True, False, True, True),
    )

    assert asyncio.run(service.signed_url("owner-a", "memory-1")) == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "unavailable",
        "failure_code": "memory_artwork_release_disabled",
    }
    assert store.signed == []


def test_backfill_is_bounded_to_newest_ten_and_retry_is_idempotent(monkeypatch):
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index in range(14):
        created_at = datetime(2026, 8, 22, 12, index, tzinfo=timezone.utc)
        memory_id = f"memory-{index:02d}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(memory_id, created_at=created_at)
    repository.conversations[("owner-a", "memory-13")]["discarded"] = True
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        config=_enabled_config(),
    )

    first = asyncio.run(service.backfill("owner-a"))
    second = asyncio.run(service.backfill("owner-a"))
    assert first["queued"] == 10
    assert first["existing"] == 0
    assert second["queued"] == 0
    assert second["existing"] == 10
    assert first["memory_ids"] == [f"memory-{index:02d}" for index in range(12, 2, -1)]
    assert repository.reserve_writes == 10
    assert len(repository.jobs) == 10

    class Snapshot:
        def to_dict(self):
            return {"id": "memory-visible", "discarded": False}

    class Query:
        def __init__(self):
            self.operations = []

        def where(self, field, operator, value):
            self.operations.append(("where", field, operator, value))
            return self

        def order_by(self, field, direction):
            self.operations.append(("order_by", field, direction))
            return self

        def limit(self, value):
            self.operations.append(("limit", value))
            return self

        def stream(self):
            return iter([Snapshot()])

    query = Query()

    class Conversations:
        def collection(self, name):
            assert name == "conversations"
            return query

    class Users:
        def document(self, uid):
            assert uid == "owner-a"
            return Conversations()

    class Database:
        def collection(self, name):
            assert name == "users"
            return Users()

    monkeypatch.setattr(artwork_database, "db", Database())
    assert artwork_database.list_recent_conversations("owner-a", limit=10) == [
        {"id": "memory-visible", "discarded": False}
    ]
    assert query.operations == [
        ("where", "discarded", "==", False),
        ("order_by", "created_at", artwork_database.firestore.Query.DESCENDING),
        ("limit", 10),
    ]


def test_durable_worker_recovers_retryable_job_after_restart():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    failing_provider = FakeProvider(failure=artwork.MemoryArtworkError("provider_busy", retryable=True))

    def failing_service():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=lambda: failing_provider,
            store_factory=FakeStore,
            config=_enabled_config(),
        )

    service = failing_service()
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    job_key = next(iter(repository.jobs))
    first_worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=failing_service,
        config=_enabled_config(),
    )
    assert asyncio.run(first_worker.run_once()) == 1
    assert repository.jobs[job_key]["status"] == "pending"
    assert repository.jobs[job_key]["attempt_count"] == 1

    succeeding_provider = FakeProvider()
    store = FakeStore()

    def succeeding_service():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=lambda: succeeding_provider,
            store_factory=lambda: store,
            config=_enabled_config(),
        )

    restarted_worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=succeeding_service,
        config=_enabled_config(),
    )
    assert asyncio.run(restarted_worker.run_once()) == 1
    assert repository.jobs[job_key]["status"] == "completed"
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["status"] == "ready"
    assert succeeding_provider.calls == 1


def test_worker_reconciles_already_ready_job_without_second_provider_call():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = FakeProvider()

    def service_factory():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=lambda: provider,
            store_factory=FakeStore,
            config=_enabled_config(),
        )

    service = service_factory()
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    job_key = next(iter(repository.jobs))
    _run_claimed_process(service, repository)
    repository.jobs[job_key]["status"] = "pending"
    repository.jobs[job_key].pop("lease_token", None)
    repository.jobs[job_key].pop("lease_expires_at", None)
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=service_factory,
        config=_enabled_config(),
    )

    assert asyncio.run(worker.run_once()) == 1
    assert repository.jobs[job_key]["status"] == "completed"
    assert provider.calls == 1


def test_default_off_worker_does_not_read_or_process_jobs():
    class NeverReadRepository(FakeRepository):
        def list_pending_jobs(self, **kwargs):
            raise AssertionError("default-off worker must not inspect jobs")

    worker = artwork.MemoryArtworkWorker(
        repository=NeverReadRepository(),
        config=artwork.MemoryArtworkConfig(False, False, False, False),
    )
    assert asyncio.run(worker.run_once()) == 0


def test_account_deletion_marker_drains_claimed_worker_before_storage_write():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    entered = asyncio.Event()
    release = asyncio.Event()
    store = FakeStore()

    class HeldProvider(FakeProvider):
        async def generate(self, **kwargs):
            self.calls += 1
            entered.set()
            await release.wait()
            return artwork.GeneratedArtwork(
                image_bytes=b"private-image",
                content_type="image/png",
                pixel_width=1536,
                pixel_height=1024,
            )

    provider = HeldProvider()

    def service_factory():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=lambda: provider,
            store_factory=lambda: store,
            config=_enabled_config(),
        )

    async def scenario():
        await service_factory().enqueue("owner-a", "memory-1")
        worker = artwork.MemoryArtworkWorker(
            repository=repository,
            service_factory=service_factory,
            config=_enabled_config(),
        )
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        job = next(iter(repository.jobs.values()))
        assert job["status"] == "processing"
        repository.deletion_pending.add("owner-a")
        release.set()
        assert await task == 1

    asyncio.run(scenario())

    job = next(iter(repository.jobs.values()))
    assert job["status"] == "failed"
    assert job["failure_code"] == "memory_artwork_deletion_pending"
    assert store.puts == []
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["status"] == "unavailable"


def test_memory_deletion_marker_drains_claimed_worker_before_storage_write():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    entered = asyncio.Event()
    release = asyncio.Event()
    store = FakeStore()

    class HeldProvider(FakeProvider):
        async def generate(self, **kwargs):
            self.calls += 1
            entered.set()
            await release.wait()
            return artwork.GeneratedArtwork(
                image_bytes=b"private-image",
                content_type="image/png",
                pixel_width=1536,
                pixel_height=1024,
            )

    provider = HeldProvider()

    def service_factory():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=lambda: provider,
            store_factory=lambda: store,
            config=_enabled_config(),
        )

    async def scenario():
        await service_factory().enqueue("owner-a", "memory-1")
        worker = artwork.MemoryArtworkWorker(
            repository=repository,
            service_factory=service_factory,
            config=_enabled_config(),
        )
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        repository.conversations[("owner-a", "memory-1")]["deletion_pending"] = True
        release.set()
        assert await task == 1

    asyncio.run(scenario())

    assert provider.calls == 1
    assert store.puts == []
    job = next(iter(repository.jobs.values()))
    assert job["status"] == "pending"
    assert job["failure_code"] == "memory_artwork_job_claim_invalid"


def test_firestore_transaction_contract_rejects_source_and_lease_drift():
    class Snapshot:
        exists = True

        def __init__(self, state):
            self.state = state

        def to_dict(self):
            return copy.deepcopy(self.state)

    class Reference:
        def __init__(self, state):
            self.state = state

        def get(self, transaction=None):
            return Snapshot(self.state)

    class Transaction:
        def __init__(self):
            self.updates = 0

        def update(self, reference, payload):
            self.updates += 1
            for key, value in payload.items():
                reference.state[key] = copy.deepcopy(value)

    discarded_reference = Reference({**_terminal_memory("discarded-memory"), "discarded": True})
    discarded_transaction = Transaction()
    discarded_reservation = artwork_database._reserve_generation_transaction(
        discarded_transaction,
        Reference({"id": "owner-a"}),
        discarded_reference,
        enrichment_revision="summary-discarded-memory",
        generation_key="d" * 64,
        artwork_state={"status": "generating"},
    )
    assert discarded_reservation["outcome"] == "source_changed"
    assert discarded_transaction.updates == 0

    state = _terminal_memory("memory-1")
    user_reference = Reference({"id": "owner-a"})
    reference = Reference(state)
    transaction = Transaction()
    generation = {
        "status": "generating",
        "generation_key": "a" * 64,
        "authority_digest": "digest-a",
        "enrichment_revision": "summary-memory-1",
    }
    reserved = artwork_database._reserve_generation_transaction(
        transaction,
        user_reference,
        reference,
        enrichment_revision="summary-memory-1",
        generation_key="a" * 64,
        artwork_state=generation,
    )
    assert reserved["outcome"] == "reserved"
    assert transaction.updates == 1

    # A signed-URL read that observed the old ready object must not demote a
    # same-key generation that another request has already reserved or claimed.
    reference.state["artwork"] = {
        **generation,
        "status": "generating",
        "binding_id": "binding-a",
        "profile_id": "profile-a",
        "style_version": "soft-gouache-v1",
        "lease_token": "new-generation-lease",
    }
    observed_ready_artwork = {
        **generation,
        "status": "ready",
        "binding_id": "binding-a",
        "profile_id": "profile-a",
        "style_version": "soft-gouache-v1",
        "object_key": "users/owner-a/old-object.png",
    }
    updates_before_stale_demotion = transaction.updates
    assert (
        artwork_database._mark_generation_unavailable_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            failure_code="memory_artwork_object_missing",
            expected_artwork=observed_ready_artwork,
        )
        is False
    )
    assert transaction.updates == updates_before_stale_demotion
    assert reference.state["artwork"]["lease_token"] == "new-generation-lease"

    reference.state["discarded"] = True
    assert (
        artwork_database._claim_generation_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            lease_token="lease-a",
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        is None
    )
    reference.state["discarded"] = False
    claim = artwork_database._claim_generation_transaction(
        transaction,
        reference,
        generation_key="a" * 64,
        lease_token="lease-a",
        now=datetime.now(timezone.utc),
        lease_seconds=120,
    )
    assert claim["lease_token"] == "lease-a"
    reference.state["discarded"] = True
    assert (
        artwork_database._finalize_generation_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            authority_digest="digest-a",
            lease_token="lease-a",
            ready_state={"status": "ready"},
        )
        is False
    )
    job_reference = Reference(
        {
            "status": "processing",
            "generation_key": "a" * 64,
            "lease_token": "lease-a",
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
        }
    )
    assert (
        artwork_database._mark_storage_cleanup_required_transaction(
            transaction,
            user_reference,
            reference,
            job_reference,
            generation_key="a" * 64,
            generation_lease_token="lease-a",
            job_lease_token="lease-a",
        )
        is False
    )
    reference.state["discarded"] = False
    assert (
        artwork_database._finalize_generation_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            authority_digest="digest-a",
            lease_token="wrong-lease",
            ready_state={"status": "ready"},
        )
        is False
    )


def test_firestore_reservation_writes_generation_and_dispatch_in_one_transaction():
    class Snapshot:
        def __init__(self, state, *, exists=True):
            self.state = state
            self.exists = exists

        def to_dict(self):
            return copy.deepcopy(self.state)

    class Reference:
        def __init__(self, state=None):
            self.state = state or {}

        def get(self, transaction=None):
            return Snapshot(self.state, exists=bool(self.state))

    class Transaction:
        def __init__(self):
            self.operations = []

        def update(self, reference, payload):
            self.operations.append(("update", reference, copy.deepcopy(payload)))
            reference.state.update(copy.deepcopy(payload))

        def set(self, reference, payload):
            self.operations.append(("set", reference, copy.deepcopy(payload)))
            reference.state = copy.deepcopy(payload)

    user_ref = Reference({"id": "owner-a"})
    conversation_ref = Reference(_terminal_memory("memory-1"))
    job_ref = Reference()
    transaction = Transaction()
    artwork_state = {
        "status": "generating",
        "generation_key": "a" * 64,
        "enrichment_revision": "summary-memory-1",
    }
    job_state = {
        "uid": "owner-a",
        "memory_id": "memory-1",
        "generation_key": "a" * 64,
        "status": "pending",
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    result = artwork_database._reserve_generation_transaction(
        transaction,
        user_ref,
        conversation_ref,
        enrichment_revision="summary-memory-1",
        generation_key="a" * 64,
        artwork_state=artwork_state,
        job_ref=job_ref,
        job_state=job_state,
    )

    assert result["outcome"] == "reserved"
    assert [operation[0] for operation in transaction.operations] == ["update", "set"]
    assert conversation_ref.state["artwork"] == artwork_state
    assert job_ref.state == job_state


def test_firestore_reservation_replaces_objectless_ready_state_and_completed_job():
    class Snapshot:
        def __init__(self, state, *, exists=True):
            self.state = state
            self.exists = exists

        def to_dict(self):
            return copy.deepcopy(self.state)

    class Reference:
        def __init__(self, state=None):
            self.state = state or {}

        def get(self, transaction=None):
            return Snapshot(self.state, exists=bool(self.state))

    class Transaction:
        def __init__(self):
            self.operations = []

        def update(self, reference, payload):
            self.operations.append(("update", copy.deepcopy(payload)))
            reference.state.update(copy.deepcopy(payload))

        def set(self, reference, payload):
            self.operations.append(("set", copy.deepcopy(payload)))
            reference.state = copy.deepcopy(payload)

    generation_key = "a" * 64
    conversation = _terminal_memory("memory-1")
    conversation["artwork"] = {
        "status": "ready",
        "generation_key": generation_key,
        "enrichment_revision": "summary-memory-1",
        "object_key": "",
    }
    conversation_ref = Reference(conversation)
    job_ref = Reference({"status": "completed", "attempt_count": 1})
    generating = {
        "status": "generating",
        "generation_key": generation_key,
        "enrichment_revision": "summary-memory-1",
    }
    pending_job = {
        "status": "pending",
        "attempt_count": 0,
        "generation_key": generation_key,
        "created_at": datetime.now(timezone.utc),
    }
    transaction = Transaction()

    result = artwork_database._reserve_generation_transaction(
        transaction,
        Reference({"id": "owner-a"}),
        conversation_ref,
        enrichment_revision="summary-memory-1",
        generation_key=generation_key,
        artwork_state=generating,
        job_ref=job_ref,
        job_state=pending_job,
    )

    assert result == {"outcome": "reserved", "artwork": generating}
    assert conversation_ref.state["artwork"] == generating
    assert job_ref.state == pending_job
    assert [operation[0] for operation in transaction.operations] == ["update", "set"]


def test_firestore_retry_reservation_preserves_attempt_history():
    class Snapshot:
        def __init__(self, state):
            self.state = state
            self.exists = bool(state)

        def to_dict(self):
            return copy.deepcopy(self.state)

    class Reference:
        def __init__(self, state):
            self.state = state

        def get(self, transaction=None):
            return Snapshot(self.state)

    class Transaction:
        def update(self, reference, payload):
            reference.state.update(copy.deepcopy(payload))

        def set(self, reference, payload):
            reference.state = copy.deepcopy(payload)

    conversation = _terminal_memory("memory-1")
    conversation["artwork"] = {
        "status": "unavailable",
        "generation_key": "a" * 64,
        "enrichment_revision": "summary-memory-1",
    }
    user_ref = Reference({"id": "owner-a"})
    conversation_ref = Reference(conversation)
    original_created_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job_ref = Reference(
        {
            "status": "pending",
            "attempt_count": 3,
            "created_at": original_created_at,
        }
    )
    job_state = {
        "status": "pending",
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    result = artwork_database._reserve_generation_transaction(
        Transaction(),
        user_ref,
        conversation_ref,
        enrichment_revision="summary-memory-1",
        generation_key="a" * 64,
        artwork_state={
            "status": "generating",
            "generation_key": "a" * 64,
            "enrichment_revision": "summary-memory-1",
        },
        job_ref=job_ref,
        job_state=job_state,
        preserve_job_attempts=True,
    )

    assert result["outcome"] == "reserved"
    assert job_ref.state["attempt_count"] == 3
    assert job_ref.state["created_at"] == original_created_at

    conversation_ref.state["artwork"]["status"] = "unavailable"
    job_ref.state.update({"status": "failed", "attempt_count": 3})
    artwork_database._reserve_generation_transaction(
        Transaction(),
        user_ref,
        conversation_ref,
        enrichment_revision="summary-memory-1",
        generation_key="a" * 64,
        artwork_state={
            "status": "generating",
            "generation_key": "a" * 64,
            "enrichment_revision": "summary-memory-1",
        },
        job_ref=job_ref,
        job_state=job_state,
    )
    assert job_ref.state["attempt_count"] == 0


def test_pending_job_query_is_bounded_to_due_work_and_ordered(monkeypatch):
    now = datetime.now(timezone.utc)

    class Snapshot:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    class Query:
        def __init__(self, payloads):
            self.operations = []
            self.payloads = payloads

        def where(self, field, operator, value):
            self.operations.append(("where", field, operator, value))
            return self

        def order_by(self, field, direction):
            self.operations.append(("order_by", field, direction))
            return self

        def limit(self, value):
            self.operations.append(("limit", value))
            return self

        def stream(self):
            return iter(self.payloads)

    pending_query = Query(
        [
            Snapshot(
                "pending-first",
                {"status": "pending", "memory_id": "memory-1", "available_at": now - timedelta(minutes=2)},
            )
        ]
    )
    processing_query = Query(
        [
            Snapshot(
                "processing-first",
                {
                    "status": "processing",
                    "memory_id": "memory-2",
                    "lease_expires_at": now - timedelta(minutes=1),
                },
            )
        ]
    )

    class Database:
        def collection(self, name):
            assert name == artwork_database.JOB_COLLECTION
            return Collection()

    class Collection:
        def where(self, field, operator, value):
            query = pending_query if value == "pending" else processing_query
            return query.where(field, operator, value)

    monkeypatch.setattr(artwork_database, "db", Database())

    jobs = artwork_database.list_pending_jobs(limit=2, now=now)

    assert [job["job_id"] for job in jobs] == ["pending-first", "processing-first"]
    assert pending_query.operations == [
        ("where", "status", "==", "pending"),
        ("where", "available_at", "<=", now),
        ("order_by", "available_at", artwork_database.firestore.Query.ASCENDING),
        ("limit", 2),
    ]
    assert processing_query.operations == [
        ("where", "status", "==", "processing"),
        ("where", "lease_expires_at", "<=", now),
        ("order_by", "lease_expires_at", artwork_database.firestore.Query.ASCENDING),
        ("limit", 2),
    ]
    indexes = json.loads((BACKEND_ROOT.parent / "firestore.indexes.json").read_text())["indexes"]
    artwork_indexes = {
        tuple((field["fieldPath"], field["order"]) for field in index["fields"])
        for index in indexes
        if index.get("collectionGroup") == artwork_database.JOB_COLLECTION
    }
    assert (
        ("status", "ASCENDING"),
        ("available_at", "ASCENDING"),
    ) in artwork_indexes
    assert (
        ("status", "ASCENDING"),
        ("lease_expires_at", "ASCENDING"),
    ) in artwork_indexes


def test_processing_job_activity_requires_a_future_lease():
    now = datetime.now(timezone.utc)
    assert (
        artwork_database._processing_job_is_active(
            {"status": "processing", "lease_expires_at": now + timedelta(seconds=1)}, now=now
        )
        is True
    )
    assert (
        artwork_database._processing_job_is_active({"status": "processing", "lease_expires_at": now}, now=now) is False
    )
    assert artwork_database._processing_job_is_active({"status": "processing"}, now=now) is False


def test_conversation_deletion_marker_fences_inflight_artwork_finalize():
    class Snapshot:
        exists = True

        def __init__(self, state):
            self.state = state

        def to_dict(self):
            return copy.deepcopy(self.state)

    class Reference:
        def __init__(self, state):
            self.state = state

        def get(self, transaction=None):
            return Snapshot(self.state)

    class Transaction:
        def update(self, reference, payload):
            reference.state.update(copy.deepcopy(payload))

    state = _terminal_memory("memory-1")
    state["artwork"] = {
        "status": "generating",
        "generation_key": "a" * 64,
        "authority_digest": "digest-a",
        "enrichment_revision": "summary-memory-1",
        "lease_token": "lease-a",
    }
    reference = Reference(state)
    transaction = Transaction()

    claimed = artwork_database._claim_deletion_transaction(transaction, reference)

    assert claimed["deletion_pending"] is True
    assert state["deletion_pending"] is True
    assert (
        artwork_database._finalize_generation_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            authority_digest="digest-a",
            lease_token="lease-a",
            ready_state={"status": "ready"},
        )
        is False
    )
    state["active_summary_version_id"] = "corrected-version"
    assert (
        artwork_database._finalize_generation_transaction(
            transaction,
            reference,
            generation_key="a" * 64,
            authority_digest="digest-a",
            lease_token="lease-a",
            ready_state={"status": "ready"},
        )
        is False
    )


def test_public_conversation_model_omits_private_object_metadata():
    from models.conversation import Conversation

    payload = _terminal_memory("memory-1")
    payload.update(
        {
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "finished_at": None,
            "artwork": {
                "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
                "status": "ready",
                "style_version": artwork.DEFAULT_STYLE_VERSION,
                "enrichment_revision": "summary-memory-1",
                "object_key": "private/object/key",
                "authority_digest": "private-authority",
            },
        }
    )
    serialized = Conversation(**payload).model_dump(mode="json")
    assert serialized["artwork"]["status"] == "ready"
    assert "object_key" not in serialized["artwork"]
    assert "authority_digest" not in serialized["artwork"]


def test_storage_owner_validation_and_production_deletion_hooks(monkeypatch):
    from utils.ella import memory_artwork_storage

    object_key = memory_artwork_storage.object_key_for(
        uid="owner-a",
        profile_binding_id="binding-owner-a",
        memory_id="memory-1",
        generation_key="a" * 64,
        content_type="image/png",
    )
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as mismatch:
        memory_artwork_storage._validated_owner_key("owner-b", "memory-1", object_key)
    assert str(mismatch.value) == "memory_artwork_object_owner_mismatch"

    deleted = []
    prefix_deleted = []
    real_store_class = memory_artwork_storage.GCSMemoryArtworkStore

    class RecordingStore:
        def delete(self, **kwargs):
            deleted.append(kwargs)

        def delete_memory_prefix(self, **kwargs):
            prefix_deleted.append(kwargs)
            return 1

    monkeypatch.setattr(memory_artwork_storage, "GCSMemoryArtworkStore", RecordingStore)
    memory_artwork_storage.delete_conversation_artwork_if_present(
        "owner-a",
        "memory-1",
        {"artwork": {"object_key": object_key, "binding_id": "binding-owner-a"}},
    )
    assert deleted == [{"uid": "owner-a", "memory_id": "memory-1", "object_key": object_key}]
    assert prefix_deleted == [
        {
            "uid": "owner-a",
            "memory_id": "memory-1",
            "profile_binding_id": "binding-owner-a",
        }
    ]

    memory_artwork_storage.delete_conversation_artwork_if_present(
        "owner-a",
        "memory-2",
        {"artwork": {"status": "generating", "binding_id": "binding-owner-a"}},
    )
    assert prefix_deleted[-1] == {
        "uid": "owner-a",
        "memory_id": "memory-2",
        "profile_binding_id": "binding-owner-a",
    }

    real_store = object.__new__(real_store_class)
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as unbound:
        real_store.delete_memory_prefix(uid="owner-a", memory_id="memory-3")
    assert str(unbound.value) == "memory_artwork_binding_required"

    conversations_source = (BACKEND_ROOT / "database" / "conversations.py").read_text(encoding="utf-8")
    account_route_source = (BACKEND_ROOT / "routers" / "users.py").read_text(encoding="utf-8")
    delete_start = conversations_source.index("def delete_conversation(uid, conversation_id)")
    assert conversations_source.index("has_processing_jobs_for_memory", delete_start) < conversations_source.index(
        "delete_conversation_artwork_if_present", delete_start
    )
    assert conversations_source.index("delete_conversation_artwork_if_present") < conversations_source.index(
        "delete_jobs_for_memory", delete_start
    )
    assert conversations_source.index("delete_jobs_for_memory", delete_start) < conversations_source.index(
        "conversation_ref.delete()", delete_start
    )
    assert account_route_source.index("prepare_account_artwork_deletion(uid)") < account_route_source.index(
        "delete_user_data(uid)", account_route_source.index("def delete_account")
    )


def test_account_artwork_cleanup_fails_closed_only_when_storage_was_touched(monkeypatch):
    from utils.ella import memory_artwork_storage

    monkeypatch.delenv("ELLA_MEMORY_ARTWORK_BUCKET", raising=False)
    assert memory_artwork_storage.delete_all_user_artwork("owner-a", cleanup_required=False) == 0
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.delete_all_user_artwork("owner-a", cleanup_required=True)
    assert str(failure.value) == "memory_artwork_storage_cleanup_unavailable"


def test_account_deletion_returns_before_cleanup_while_claimed_worker_is_active(monkeypatch):
    from utils.ella import memory_artwork_storage

    calls = []

    class Repository:
        @staticmethod
        def begin_account_deletion(uid):
            calls.append(("begin", uid))
            return True

        @staticmethod
        def has_processing_jobs(uid):
            calls.append(("processing", uid))
            return True

        @staticmethod
        def storage_cleanup_required(uid):
            raise AssertionError("storage cleanup must wait for the claimed worker")

        @staticmethod
        def delete_jobs_for_uid(uid):
            raise AssertionError("claimed worker job must remain until it reaches terminal state")

    monkeypatch.setattr(
        memory_artwork_storage,
        "delete_all_user_artwork",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("object cleanup must not race the worker")),
    )

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.prepare_account_artwork_deletion("owner-a", repository=Repository)

    assert str(failure.value) == "memory_artwork_worker_drain_pending"
    assert calls == [("begin", "owner-a"), ("processing", "owner-a")]


def test_account_deletion_stops_before_job_cleanup_when_storage_absence_is_unproven(monkeypatch):
    from utils.ella import memory_artwork_storage

    class Repository:
        jobs_deleted = False

        @staticmethod
        def begin_account_deletion(uid):
            return True

        @staticmethod
        def has_processing_jobs(uid):
            return False

        @staticmethod
        def storage_cleanup_required(uid):
            return True

        @classmethod
        def delete_jobs_for_uid(cls, uid):
            cls.jobs_deleted = True

    monkeypatch.delenv("ELLA_MEMORY_ARTWORK_BUCKET", raising=False)
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.prepare_account_artwork_deletion("owner-a", repository=Repository)
    assert str(failure.value) == "memory_artwork_storage_cleanup_unavailable"
    assert Repository.jobs_deleted is False

    account_route_source = (BACKEND_ROOT / "routers" / "users.py").read_text(encoding="utf-8")
    delete_start = account_route_source.index("def delete_account")
    assert account_route_source.index("except MemoryArtworkStorageError", delete_start) < account_route_source.index(
        "except Exception", delete_start
    )
    assert (
        'status_code=503'
        in account_route_source[delete_start : account_route_source.index("@router.patch", delete_start)]
    )


def test_mounted_route_rejects_unauthenticated_request_before_service_work(monkeypatch):
    service_module_name = "ella.services.memory_artwork"
    saved_service = sys.modules.get(service_module_name)
    sys.modules[service_module_name] = artwork
    router_name = "ella_memory_artwork_router_test_module"
    spec = importlib.util.spec_from_file_location(
        router_name,
        BACKEND_ROOT / "ella" / "routers" / "memory_artwork.py",
    )
    router_module = importlib.util.module_from_spec(spec)
    sys.modules[router_name] = router_module
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(router_module)
    finally:
        if saved_service is None:
            sys.modules.pop(service_module_name, None)
        else:
            sys.modules[service_module_name] = saved_service

    class NeverCalled:
        calls = 0

        async def signed_url(self, uid, memory_id):
            self.calls += 1
            return {"status": "ready"}

    fake = NeverCalled()
    monkeypatch.setattr(router_module, "MemoryArtworkService", lambda: fake)
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.get("/v1/ella/memories/memory-1/artwork")
    assert response.status_code == 401
    assert fake.calls == 0

    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    live_policy_service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        store_factory=FakeStore,
        global_consent_checker=lambda uid: True,
        config=_enabled_config(),
    )
    monkeypatch.setattr(router_module, "MemoryArtworkService", lambda: live_policy_service)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    available = client.get("/v1/ella/memories/memory-1/artwork")
    assert available.status_code == 200
    assert available.json() == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "unavailable",
        "failure_code": None,
    }

    repository.deletion_pending.add("owner-a")
    blocked = client.get("/v1/ella/memories/memory-1/artwork")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "memory_artwork_deletion_pending",
        "retryable": False,
    }
    assert repository.reserve_writes == 0


def test_mounted_internal_process_route_uses_durable_worker_job_claim(monkeypatch):
    service_module_name = "ella.services.memory_artwork"
    saved_service = sys.modules.get(service_module_name)
    sys.modules[service_module_name] = artwork
    router_name = "ella_memory_artwork_internal_router_test_module"
    spec = importlib.util.spec_from_file_location(
        router_name,
        BACKEND_ROOT / "ella" / "routers" / "memory_artwork.py",
    )
    router_module = importlib.util.module_from_spec(spec)
    sys.modules[router_name] = router_module
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(router_module)
    finally:
        if saved_service is None:
            sys.modules.pop(service_module_name, None)
        else:
            sys.modules[service_module_name] = saved_service

    calls = []

    class Repository:
        @staticmethod
        def get_conversation(uid, memory_id):
            calls.append(("read", uid, memory_id))
            return {"artwork": {"generation_key": "a" * 64}}

    class Worker:
        repository = Repository()

        async def run_job(self, uid, memory_id, generation_key, *, raise_errors):
            calls.append(("claim", uid, memory_id, generation_key, raise_errors))
            return {"outcome": "ready", "status": "ready"}

    class Authority:
        @staticmethod
        def require_uid(uid, *, feature):
            assert feature == "Memory artwork worker"
            return uid

    monkeypatch.setattr(router_module, "MemoryArtworkWorker", Worker)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.require_memory_artwork_service] = Authority
    client = TestClient(app)

    response = client.post("/v1/ella/internal/memory-artwork/memory-1/process?uid=owner-a")

    assert response.status_code == 200
    assert response.json() == {"outcome": "ready", "status": "ready"}
    assert calls == [
        ("read", "owner-a", "memory-1"),
        ("claim", "owner-a", "memory-1", "a" * 64, True),
    ]


def test_terminal_enrichment_hook_runs_idempotent_processor_without_raising(monkeypatch):
    calls = []

    class Service:
        async def enqueue(self, uid, memory_id):
            calls.append(("enqueue", uid, memory_id))
            return {"outcome": "reserved", "status": "generating"}

    monkeypatch.setattr(artwork, "MemoryArtworkService", Service)

    assert asyncio.run(artwork.enqueue_after_terminal_enrichment("owner-a", "memory-1")) is None
    assert calls == [("enqueue", "owner-a", "memory-1")]


def test_xai_provider_uses_fixed_base64_contract_and_normalizes_dimensions(monkeypatch):
    source = io.BytesIO()
    Image.new("RGB", (900, 600), color=(84, 132, 118)).save(source, format="PNG")
    encoded = base64.b64encode(source.getvalue()).decode("ascii")

    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.XaiMemoryArtworkProvider(client=client)
            return await provider.generate(
                prompt="A calm abstract garden with no text or identifiable people.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="a" * 64,
            )

    generated = asyncio.run(scenario())

    assert generated.content_type == "image/jpeg"
    assert (generated.pixel_width, generated.pixel_height) == (artwork.TARGET_WIDTH, artwork.TARGET_HEIGHT)
    with Image.open(io.BytesIO(generated.image_bytes)) as normalized:
        assert normalized.size == (artwork.TARGET_WIDTH, artwork.TARGET_HEIGHT)
        assert normalized.mode == "RGB"
    assert len(calls) == 1
    request = calls[0]
    payload = json.loads(request.content)
    assert str(request.url) == artwork.XAI_IMAGE_ENDPOINT
    assert payload["model"] == artwork.DEFAULT_XAI_IMAGE_MODEL
    assert payload["response_format"] == "b64_json"
    assert payload["aspect_ratio"] == "3:2"
    assert request.headers["Authorization"] == "Bearer test-only-key"

    monkeypatch.setattr(artwork, "_PIL_AVAILABLE", False)
    with pytest.raises(artwork.MemoryArtworkError) as missing_codec:
        artwork.XaiMemoryArtworkProvider._normalize_image(source.getvalue())
    assert missing_codec.value.code == "memory_artwork_image_codec_unavailable"


def test_xai_provider_rejects_vendor_url_only_response(monkeypatch):
    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")

    def handler(request):
        return httpx.Response(200, json={"data": [{"url": "https://vendor.invalid/temporary.jpg"}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.XaiMemoryArtworkProvider(client=client)
            return await provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="a" * 64,
            )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(scenario())

    assert failure.value.code == "memory_artwork_provider_response_invalid"


def test_xai_provider_rejects_oversized_response_before_buffering(monkeypatch):
    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")
    monkeypatch.setattr(artwork, "MAX_PROVIDER_RESPONSE_BYTES", 32)

    def handler(request):
        return httpx.Response(200, content=b"x" * 33, headers={"content-length": "33"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.XaiMemoryArtworkProvider(client=client)
            return await provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="a" * 64,
            )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(scenario())

    assert failure.value.code == "memory_artwork_provider_response_invalid"


def test_xai_provider_rejects_encoded_image_before_decode_allocation(monkeypatch):
    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")
    monkeypatch.setattr(artwork, "MAX_BASE64_ARTWORK_CHARS", 4)

    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": "AAAAAA=="}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.XaiMemoryArtworkProvider(client=client)
            return await provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="a" * 64,
            )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(scenario())

    assert failure.value.code == "memory_artwork_provider_response_invalid"


def test_xai_provider_selection_fails_closed_without_credential(monkeypatch):
    monkeypatch.setenv(artwork.PROVIDER_KIND_ENV, "xai")
    monkeypatch.delenv(artwork.XAI_API_KEY_ENV, raising=False)

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        artwork.memory_artwork_provider_factory()

    assert failure.value.code == "memory_artwork_provider_credential_unavailable"
