import asyncio
import base64
import copy
import importlib.util
import io
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
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
        "complete_job",
        "retry_job",
        "fail_job",
        "mark_storage_cleanup_required",
    ):
        setattr(database_stub, name, lambda *args, **kwargs: None)
    database_stub.STORAGE_CLEANUP_REQUIRED_FIELD = "memory_artwork_storage_cleanup_required"
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

    def get_preferences(self, uid):
        return copy.deepcopy(self.preferences_by_uid.get(uid, {}))

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
        if current.get("generation_key") == generation_key and current.get("status") in {"generating", "ready"}:
            if current.get("status") == "generating":
                self.jobs[job_key] = effective_job
            return {"outcome": "existing", "artwork": copy.deepcopy(current)}
        conversation["artwork"] = copy.deepcopy(artwork_state)
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
        current.update({"lease_token": lease_token, "lease_expires_at": now, "updated_at": now})
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
    ):
        conversation = self.conversations.get((uid, memory_id))
        current = (conversation or {}).get("artwork") or {}
        if current.get("generation_key") != generation_key:
            return False
        if lease_token is not None and current.get("lease_token") != lease_token:
            return False
        current.update({"status": "unavailable", "failure_code": failure_code})
        current.pop("lease_token", None)
        return True

    def list_pending_jobs(self, *, limit, now):
        pending = []
        for (uid, memory_id, generation_key), job in self.jobs.items():
            if job.get("status") != "pending":
                continue
            available_at = job.get("available_at")
            if isinstance(available_at, datetime) and available_at > now:
                continue
            pending.append(copy.deepcopy(job))
        return pending[:limit]

    def complete_job(self, uid, memory_id, generation_key):
        self.jobs[(uid, memory_id, generation_key)]["status"] = "completed"

    def retry_job(
        self,
        uid,
        memory_id,
        generation_key,
        *,
        attempt_count,
        delay_seconds,
        failure_code,
    ):
        job = self.jobs[(uid, memory_id, generation_key)]
        job.update(
            {
                "status": "pending",
                "attempt_count": attempt_count,
                "available_at": datetime.now(timezone.utc),
                "failure_code": failure_code,
            }
        )

    def fail_job(self, uid, memory_id, generation_key, *, failure_code):
        job = self.jobs[(uid, memory_id, generation_key)]
        job.update({"status": "failed", "failure_code": failure_code})

    def mark_storage_cleanup_required(self, uid):
        self.storage_cleanup_required.add(uid)


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
    assert asyncio.run(service.process("owner-a", "memory-1")) == {"outcome": "ready", "status": "ready"}
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
        asyncio.run(service.process("owner-a", "memory-1"))
    assert provider.calls == 0
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["status"] == "unavailable"


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
        asyncio.run(service.process("owner-a", "memory-1"))

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
        asyncio.run(service.process("owner-a", "memory-1"))
    assert failure.value.code == "memory_artwork_provider_failed"
    assert repository.conversations[("owner-a", "memory-1")]["artwork"]["failure_code"] == failure.value.code
    assert store.puts == []


def test_failed_storage_write_does_not_set_cleanup_latch():
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
        asyncio.run(service.process("owner-a", "memory-1"))

    assert failure.value.code == "memory_artwork_storage_failed"
    assert repository.storage_cleanup_required == set()


def test_cleanup_latch_failure_removes_uploaded_object():
    class FailingMarkerRepository(FakeRepository):
        def mark_storage_cleanup_required(self, uid):
            raise RuntimeError("database unavailable")

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
        asyncio.run(service.process("owner-a", "memory-1"))

    assert failure.value.code == "memory_artwork_storage_failed"
    assert len(store.puts) == 1
    assert store.deletes == [
        {
            "uid": "owner-a",
            "memory_id": "memory-1",
            "object_key": "users/owner/profiles/profile/memories/memory-1/object.png",
        }
    ]


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
        asyncio.run(service.process("owner-a", "memory-1"))
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
        asyncio.run(service.process("owner-a", "memory-1"))
    assert failure.value.code == "memory_artwork_authority_changed"
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
        asyncio.run(service.process("owner-a", "memory-1"))

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


def test_backfill_is_bounded_to_newest_ten_and_retry_is_idempotent():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index in range(14):
        created_at = datetime(2026, 8, 22, 12, index, tzinfo=timezone.utc)
        memory_id = f"memory-{index:02d}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(memory_id, created_at=created_at)
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
    assert first["memory_ids"] == [f"memory-{index:02d}" for index in range(13, 3, -1)]
    assert repository.reserve_writes == 10
    assert len(repository.jobs) == 10


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
    asyncio.run(service.process("owner-a", "memory-1"))
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

    state = _terminal_memory("memory-1")
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
        reference,
        enrichment_revision="summary-memory-1",
        generation_key="a" * 64,
        artwork_state=generation,
    )
    assert reserved["outcome"] == "reserved"
    assert transaction.updates == 1

    claim = artwork_database._claim_generation_transaction(
        transaction,
        reference,
        generation_key="a" * 64,
        lease_token="lease-a",
        now=datetime.now(timezone.utc),
        lease_seconds=120,
    )
    assert claim["lease_token"] == "lease-a"
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


def test_pending_job_query_is_bounded_and_ordered_by_availability(monkeypatch):
    now = datetime.now(timezone.utc)

    class Snapshot:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

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
            return iter(
                [
                    Snapshot("first", {"memory_id": "memory-1", "available_at": now}),
                    Snapshot("second", {"memory_id": "memory-2", "available_at": now}),
                ]
            )

    query = Query()

    class Database:
        def collection(self, name):
            assert name == artwork_database.JOB_COLLECTION
            return query

    monkeypatch.setattr(artwork_database, "db", Database())

    jobs = artwork_database.list_pending_jobs(limit=2, now=now)

    assert [job["job_id"] for job in jobs] == ["first", "second"]
    assert query.operations == [
        ("where", "status", "==", "pending"),
        ("where", "available_at", "<=", now),
        ("order_by", "available_at", artwork_database.firestore.Query.ASCENDING),
        ("limit", 2),
    ]


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
    assert prefix_deleted == []

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
    assert conversations_source.index("delete_conversation_artwork_if_present") < conversations_source.index(
        "conversation_ref.delete()", conversations_source.index("def delete_conversation(uid, conversation_id)")
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


def test_account_deletion_stops_before_job_cleanup_when_storage_absence_is_unproven(monkeypatch):
    from utils.ella import memory_artwork_storage

    class Repository:
        jobs_deleted = False

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

    class Response:
        status_code = 200
        content = b"bounded-json"

        def json(self):
            return {"data": [{"b64_json": encoded}]}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")
    client = Client()
    provider = artwork.XaiMemoryArtworkProvider(client=client)

    generated = asyncio.run(
        provider.generate(
            prompt="A calm abstract garden with no text or identifiable people.",
            style_version=artwork.DEFAULT_STYLE_VERSION,
            idempotency_key="a" * 64,
        )
    )

    assert generated.content_type == "image/jpeg"
    assert (generated.pixel_width, generated.pixel_height) == (artwork.TARGET_WIDTH, artwork.TARGET_HEIGHT)
    with Image.open(io.BytesIO(generated.image_bytes)) as normalized:
        assert normalized.size == (artwork.TARGET_WIDTH, artwork.TARGET_HEIGHT)
        assert normalized.mode == "RGB"
    assert len(client.calls) == 1
    url, request = client.calls[0]
    assert url == artwork.XAI_IMAGE_ENDPOINT
    assert request["json"]["model"] == artwork.DEFAULT_XAI_IMAGE_MODEL
    assert request["json"]["response_format"] == "b64_json"
    assert request["json"]["aspect_ratio"] == "3:2"
    assert request["headers"]["Authorization"] == "Bearer test-only-key"


def test_xai_provider_rejects_vendor_url_only_response(monkeypatch):
    class Response:
        status_code = 200
        content = b"bounded-json"

        def json(self):
            return {"data": [{"url": "https://vendor.invalid/temporary.jpg"}]}

    class Client:
        async def post(self, url, **kwargs):
            return Response()

    monkeypatch.setenv(artwork.XAI_API_KEY_ENV, "test-only-key")
    provider = artwork.XaiMemoryArtworkProvider(client=Client())

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(
            provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="a" * 64,
            )
        )

    assert failure.value.code == "memory_artwork_provider_response_invalid"


def test_xai_provider_selection_fails_closed_without_credential(monkeypatch):
    monkeypatch.setenv(artwork.PROVIDER_KIND_ENV, "xai")
    monkeypatch.delenv(artwork.XAI_API_KEY_ENV, raising=False)

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        artwork.memory_artwork_provider_factory()

    assert failure.value.code == "memory_artwork_provider_credential_unavailable"
