import importlib.util
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest
from google.cloud import firestore


@pytest.mark.skipif(
    os.environ.get("ELLA_FIRESTORE_EMULATOR_TESTS") != "true",
    reason="requires the hosted Firestore emulator gate",
)
def test_real_firestore_style_only_cannot_overwrite_a_concurrent_decline(monkeypatch):
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "omi-ci"))
    monkeypatch.setitem(sys.modules, "database._client", SimpleNamespace(db=client))
    path = Path(__file__).resolve().parents[2] / "database" / "memory_artwork.py"
    spec = importlib.util.spec_from_file_location("database.memory_artwork_style_emulator_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    user_ref = client.collection("users").document(f"artwork-style-test-{uuid.uuid4()}")
    preferences = {
        "consent": "accepted",
        "consent_version": "test-version",
        "style_version": "original-style",
        "binding_id": "binding-a",
        "profile_id": "profile-a",
        "authority_digest": "digest-a",
        "receipt_id": "retained-receipt",
        "decided_at": "original-decision",
    }
    kwargs = dict(
        consent_version="test-version",
        style_version="new-style",
        binding_id="binding-a",
        profile_id="profile-a",
        authority_digest="digest-a",
        updated_at=datetime.now(timezone.utc),
    )
    try:
        user_ref.set({module.PREFERENCES_FIELD: preferences})
        assert module.update_style(user_ref.id, **kwargs) == "updated"
        stored = user_ref.get().to_dict()[module.PREFERENCES_FIELD]
        assert {key: stored[key] for key in preferences if key != "style_version"} == {
            key: value for key, value in preferences.items() if key != "style_version"
        }
        user_ref.set({module.PREFERENCES_FIELD: preferences})
        original = module._update_style_transaction
        read_completed = Event()
        decline_submitted = Event()

        def update_with_concurrent_decline(transaction, reference, **arguments):
            outcome = original(transaction, reference, **arguments)
            read_completed.set()
            assert decline_submitted.wait(5)
            return outcome

        def decline():
            assert read_completed.wait(5)
            decline_submitted.set()
            user_ref.update({f"{module.PREFERENCES_FIELD}.consent": "declined"})

        monkeypatch.setattr(module, "_update_style_transaction", update_with_concurrent_decline)
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending_decline = executor.submit(decline)
            pending_style = executor.submit(module.update_style, user_ref.id, **kwargs)
            assert pending_style.result(timeout=30) in {"updated", "consent_required"}
            pending_decline.result(timeout=30)
        stored = user_ref.get().to_dict()[module.PREFERENCES_FIELD]
        assert stored["consent"] == "declined"
        assert stored["style_version"] in {"original-style", "new-style"}
        assert {key: stored[key] for key in preferences if key not in {"consent", "style_version"}} == {
            key: value for key, value in preferences.items() if key not in {"consent", "style_version"}
        }
    finally:
        user_ref.delete()
        client.close()


@pytest.mark.skipif(
    os.environ.get("ELLA_FIRESTORE_EMULATOR_TESTS") != "true",
    reason="requires the hosted Firestore emulator gate",
)
def test_real_firestore_generation_and_dispatch_commit_and_repair_together(monkeypatch):
    from google.cloud import firestore

    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "omi-ci"))
    monkeypatch.setitem(sys.modules, "database._client", SimpleNamespace(db=client))
    path = Path(__file__).resolve().parents[2] / "database" / "memory_artwork.py"
    spec = importlib.util.spec_from_file_location("database.memory_artwork_emulator_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    uid = f"artwork-owner-{uuid.uuid4()}"
    memory_id = "memory-a"
    generation_key = "a" * 64
    user_ref = client.collection("users").document(uid)
    conversation_ref = user_ref.collection("conversations").document(memory_id)
    now = datetime.now(timezone.utc)
    user_ref.set({"id": uid})
    conversation_ref.set(
        {
            "id": memory_id,
            "status": "completed",
            "active_summary_version_id": "summary-a",
            "enrichment_state": {"status": "writeback_applied", "kind": "hermes_enriched"},
        }
    )
    artwork_state = {
        "status": "generating",
        "generation_key": generation_key,
        "enrichment_revision": "summary-a",
    }
    job_state = {
        "uid": uid,
        "memory_id": memory_id,
        "generation_key": generation_key,
        "status": "pending",
        "attempt_count": 0,
        "created_at": now,
        "updated_at": now,
        "available_at": now,
    }

    try:
        result = module.reserve_generation(
            uid,
            memory_id,
            enrichment_revision="summary-a",
            generation_key=generation_key,
            artwork_state=artwork_state,
            job_state=job_state,
        )
        assert result["outcome"] == "reserved"
        assert conversation_ref.get().to_dict()["artwork"] == artwork_state

        job_ref = module._job_ref(uid, memory_id, generation_key)
        assert job_ref.get().to_dict()["status"] == "pending"

        # A lost dispatch acknowledgement is repaired by replaying the same
        # deterministic reservation without changing the generation identity.
        job_ref.delete()
        replay = module.reserve_generation(
            uid,
            memory_id,
            enrichment_revision="summary-a",
            generation_key=generation_key,
            artwork_state=artwork_state,
            job_state=job_state,
        )
        assert replay["outcome"] == "existing"
        assert job_ref.get().to_dict()["generation_key"] == generation_key

        # A claimed worker is durable before provider work. Account deletion
        # marks the owner first, sees the processing job, and prevents any new
        # claim or reservation. Terminal acknowledgement never recreates a job
        # that cleanup has removed.
        lease_token = "lease-a"
        claimed = module.claim_job(
            uid,
            memory_id,
            generation_key,
            lease_token=lease_token,
            now=now,
            lease_seconds=120,
        )
        assert claimed["status"] == "processing"
        assert module.job_claim_is_current(uid, memory_id, generation_key, lease_token=lease_token, now=now) is True
        generation_lease_token = "generation-lease-a"
        generation_claim = module.claim_generation(
            uid,
            memory_id,
            generation_key=generation_key,
            lease_token=generation_lease_token,
            now=now,
            lease_seconds=120,
        )
        assert generation_claim is not None
        assert module.has_processing_jobs(uid, now=now) is True
        assert module.has_processing_jobs(uid, now=now + timedelta(seconds=121)) is False
        assert (
            module.mark_storage_cleanup_required(
                uid,
                memory_id,
                generation_key,
                generation_lease_token=generation_lease_token,
                job_lease_token=lease_token,
            )
            is True
        )
        assert module.begin_account_deletion(uid) is True
        assert module.has_processing_jobs(uid) is True
        assert module.job_claim_is_current(uid, memory_id, generation_key, lease_token=lease_token) is False
        assert (
            module.mark_storage_cleanup_required(
                uid,
                memory_id,
                generation_key,
                generation_lease_token=generation_lease_token,
                job_lease_token=lease_token,
            )
            is False
        )
        assert module.complete_job(uid, memory_id, generation_key, lease_token=lease_token) is True
        assert module.has_processing_jobs(uid) is False
        assert module.delete_jobs_for_uid(uid) == 1
        assert module.complete_job(uid, memory_id, generation_key, lease_token=lease_token) is False
        assert job_ref.get().exists is False
        blocked = module.reserve_generation(
            uid,
            memory_id,
            enrichment_revision="summary-a",
            generation_key=generation_key,
            artwork_state=artwork_state,
            job_state=job_state,
        )
        assert blocked["outcome"] == "deletion_pending"
        assert job_ref.get().exists is False
    finally:
        module._job_ref(uid, memory_id, generation_key).delete()
        conversation_ref.delete()
        user_ref.delete()
