import asyncio
import copy
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        "mark_storage_cleanup_required",
        "claim_deletion",
    ):
        setattr(database_stub, name, lambda *args, **kwargs: None)
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

    def get_preferences(self, uid):
        return copy.deepcopy(self.preferences_by_uid.get(uid, {}))

    def set_preferences(self, uid, preferences):
        self.preferences_by_uid[uid] = copy.deepcopy(preferences)

    def mark_storage_cleanup_required(self, uid):
        self.preferences_by_uid.setdefault(uid, {})["storage_cleanup_required"] = True

    def get_conversation(self, uid, memory_id):
        value = self.conversations.get((uid, memory_id))
        return copy.deepcopy(value) if value is not None else None

    def list_recent_conversations(self, uid, *, limit):
        values = [copy.deepcopy(value) for (owner, _), value in self.conversations.items() if owner == uid]
        return sorted(values, key=lambda value: value["created_at"], reverse=True)[:limit]

    def reserve_generation(self, uid, memory_id, *, enrichment_revision, generation_key, artwork_state):
        conversation = self.conversations.get((uid, memory_id))
        if conversation is None:
            return {"outcome": "not_found"}
        if conversation.get("active_summary_version_id") != enrichment_revision:
            return {"outcome": "source_changed"}
        current = conversation.get("artwork") or {}
        if current.get("generation_key") == generation_key and current.get("status") in {"generating", "ready"}:
            return {"outcome": "existing", "artwork": copy.deepcopy(current)}
        conversation["artwork"] = copy.deepcopy(artwork_state)
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


def test_sensitive_assessment_drift_after_enqueue_fails_before_provider_egress():
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
    repository.conversations[("owner-a", "memory-1")]["internal_assessment"] = {"risk_level": "high"}

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(service.process("owner-a", "memory-1"))

    assert failure.value.code == "memory_artwork_sensitive_source_excluded"
    assert provider.calls == 0


def test_sensitive_assessment_drift_during_pre_egress_fence_fails_closed():
    class DriftingRepository(FakeRepository):
        def mark_storage_cleanup_required(self, uid):
            super().mark_storage_cleanup_required(uid)
            self.conversations[(uid, "memory-1")]["internal_assessment"] = {"risk_level": "high"}

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


@pytest.mark.parametrize(
    "preferences",
    [
        {},
        {"consent": "not_set"},
        {"consent": "accepted", "consent_version": "stale"},
    ],
)
def test_signed_url_requires_exact_current_consent(preferences):
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
    asyncio.run(service.process("owner-a", "memory-1"))
    repository.preferences_by_uid["owner-a"] = preferences

    result = asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert result["status"] == "unavailable"
    assert result["failure_code"] == "memory_artwork_consent_required"
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

    class RecordingStore:
        def delete_memory_prefix(self, **kwargs):
            deleted.append(kwargs)

    monkeypatch.setattr(memory_artwork_storage, "GCSMemoryArtworkStore", RecordingStore)
    memory_artwork_storage.delete_conversation_artwork_if_present(
        "owner-a",
        "memory-1",
        {"artwork": {"object_key": object_key}},
    )
    assert deleted == [{"uid": "owner-a", "memory_id": "memory-1"}]

    conversations_source = (BACKEND_ROOT / "database" / "conversations.py").read_text(encoding="utf-8")
    account_route_source = (BACKEND_ROOT / "routers" / "users.py").read_text(encoding="utf-8")
    assert conversations_source.index("delete_conversation_artwork_if_present") < conversations_source.index(
        "conversation_ref.delete()", conversations_source.index("def delete_conversation(uid, conversation_id)")
    )
    assert account_route_source.index("delete_all_user_artwork(uid, required=") < account_route_source.index(
        "delete_user_data(uid)", account_route_source.index("def delete_account")
    )


def test_storage_prefix_cleanup_removes_all_outcome_ambiguous_memory_objects():
    from utils.ella import memory_artwork_storage

    owner_hash = memory_artwork_storage._sha256("owner-a")
    other_owner_hash = memory_artwork_storage._sha256("owner-b")

    class Blob:
        def __init__(self, name):
            self.name = name
            self.deleted = False

        def delete(self):
            self.deleted = True

    blobs = [
        Blob(f"users/{owner_hash}/profiles/{'a' * 64}/memories/memory-1/{'1' * 64}.png"),
        Blob(f"users/{owner_hash}/profiles/{'b' * 64}/memories/memory-1/{'2' * 64}.webp"),
        Blob(f"users/{owner_hash}/profiles/{'a' * 64}/memories/memory-2/{'3' * 64}.png"),
        Blob(f"users/{other_owner_hash}/profiles/{'a' * 64}/memories/memory-1/{'4' * 64}.png"),
    ]

    class Bucket:
        def list_blobs(self, prefix):
            return [blob for blob in blobs if blob.name.startswith(prefix)]

    class Client:
        def bucket(self, name):
            assert name == "private-artwork"
            return Bucket()

    store = memory_artwork_storage.GCSMemoryArtworkStore(bucket_name="private-artwork", client=Client())

    assert store.delete_memory_prefix(uid="owner-a", memory_id="memory-1") == 2
    assert [blob.deleted for blob in blobs] == [True, True, False, False]


def test_required_account_cleanup_fails_closed_without_configured_bucket(monkeypatch):
    from utils.ella import memory_artwork_storage

    monkeypatch.delenv("ELLA_MEMORY_ARTWORK_BUCKET", raising=False)

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.delete_all_user_artwork("owner-a", required=True)

    assert str(failure.value) == "memory_artwork_storage_not_configured"


def test_processing_marks_cleanup_required_before_provider_call():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    class CheckingProvider(FakeProvider):
        async def generate(self, **kwargs):
            assert repository.preferences_by_uid["owner-a"]["storage_cleanup_required"] is True
            return await super().generate(**kwargs)

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=CheckingProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "memory-1"))

    assert asyncio.run(service.process("owner-a", "memory-1"))["status"] == "ready"


def test_preference_update_preserves_existing_storage_cleanup_requirement():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = {
        **_accepted_preferences(_authority()),
        "storage_cleanup_required": True,
    }
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        config=_enabled_config(),
    )

    asyncio.run(
        service.set_preferences(
            "owner-a",
            consent="declined",
            consent_version=artwork.ARTWORK_CONSENT_VERSION,
            style_version=artwork.DEFAULT_STYLE_VERSION,
        )
    )

    assert repository.preferences_by_uid["owner-a"]["consent"] == "declined"
    assert repository.preferences_by_uid["owner-a"]["storage_cleanup_required"] is True


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

        async def enqueue(self, uid, memory_id):
            self.calls += 1
            return {"outcome": "reserved", "status": "generating"}

    fake = NeverCalled()
    monkeypatch.setattr(router_module, "MemoryArtworkService", lambda: fake)
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.get("/v1/ella/memories/memory-1/artwork")
    assert response.status_code == 401
    assert fake.calls == 0

    drained = []

    async def drain(uid, memory_ids):
        drained.append((uid, memory_ids))

    monkeypatch.setattr(router_module, "process_queued_artwork", drain)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"
    response = client.post("/v1/ella/memories/memory-1/artwork")
    assert response.status_code == 200
    assert response.json()["processing_scheduled"] is True
    assert fake.calls == 1
    assert drained == [("owner-a", ["memory-1"])]


def test_terminal_enrichment_hook_runs_idempotent_processor_without_raising(monkeypatch):
    calls = []

    class Service:
        async def enqueue(self, uid, memory_id):
            calls.append(("enqueue", uid, memory_id))
            return {"outcome": "reserved", "status": "generating"}

        async def process(self, uid, memory_id):
            calls.append(("process", uid, memory_id))
            raise artwork.MemoryArtworkError("memory_artwork_provider_unavailable", retryable=True)

    monkeypatch.setattr(artwork, "MemoryArtworkService", Service)

    assert asyncio.run(artwork.enqueue_after_terminal_enrichment("owner-a", "memory-1")) is None
    assert calls == [
        ("enqueue", "owner-a", "memory-1"),
        ("process", "owner-a", "memory-1"),
    ]


def test_bounded_processor_continues_after_one_memory_fails(monkeypatch):
    calls = []

    class Service:
        async def process(self, uid, memory_id):
            calls.append((uid, memory_id))
            if memory_id == "memory-1":
                raise artwork.MemoryArtworkError("memory_artwork_provider_unavailable", retryable=True)
            return {"outcome": "ready", "status": "ready"}

    monkeypatch.setattr(artwork, "MemoryArtworkService", Service)

    asyncio.run(artwork.process_queued_artwork("owner-a", ["memory-1", "memory-2"]))

    assert calls == [("owner-a", "memory-1"), ("owner-a", "memory-2")]
