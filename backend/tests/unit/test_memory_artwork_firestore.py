import importlib.util
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


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
        assert module.job_claim_is_current(uid, memory_id, generation_key, lease_token=lease_token) is True
        assert (
            module.mark_storage_cleanup_required(
                uid,
                memory_id,
                generation_key,
                lease_token=lease_token,
            )
            is True
        )
        assert module.begin_account_deletion(uid) is True
        assert module.has_processing_jobs(uid) is True
        assert (
            module.mark_storage_cleanup_required(
                uid,
                memory_id,
                generation_key,
                lease_token=lease_token,
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
