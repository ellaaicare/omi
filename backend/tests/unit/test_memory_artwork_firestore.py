import importlib.util
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted
from google.cloud import firestore


@pytest.mark.skipif(
    os.environ.get("ELLA_FIRESTORE_EMULATOR_TESTS") != "true",
    reason="requires the hosted Firestore emulator gate",
)
def test_real_firestore_style_only_cannot_overwrite_a_concurrent_decline(monkeypatch):
    """Inject a server abort, then prove real decline commit and SDK retry.

    This is deterministic retry evidence, not natural-contention evidence.
    Firestore's pessimistic read lock would otherwise block a competing write
    while a test waits for that write to commit before releasing the read.
    """
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
    control = {"state": "paused", "generation_id": "original-generation", "auto_continue": False}
    try:
        user_ref.set({module.PREFERENCES_FIELD: preferences, module.BACKFILL_CONTROL_FIELD: control})
        assert module.update_style(user_ref.id, **kwargs) == "updated"
        assert user_ref.get().to_dict()[module.BACKFILL_CONTROL_FIELD] == control
        stored = user_ref.get().to_dict()[module.PREFERENCES_FIELD]
        assert {key: stored[key] for key in preferences if key != "style_version"} == {
            key: value for key, value in preferences.items() if key != "style_version"
        }
        user_ref.set({module.PREFERENCES_FIELD: preferences, module.BACKFILL_CONTROL_FIELD: control})
        original = module._update_style_transaction
        commit = client._firestore_api.commit
        attempts = []
        aborted_commits = []

        def record_real_commit(**arguments):
            try:
                return commit(**arguments)
            except Aborted:
                aborted_commits.append(True)
                raise

        def update_after_injected_server_abort(transaction, reference, **arguments):
            observed = reference.get(transaction=transaction).to_dict()[module.PREFERENCES_FIELD]["consent"]
            outcome = original(transaction, reference, **arguments)
            attempts.append((observed, outcome))
            if len(attempts) == 1:
                assert (observed, outcome) == ("accepted", "updated")
                # Release the server-side read lock without cleaning the SDK's
                # transaction ID. Its subsequent real commit RPC must abort.
                client._firestore_api.rollback(
                    request={"database": client._database_string, "transaction": transaction.id}
                )
                reference.update({f"{module.PREFERENCES_FIELD}.consent": "declined"})
                assert reference.get().to_dict()[module.PREFERENCES_FIELD]["consent"] == "declined"
            return outcome

        monkeypatch.setattr(client._firestore_api, "commit", record_real_commit)
        monkeypatch.setattr(module, "_update_style_transaction", update_after_injected_server_abort)
        assert module.update_style(user_ref.id, **kwargs) == "consent_required"
        assert attempts == [("accepted", "updated"), ("declined", "consent_required")]
        assert aborted_commits == [True]
        stored = user_ref.get().to_dict()[module.PREFERENCES_FIELD]
        assert user_ref.get().to_dict()[module.BACKFILL_CONTROL_FIELD] == control
        assert stored == {**preferences, "consent": "declined"}
    finally:
        user_ref.delete()
        client.close()


@pytest.mark.skipif(
    os.environ.get("ELLA_FIRESTORE_EMULATOR_TESTS") != "true",
    reason="requires the hosted Firestore emulator gate",
)
def test_real_firestore_generation_and_dispatch_commit_and_repair_together(monkeypatch):
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
    user_ref.set(
        {
            "id": uid,
            module.PREFERENCES_FIELD: {
                "consent": "accepted",
                "consent_version": "ai-data-processors-v10",
                "style_version": "ella.memory_artwork.style.soft-gouache.v1",
                "binding_id": "fixture-binding",
                "profile_id": "fixture-profile",
                "authority_digest": "fixture-digest",
            },
        }
    )
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
