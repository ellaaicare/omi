import asyncio
import base64
import copy
import gc
import hashlib
import importlib.util
import io
import json
import sys
import types
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(autouse=True)
def _current_global_ai_consent(monkeypatch):
    monkeypatch.setattr(artwork, "has_current_global_ai_consent", lambda uid: True)

    @asynccontextmanager
    async def publication_lock(uid):
        yield object()

    monkeypatch.setattr(artwork, "acquire_memory_artwork_publication_lock", publication_lock)


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _load_service_module():
    database_stub = types.ModuleType("database.memory_artwork")
    for name in (
        "get_preferences",
        "stabilize_preferences_authority",
        "set_preferences",
        "get_backfill_control",
        "set_backfill_control",
        "pause_observed_legacy_auto_continue_control",
        "list_jobs_for_uid",
        "get_job",
        "get_conversation",
        "list_conversations_page",
        "list_recent_conversations",
        "list_ready_artwork_conversations",
        "reserve_generation",
        "claim_generation",
        "finalize_generation",
        "clear_published_artwork",
        "restore_permanent_artwork",
        "attach_artwork_renditions",
        "mark_generation_unavailable",
        "claim_deletion",
        "list_pending_jobs",
        "claim_job",
        "job_claim_is_current",
        "complete_job",
        "retry_job",
        "fail_job",
        "mark_storage_cleanup_required",
        "renew_publication_claim",
        "create_reconciliation_job",
        "get_reconciliation_job",
        "list_pending_reconciliation_jobs",
        "claim_reconciliation_job",
        "finish_reconciliation_job",
        "storage_cleanup_required",
    ):
        setattr(database_stub, name, lambda *args, **kwargs: None)
    database_stub.STORAGE_CLEANUP_REQUIRED_FIELD = "memory_artwork_storage_cleanup_required"
    database_stub.DELETION_PENDING_FIELD = "memory_artwork_deletion_pending"
    database_stub.ARTWORK_FIELD = "artwork"
    database_stub.PUBLISHED_ARTWORK_FIELD = "published_artwork"
    database_stub.BACKFILL_CONTROL_FIELD = "memory_artwork_backfill_control"
    database_stub.DEFAULT_BACKFILL_BATCH_SIZE = 10
    database_stub.TERMINAL_ENRICHMENT_ORIGIN = "terminal_enrichment"
    database_stub.HISTORICAL_BACKFILL_ORIGIN = "historical_backfill"
    database_stub.PREVIEW_BACKFILL_ORIGIN = "preview_backfill"
    database_stub.reconciliation_job_id = lambda uid, authority_digest, style_version: hashlib.sha256(
        f"{uid}\0{authority_digest}\0{style_version}".encode("utf-8")
    ).hexdigest()
    database_stub._auto_continue_receipt_is_current = lambda control: bool(
        isinstance(control.get("auto_continue_receipt"), dict)
        and control["auto_continue_receipt"].get("schema_version") == "ella.memory_artwork.auto_continue.v1"
        and control["auto_continue_receipt"].get("generation_id") == control.get("generation_id")
        and control["auto_continue_receipt"].get("authority_digest") == control.get("authority_digest")
        and control["auto_continue_receipt"].get("style_version") == control.get("style_version")
    )
    ella_stub = types.ModuleType("ella")
    ella_stub.__path__ = []
    ella_services_stub = types.ModuleType("ella.services")
    ella_services_stub.__path__ = []
    ai_consent_stub = types.ModuleType("ella.services.ai_consent")
    ai_consent_stub.CURRENT_POLICY_VERSION = "ai-data-processors-v10"
    ai_consent_stub.get_ai_consent_service = lambda: None
    runtime_stub = types.ModuleType("ella.services.runtime_resolver")
    runtime_stub.resolve_isolated_runtime = lambda *args, **kwargs: None
    runtime_stub.runtime_authority_identity = lambda runtime: None
    saved = {
        name: sys.modules.get(name)
        for name in (
            "database.memory_artwork",
            "ella",
            "ella.services",
            "ella.services.ai_consent",
            "ella.services.runtime_resolver",
        )
    }
    sys.modules["database.memory_artwork"] = database_stub
    sys.modules["ella"] = ella_stub
    sys.modules["ella.services"] = ella_services_stub
    sys.modules["ella.services.ai_consent"] = ai_consent_stub
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
memory_artwork_storage = sys.modules["utils.ella.memory_artwork_storage"]


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


def _ready_artwork(memory: dict, *, authority, style_version: str) -> dict:
    _, prompt_sha256 = artwork._prompt_for(memory, style_version)
    return {
        "status": "ready",
        "style_version": style_version,
        "object_key": f"private/{memory['id']}.png",
        "authority_digest": authority.authority_digest,
        "binding_id": authority.binding_id,
        "profile_id": authority.profile_id,
        "enrichment_revision": memory["active_summary_version_id"],
        "prompt_sha256": prompt_sha256,
    }


class FakeRepository:
    def __init__(self):
        self.preferences_by_uid = {}
        self.conversations = {}
        self.reserve_writes = 0
        self.jobs = {}
        self.storage_cleanup_required_uids = set()
        self.deletion_pending = set()
        self.reconciliation_jobs = {}
        self.backfill_controls = {}
        self.job_list_migration_requests = []

    def get_preferences(self, uid):
        result = copy.deepcopy(self.preferences_by_uid.get(uid, {}))
        result[artwork.artwork_db.DELETION_PENDING_FIELD] = uid in self.deletion_pending
        return result

    def set_preferences(self, uid, preferences, *, backfill_control_state=None):
        self.preferences_by_uid[uid] = copy.deepcopy(preferences)
        if backfill_control_state is not None:
            generation_id = artwork.artwork_db.reconciliation_job_id(
                uid,
                preferences["authority_digest"],
                preferences["style_version"],
            )
            self.backfill_controls[uid] = {
                "schema_version": "ella.memory_artwork.queue_control.v1",
                "generation_id": generation_id,
                "authority_digest": preferences["authority_digest"],
                "style_version": preferences["style_version"],
                "state": backfill_control_state,
                "auto_continue": False,
                "batch_size": artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE,
                "batch_remaining": (
                    artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE if backfill_control_state == "running" else 0
                ),
                "pause_reason": "",
                "updated_at": datetime.now(timezone.utc),
            }

    def stabilize_preferences_authority(self, uid, *, binding_id, profile_id, authority_digest):
        preferences = self.get_preferences(uid)
        if (
            not preferences.get(artwork.artwork_db.DELETION_PENDING_FIELD)
            and preferences.get("binding_id") == binding_id
            and preferences.get("profile_id") == profile_id
            and preferences.get("authority_digest") != authority_digest
        ):
            preferences["authority_digest"] = authority_digest
            preferences["updated_at"] = datetime.now(timezone.utc)
            preferences.pop(artwork.artwork_db.DELETION_PENDING_FIELD, None)
            self.preferences_by_uid[uid] = copy.deepcopy(preferences)
        return preferences

    def get_backfill_control(self, uid):
        return copy.deepcopy(self.backfill_controls.get(uid, {}))

    def set_backfill_control(self, uid, *, expected_generation_id, state, auto_continue=False):
        preferences = self.preferences_by_uid.get(uid) or {}
        generation_id = artwork.artwork_db.reconciliation_job_id(
            uid,
            str(preferences.get("authority_digest") or ""),
            str(preferences.get("style_version") or ""),
        )
        if uid in self.deletion_pending:
            return {"outcome": "deletion_pending"}
        if generation_id != expected_generation_id:
            return {"outcome": "generation_stale"}
        control = {
            "schema_version": "ella.memory_artwork.queue_control.v1",
            "generation_id": generation_id,
            "authority_digest": preferences["authority_digest"],
            "style_version": preferences["style_version"],
            "state": state,
            "auto_continue": auto_continue if state == "running" else False,
            "batch_size": artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE,
            "batch_remaining": (
                0 if state != "running" or auto_continue else artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE
            ),
            "pause_reason": "user_paused" if state == "paused" else "user_cancelled" if state == "cancelled" else "",
            "updated_at": datetime.now(timezone.utc),
        }
        if state == "running" and auto_continue:
            control["auto_continue_receipt"] = {
                "schema_version": "ella.memory_artwork.auto_continue.v1",
                "generation_id": generation_id,
                "authority_digest": preferences["authority_digest"],
                "style_version": preferences["style_version"],
                "granted_at": control["updated_at"],
            }
        self.backfill_controls[uid] = control
        return {"outcome": "updated", "control": copy.deepcopy(control)}

    def pause_observed_legacy_auto_continue_control(
        self,
        uid,
        *,
        observed_control,
        expected_generation_id,
        authority_digest,
        style_version,
    ):
        current = self.backfill_controls.get(uid)
        if uid in self.deletion_pending:
            return {"outcome": "deletion_pending"}
        if not isinstance(current, dict):
            return {"outcome": "stale", "control": {}}
        if not (
            bool(current.get("auto_continue"))
            and not artwork.artwork_db._auto_continue_receipt_is_current(current)
            and bool(observed_control.get("auto_continue"))
            and not artwork.artwork_db._auto_continue_receipt_is_current(observed_control)
            and observed_control.get("generation_id") == expected_generation_id
            and observed_control.get("authority_digest") == authority_digest
            and observed_control.get("style_version") == style_version
            and all(
                current.get(key) == observed_control.get(key)
                for key in (
                    "schema_version",
                    "generation_id",
                    "authority_digest",
                    "style_version",
                    "state",
                    "auto_continue",
                    "auto_continue_receipt",
                )
            )
        ):
            return {"outcome": "stale", "control": copy.deepcopy(current)}
        paused = {
            **current,
            "state": "paused",
            "auto_continue": False,
            "batch_size": int(current.get("batch_size") or artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE),
            "batch_remaining": 0,
            "pause_reason": "manual_batches_required",
            "updated_at": datetime.now(timezone.utc),
        }
        self.backfill_controls[uid] = paused
        return {"outcome": "updated", "control": copy.deepcopy(paused)}

    def list_jobs_for_uid(self, uid, *, migrate_legacy_jobs=True):
        self.job_list_migration_requests.append(migrate_legacy_jobs)
        return [copy.deepcopy(job) for (owner, _, _), job in self.jobs.items() if owner == uid]

    def get_job(self, uid, memory_id, generation_key):
        job = self.jobs.get((uid, memory_id, generation_key))
        return copy.deepcopy(job) if job is not None else None

    def get_conversation(self, uid, memory_id):
        value = self.conversations.get((uid, memory_id))
        return copy.deepcopy(value) if value is not None else None

    def list_conversations_page(self, uid, *, limit, cursor_memory_id=None):
        values = [copy.deepcopy(value) for (owner, _), value in self.conversations.items() if owner == uid]
        values = sorted(values, key=lambda value: value["created_at"], reverse=True)
        if cursor_memory_id:
            cursor_index = next(
                (index for index, value in enumerate(values) if value.get("id") == cursor_memory_id),
                None,
            )
            if cursor_index is None:
                raise ValueError("memory_artwork_backfill_cursor_invalid")
            values = values[cursor_index + 1 :]
        return values[:limit]

    def list_recent_conversations(self, uid, *, limit):
        return self.list_conversations_page(uid, limit=limit)

    def list_ready_artwork_conversations(self, uid):
        return [
            copy.deepcopy(value)
            for (owner, _), value in self.conversations.items()
            if owner == uid
            and any(
                isinstance(value.get(field), dict)
                and value[field].get("status") == "ready"
                and value[field].get("object_key")
                for field in (artwork.artwork_db.ARTWORK_FIELD, artwork.artwork_db.PUBLISHED_ARTWORK_FIELD)
            )
        ]

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
        allow_retry=True,
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
            if current_status == "generating":
                existing_job_status = existing_job.get("status")
                if not allow_retry and existing_job_status not in {"pending", "processing"}:
                    return {"outcome": "automatic_attempt_already_used", "artwork": copy.deepcopy(current)}
                if (
                    existing_job_status == "pending"
                    and existing_job.get("origin") != artwork.TERMINAL_ENRICHMENT_ORIGIN
                    and job_state.get("origin") == artwork.TERMINAL_ENRICHMENT_ORIGIN
                ):
                    self.jobs[job_key] = {
                        **existing_job,
                        "origin": artwork.TERMINAL_ENRICHMENT_ORIGIN,
                        "updated_at": job_state.get("updated_at"),
                    }
                elif (
                    existing_job_status == "pending"
                    and existing_job.get("origin") == artwork.HISTORICAL_BACKFILL_ORIGIN
                    and job_state.get("origin") == artwork.PREVIEW_BACKFILL_ORIGIN
                ):
                    self.jobs[job_key] = {
                        **existing_job,
                        "origin": artwork.PREVIEW_BACKFILL_ORIGIN,
                        "updated_at": job_state.get("updated_at"),
                    }
                elif existing_job_status not in {"pending", "processing"}:
                    self.jobs[job_key] = effective_job
            return {"outcome": "existing", "artwork": copy.deepcopy(current)}
        if not allow_retry and (current.get("generation_key") == generation_key or existing_job):
            return {"outcome": "automatic_attempt_already_used", "artwork": copy.deepcopy(current)}
        if current.get("status") == "ready" and str(current.get("object_key") or "").strip():
            conversation["published_artwork"] = copy.deepcopy(current)
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

    def clear_published_artwork(self, uid, memory_id, *, object_key, object_generation):
        conversation = self.conversations.get((uid, memory_id))
        published = (conversation or {}).get("published_artwork") or {}
        if (
            published.get("object_key") != object_key
            or str(published.get("object_generation") or "") != object_generation
        ):
            return False
        conversation.pop("published_artwork", None)
        return True

    def restore_permanent_artwork(
        self,
        uid,
        memory_id,
        *,
        binding_id,
        profile_id,
        authority_digest,
        consent_version,
    ):
        conversation = self.conversations.get((uid, memory_id))
        preferences = self.preferences_by_uid.get(uid) or {}
        if conversation is None:
            return {"outcome": "not_found"}
        if (
            preferences.get("consent") != "accepted"
            or preferences.get("consent_version") != consent_version
            or uid in self.deletion_pending
            or conversation.get("deletion_pending")
            or conversation.get("discarded")
            or artwork._source_is_sensitive(conversation)
        ):
            return {"outcome": "blocked"}
        if preferences.get("binding_id") != binding_id or preferences.get("profile_id") != profile_id:
            return {"outcome": "authority_mismatch"}
        for field in ("artwork", "published_artwork"):
            state = conversation.get(field) or {}
            if not state.get("object_key"):
                continue
            if state.get("status") == "unavailable" and state.get("failure_code") not in {
                "authority_changed",
                "authority_unavailable",
                "memory_artwork_preference_authority_stale",
                "preference_changed",
            }:
                continue
            if state.get("status") not in {"ready", "unavailable"}:
                continue
            was_restored = state.get("status") != "ready" or state.get("authority_digest") != authority_digest
            state.update({"status": "ready", "authority_digest": authority_digest})
            state.pop("failure_code", None)
            preferences["authority_digest"] = authority_digest
            return {
                "outcome": "restored" if was_restored else "ready",
                "field": field,
                "artwork": copy.deepcopy(state),
            }
        return {"outcome": "not_restorable"}

    def attach_artwork_renditions(
        self,
        uid,
        memory_id,
        *,
        source_object_key,
        source_object_generation,
        rendition_update,
    ):
        conversation = self.conversations.get((uid, memory_id))
        updated = False
        for field in ("artwork", "published_artwork"):
            state = (conversation or {}).get(field) or {}
            if state.get("object_key") == source_object_key and str(state.get("object_generation") or "") == str(
                source_object_generation
            ):
                state.update(copy.deepcopy(rendition_update))
                updated = True
        return updated

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
        if expected_artwork is not None:
            if (
                expected_artwork.get("status") != "ready"
                or not str(expected_artwork.get("object_key") or "").strip()
                or expected_artwork.get("generation_key") != generation_key
                or current != expected_artwork
            ):
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

    def create_reconciliation_job(self, uid, *, authority_digest, style_version):
        job_id = artwork.artwork_db.reconciliation_job_id(uid, authority_digest, style_version)
        existing = self.reconciliation_jobs.get(job_id)
        if existing and existing.get("status") in {"pending", "processing"}:
            return {"outcome": "existing", "job": copy.deepcopy(existing)}
        now = datetime.now(timezone.utc)
        job = {
            "schema_version": artwork.ARTWORK_RECONCILIATION_SCHEMA_VERSION,
            "job_id": job_id,
            "uid": uid,
            "authority_digest": authority_digest,
            "style_version": style_version,
            "status": "pending",
            "cursor": None,
            "pages_processed": 0,
            "scanned": 0,
            "queued": 0,
            "existing": 0,
            "skipped": 0,
            "attempt_count": 0,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
        self.reconciliation_jobs[job_id] = job
        return {"outcome": "reserved", "job": copy.deepcopy(job)}

    def get_reconciliation_job(self, uid, job_id):
        job = self.reconciliation_jobs.get(job_id)
        return copy.deepcopy(job) if job and job.get("uid") == uid else None

    def list_pending_reconciliation_jobs(self, *, limit, now):
        return [
            copy.deepcopy(job)
            for job in self.reconciliation_jobs.values()
            if job.get("status") == "pending" and job.get("available_at") <= now
        ][:limit]

    def claim_reconciliation_job(self, uid, job_id, *, lease_token, now, lease_seconds):
        job = self.reconciliation_jobs.get(job_id)
        if uid in self.deletion_pending:
            self.reconciliation_jobs.pop(job_id, None)
            return None
        control = self.backfill_controls.get(uid)
        if control and (control.get("generation_id") != job_id or control.get("state") != "running"):
            return None
        if not job or job.get("uid") != uid or job.get("status") != "pending":
            return None
        job.update(
            {
                "status": "processing",
                "lease_token": lease_token,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
            }
        )
        return copy.deepcopy(job)

    def finish_reconciliation_job(self, job_id, *, lease_token, update):
        job = self.reconciliation_jobs.get(job_id)
        if not job or job.get("status") != "processing" or job.get("lease_token") != lease_token:
            return False
        job.update(copy.deepcopy(update))
        job.pop("lease_token", None)
        job.pop("lease_expires_at", None)
        return True

    def claim_job(self, uid, memory_id, generation_key, *, lease_token, now, lease_seconds):
        if uid in self.deletion_pending:
            return None
        job = self.jobs.get((uid, memory_id, generation_key))
        if job is None:
            return None
        preferences = self.preferences_by_uid.get(uid) or {}
        job.setdefault("authority_digest", preferences.get("authority_digest", ""))
        job.setdefault("style_version", preferences.get("style_version", ""))
        job.setdefault("origin", artwork.HISTORICAL_BACKFILL_ORIGIN)
        if (
            not job.get("authority_digest")
            or not job.get("style_version")
            or job.get("authority_digest") != preferences.get("authority_digest")
            or job.get("style_version") != preferences.get("style_version")
        ):
            return None
        control = self.backfill_controls.get(uid)
        expected_generation_id = artwork.artwork_db.reconciliation_job_id(
            uid,
            str(job.get("authority_digest") or ""),
            str(job.get("style_version") or ""),
        )
        if job.get("origin") != artwork.TERMINAL_ENRICHMENT_ORIGIN:
            if control is None:
                control = {
                    "schema_version": "ella.memory_artwork.queue_control.v1",
                    "generation_id": expected_generation_id,
                    "authority_digest": job["authority_digest"],
                    "style_version": job["style_version"],
                    "state": "running",
                    "auto_continue": False,
                    "batch_size": artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE,
                    "batch_remaining": artwork.DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE,
                    "pause_reason": "",
                }
                self.backfill_controls[uid] = control
            if control.get("generation_id") != expected_generation_id or control.get("state") != "running":
                return None
            if control.get("auto_continue") and not artwork.artwork_db._auto_continue_receipt_is_current(control):
                control.update(
                    {
                        "state": "paused",
                        "auto_continue": False,
                        "batch_remaining": 0,
                        "pause_reason": "manual_batches_required",
                    }
                )
                return None
        if job.get("status") == "processing":
            lease_expires_at = job.get("lease_expires_at")
            if not isinstance(lease_expires_at, datetime) or lease_expires_at > now:
                return None
        elif job.get("status") != "pending":
            return None
        if job.get("origin") != artwork.TERMINAL_ENRICHMENT_ORIGIN and not control.get("auto_continue"):
            remaining = int(control.get("batch_remaining") or 0)
            if remaining < 1:
                return None
            remaining -= 1
            control["batch_remaining"] = remaining
            if remaining == 0:
                control["state"] = "paused"
                control["pause_reason"] = "batch_complete"
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
        self.storage_cleanup_required_uids.add(uid)
        return True

    def renew_publication_claim(
        self,
        uid,
        memory_id,
        generation_key,
        *,
        generation_lease_token,
        job_lease_token,
        now,
        lease_seconds,
    ):
        if not self.mark_storage_cleanup_required(
            uid,
            memory_id,
            generation_key,
            generation_lease_token=generation_lease_token,
            job_lease_token=job_lease_token,
        ):
            return False
        publication_expiry = now + timedelta(seconds=lease_seconds)
        conversation = self.conversations[(uid, memory_id)]
        conversation["artwork"]["lease_expires_at"] = publication_expiry
        self.jobs[(uid, memory_id, generation_key)]["lease_expires_at"] = publication_expiry
        return True

    def storage_cleanup_required(self, uid):
        return uid in self.storage_cleanup_required_uids


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


def _valid_test_image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (1536, 1024), color=(68, 107, 91)).save(output, format="JPEG", quality=90)
    return output.getvalue()


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
            image_bytes=_valid_test_image_bytes(),
            content_type="image/jpeg",
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
        extension = "jpg" if kwargs["content_type"] == "image/jpeg" else "webp"
        rendition = kwargs.get("rendition", "master")
        return artwork.StoredArtwork(
            object_key=(
                f"users/owner/profiles/{kwargs['profile_binding_id']}/memories/"
                f"{kwargs['memory_id']}/{kwargs['generation_key']}/{hashlib.sha256(kwargs['image_bytes']).hexdigest()}-"
                f"{rendition}.{extension}"
            ),
            object_generation="7",
            content_type=kwargs["content_type"],
            byte_size=len(kwargs["image_bytes"]),
            cache_key=hashlib.sha256(kwargs["image_bytes"]).hexdigest(),
        )

    def get_bytes(self, **kwargs):
        return _valid_test_image_bytes()

    def delete(self, **kwargs):
        self.deletes.append(kwargs)

    def delete_memory_prefix(self, **kwargs):
        self.prefix_deletes.append(kwargs)
        return 0

    def delete_memory_all_bindings(self, **kwargs):
        self.prefix_deletes.append(kwargs)
        return 0

    def delete_user_prefix(self, **kwargs):
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
        revision=7,
        authority_digest=digest,
    )


def _enabled_config(*, backfill=True, provider=True):
    return artwork.MemoryArtworkConfig(
        enabled=True,
        release_enabled=True,
        provider_enabled=provider,
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


def _queue_job(
    *,
    status,
    style=artwork.DEFAULT_STYLE_VERSION,
    attempt_count=0,
    lease_expires_at=None,
):
    return {
        "uid": "owner-a",
        "memory_id": f"memory-{status}-{attempt_count}-{style}",
        "generation_key": hashlib.sha256(f"{status}-{attempt_count}-{style}".encode()).hexdigest(),
        "authority_digest": "digest-a",
        "style_version": style,
        "status": status,
        "attempt_count": attempt_count,
        "available_at": datetime.now(timezone.utc),
        "lease_expires_at": lease_expires_at,
        "updated_at": datetime.now(timezone.utc),
    }


def test_queue_status_separates_active_queued_retrying_failed_and_style_generations():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    jobs = [
        _queue_job(status="completed"),
        _queue_job(status="processing", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)),
        _queue_job(status="pending"),
        _queue_job(status="pending", attempt_count=2),
        _queue_job(status="failed", attempt_count=5),
        _queue_job(status="completed", style="ella.memory_artwork.style.paper-collage.v1"),
    ]
    for index, job in enumerate(jobs):
        repository.jobs[("owner-a", f"memory-{index}", job["generation_key"])] = job
    job_id = artwork.artwork_db.reconciliation_job_id("owner-a", "digest-a", artwork.DEFAULT_STYLE_VERSION)
    repository.reconciliation_jobs[job_id] = {
        "job_id": job_id,
        "uid": "owner-a",
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "status": "completed",
        "scanned": 5,
        "pages_processed": 1,
        "updated_at": datetime.now(timezone.utc),
    }
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )

    status = asyncio.run(service.queue_status("owner-a"))

    assert status["schema_version"] == artwork.ARTWORK_QUEUE_SCHEMA_VERSION
    assert status["generation_id"] == job_id
    assert status["state"] == "needs_attention"
    assert status["control_state"] == "running"
    assert status["ready"] == 1
    assert status["active"] == 1
    assert status["queued"] == 1
    assert status["retrying"] == 1
    assert status["exhausted"] == 1
    assert status["failed"] == 1
    assert status["total"] == 5
    assert status["remaining"] == 4
    assert status["scanned"] == 5
    assert repository.job_list_migration_requests == [False]
    by_style = {item["style_version"]: item for item in status["styles"]}
    assert by_style["ella.memory_artwork.style.paper-collage.v1"]["ready"] == 1
    assert by_style["ella.memory_artwork.style.paper-collage.v1"]["state"] == "paused"


def test_recent_recovery_is_recent_first_idempotent_and_reports_durable_states():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    base_time = datetime(2026, 9, 24, tzinfo=timezone.utc)

    ready = _terminal_memory("ready", created_at=base_time)
    ready["artwork"] = _ready_artwork(
        ready,
        authority=authority,
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
    stale = _terminal_memory("stale", created_at=base_time - timedelta(minutes=1))
    stale["artwork"] = _ready_artwork(
        stale,
        authority=authority,
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
    stale["active_summary_version_id"] = "summary-stale-corrected"
    stale["structured"]["overview"] = "A corrected walk near a garden."
    missing = _terminal_memory("missing", created_at=base_time - timedelta(minutes=2))
    exhausted = _terminal_memory("exhausted", created_at=base_time - timedelta(minutes=3))
    retrying = _terminal_memory("retrying", created_at=base_time - timedelta(minutes=4))
    for memory in (ready, stale, missing, exhausted, retrying):
        repository.conversations[("owner-a", memory["id"])] = memory

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    asyncio.run(service.enqueue("owner-a", "exhausted", request_mode="automatic"))
    exhausted_artwork = repository.conversations[("owner-a", "exhausted")]["artwork"]
    exhausted_key = exhausted_artwork["generation_key"]
    exhausted_artwork.update({"status": "unavailable", "failure_code": "memory_artwork_provider_failed"})
    repository.jobs[("owner-a", "exhausted", exhausted_key)].update(
        {"status": "failed", "attempt_count": artwork.WORKER_MAX_ATTEMPTS}
    )
    asyncio.run(service.enqueue("owner-a", "retrying", request_mode="automatic"))
    retrying_artwork = repository.conversations[("owner-a", "retrying")]["artwork"]
    retrying_key = retrying_artwork["generation_key"]
    retrying_artwork.update({"status": "unavailable", "failure_code": "memory_artwork_provider_failed"})
    repository.jobs[("owner-a", "retrying", retrying_key)].update({"status": "pending", "attempt_count": 2})
    writes_before_recovery = repository.reserve_writes

    first = asyncio.run(service.recover_recent("owner-a"))

    assert first == {
        "schema_version": artwork.ARTWORK_RECENT_RECOVERY_SCHEMA_VERSION,
        "scanned": 5,
        "reservation_limit": artwork.RECENT_RECOVERY_RESERVATION_LIMIT,
        "reserved": 1,
        "deferred": 0,
        "ready": 2,
        "pending": 1,
        "retrying": 1,
        "exhausted": 1,
        "skipped": 0,
        "items": [
            {"memory_id": "missing", "status": "pending"},
            {"memory_id": "exhausted", "status": "exhausted"},
            {"memory_id": "retrying", "status": "retrying"},
        ],
    }
    assert repository.reserve_writes == writes_before_recovery + 1
    assert repository.conversations[("owner-a", "ready")]["artwork"]["status"] == "ready"

    second = asyncio.run(service.recover_recent("owner-a"))

    assert second["reserved"] == 0
    assert second["pending"] == 1
    assert second["retrying"] == 1
    assert second["exhausted"] == 1
    assert repository.reserve_writes == writes_before_recovery + 1


def test_recent_recovery_caps_new_reservations_without_starving_later_candidates():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    for index in range(12):
        memory = _terminal_memory(f"memory-{index}", created_at=now - timedelta(minutes=index))
        repository.conversations[("owner-a", memory["id"])] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    first = asyncio.run(service.recover_recent("owner-a"))
    second = asyncio.run(service.recover_recent("owner-a"))

    assert first["reserved"] == artwork.RECENT_RECOVERY_RESERVATION_LIMIT
    assert first["deferred"] == 2
    assert second["reserved"] == 2
    assert second["deferred"] == 0
    assert second["pending"] == 12
    assert repository.reserve_writes == 12


def test_recent_recovery_does_not_report_stale_ready_artwork_over_current_pending_job():
    generation_key = "a" * 64

    status = artwork._recovery_status(
        {"status": "ready", "generation_key": "b" * 64},
        {
            "status": "pending",
            "attempt_count": 0,
            "generation_key": generation_key,
            "authority_digest": "digest-a",
        },
        generation_key=generation_key,
        authority_digest="digest-a",
    )

    assert status == "pending"


def test_recent_recovery_reports_current_objectless_ready_artwork_as_exhausted():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    memory = _terminal_memory("objectless")
    memory["artwork"] = _ready_artwork(
        memory,
        authority=authority,
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
    memory["artwork"]["generation_key"] = artwork._generation_key(
        uid="owner-a",
        authority=authority,
        memory_id="objectless",
        enrichment_revision=memory["active_summary_version_id"],
        style_version=artwork.DEFAULT_STYLE_VERSION,
        prompt_sha256=memory["artwork"]["prompt_sha256"],
    )
    memory["artwork"].pop("object_key")
    repository.conversations[("owner-a", "objectless")] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    result = asyncio.run(service.recover_recent("owner-a"))

    assert result["ready"] == 0
    assert result["exhausted"] == 1
    assert result["reserved"] == 0
    assert result["items"] == [{"memory_id": "objectless", "status": "exhausted"}]


def test_recent_recovery_reads_the_generation_reserved_after_source_drift():
    class SourceDriftRepository(FakeRepository):
        def list_conversations_page(self, uid, *, limit, cursor_memory_id=None):
            snapshot = super().list_conversations_page(
                uid,
                limit=limit,
                cursor_memory_id=cursor_memory_id,
            )
            self.conversations[(uid, "drift")]["active_summary_version_id"] = "summary-drift-corrected"
            return snapshot

    repository = SourceDriftRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    repository.conversations[("owner-a", "drift")] = _terminal_memory("drift")
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    result = asyncio.run(service.recover_recent("owner-a"))

    reserved_key = repository.conversations[("owner-a", "drift")]["artwork"]["generation_key"]
    assert repository.jobs[("owner-a", "drift", reserved_key)]["status"] == "pending"
    assert result["pending"] == 1
    assert result["exhausted"] == 0
    assert result["items"] == [{"memory_id": "drift", "status": "pending"}]


def test_recent_recovery_preserves_generation_across_runtime_fingerprint_rotation():
    repository = FakeRepository()
    old_authority = _authority(digest="digest-old")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(old_authority)
    repository.conversations[("owner-a", "rotated")] = _terminal_memory("rotated")
    old_service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: asyncio.sleep(0, result=old_authority),
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    assert asyncio.run(old_service.enqueue("owner-a", "rotated", request_mode="automatic"))["outcome"] == "reserved"
    old_generation_key = repository.conversations[("owner-a", "rotated")]["artwork"]["generation_key"]
    writes_before_rotation = repository.reserve_writes

    new_authority = _authority(digest="digest-new")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(new_authority)
    current_service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: asyncio.sleep(0, result=new_authority),
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call provider")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    first = asyncio.run(current_service.recover_recent("owner-a"))
    current_generation_key = repository.conversations[("owner-a", "rotated")]["artwork"]["generation_key"]
    second = asyncio.run(current_service.recover_recent("owner-a"))

    assert current_generation_key == old_generation_key
    assert repository.jobs[("owner-a", "rotated", old_generation_key)]["authority_digest"] == "digest-old"
    assert first["pending"] == 1
    assert first["retrying"] == 0
    assert first["exhausted"] == 0
    assert first["reserved"] == 0
    assert first["items"] == [{"memory_id": "rotated", "status": "pending"}]
    assert second["pending"] == 1
    assert second["reserved"] == 0
    assert repository.reserve_writes == writes_before_rotation


def test_recent_recovery_fails_before_inventory_without_current_consent_or_binding():
    class NeverScanRepository(FakeRepository):
        def list_conversations_page(self, uid, *, limit, cursor_memory_id=None):
            raise AssertionError("recovery must fence authority before inventory")

    repository = NeverScanRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    without_consent = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        global_consent_checker=lambda uid: False,
        config=_enabled_config(),
    )

    with pytest.raises(artwork.MemoryArtworkError) as denied:
        asyncio.run(without_consent.recover_recent("owner-a"))
    assert denied.value.code == "memory_artwork_consent_required"

    repository.preferences_by_uid["owner-a"]["binding_id"] = "stale-binding"
    with_stale_authority = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        global_consent_checker=lambda uid: True,
        config=_enabled_config(),
    )
    with pytest.raises(artwork.MemoryArtworkError) as stale:
        asyncio.run(with_stale_authority.recover_recent("owner-a"))
    assert stale.value.code == "memory_artwork_preference_authority_stale"

    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    repository.deletion_pending.add("owner-a")
    with pytest.raises(artwork.MemoryArtworkError) as deletion:
        asyncio.run(with_stale_authority.recover_recent("owner-a"))
    assert deletion.value.code == "memory_artwork_deletion_pending"


def test_permanent_recovery_restores_runtime_stale_and_backfills_variants_without_provider_calls():
    repository = FakeRepository()
    old_authority = _authority(digest="legacy-runtime-fingerprint")
    current_authority = artwork.ArtworkRuntimeAuthority(
        uid="owner-a",
        binding_id=old_authority.binding_id,
        profile_id=old_authority.profile_id,
        revision=99,
        authority_digest="stable-owner-profile-digest",
    )
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(old_authority)
    restorable = _terminal_memory("restorable")
    restorable["artwork"] = {
        **_ready_artwork(restorable, authority=old_authority, style_version=artwork.DEFAULT_STYLE_VERSION),
        "status": "unavailable",
        "failure_code": "authority_changed",
        "generation_key": "a" * 64,
        "object_generation": "7",
        "content_type": "image/png",
    }
    source_changed = _terminal_memory("source-changed")
    source_changed["artwork"] = {
        **_ready_artwork(source_changed, authority=old_authority, style_version=artwork.DEFAULT_STYLE_VERSION),
        "status": "unavailable",
        "failure_code": "source_changed",
        "generation_key": "b" * 64,
        "object_generation": "8",
        "content_type": "image/png",
    }
    repository.conversations[("owner-a", "restorable")] = restorable
    repository.conversations[("owner-a", "source-changed")] = source_changed
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: asyncio.sleep(0, result=current_authority),
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call a provider")),
        store_factory=lambda: store,
        config=_enabled_config(provider=False),
    )

    result = asyncio.run(service.recover_permanent_artwork("owner-a"))

    assert result["restored"] == 1
    assert result["regenerations_avoided"] == 1
    assert result["variant_backfilled"] == 1
    assert result["provider_calls"] == 0
    assert {item["rendition"] for item in store.puts} == {"master", "w384", "w768", "w1536"}
    recovered = repository.conversations[("owner-a", "restorable")]["artwork"]
    assert recovered["status"] == "ready"
    assert recovered["authority_digest"] == current_authority.authority_digest
    assert [item["w"] for item in recovered["variants"]] == [384, 768, 1536]
    assert repository.conversations[("owner-a", "source-changed")]["artwork"]["status"] == "unavailable"
    assert store.deletes == []


def test_permanent_recovery_erases_new_renditions_when_source_becomes_sensitive_before_attach(monkeypatch):
    class SensitiveDuringAttachRepository(FakeRepository):
        def attach_artwork_renditions(self, uid, memory_id, **kwargs):
            self.conversations[(uid, memory_id)]["ella_tags"] = ["caregiver-private"]
            return False

    repository = SensitiveDuringAttachRepository()
    authority = _authority(digest="stable-owner-profile-digest")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    memory = _terminal_memory("restorable")
    memory["artwork"] = {
        **_ready_artwork(memory, authority=authority, style_version=artwork.DEFAULT_STYLE_VERSION),
        "generation_key": "a" * 64,
        "object_generation": "7",
        "content_type": "image/png",
    }
    repository.conversations[("owner-a", "restorable")] = memory
    store = FakeStore()

    def erase_sensitive(uid, memory_id, conversation, *, lock_proof, store):
        store.delete_memory_all_bindings(uid=uid, memory_id=memory_id)

    monkeypatch.setattr(artwork, "delete_memory_artwork_for_exclusion", erase_sensitive)
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: asyncio.sleep(0, result=authority),
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("recovery must not call a provider")),
        store_factory=lambda: store,
        config=_enabled_config(provider=False),
    )

    result = asyncio.run(service.recover_permanent_artwork("owner-a"))

    assert result["variant_backfilled"] == 0
    assert result["failures"] == 1
    assert result["provider_calls"] == 0
    assert store.prefix_deletes == [{"uid": "owner-a", "memory_id": "restorable"}]


def test_generation_identity_ignores_runtime_fingerprint_and_revision():
    memory = _terminal_memory("memory-stable")
    _, prompt_sha256 = artwork._prompt_for(memory, artwork.DEFAULT_STYLE_VERSION)
    before = _authority(digest="runtime-token-a")
    after = artwork.ArtworkRuntimeAuthority(
        uid=before.uid,
        binding_id=before.binding_id,
        profile_id=before.profile_id,
        revision=before.revision + 100,
        authority_digest="runtime-token-b",
    )

    before_key = artwork._generation_key(
        uid="owner-a",
        authority=before,
        memory_id="memory-stable",
        enrichment_revision=memory["active_summary_version_id"],
        style_version=artwork.DEFAULT_STYLE_VERSION,
        prompt_sha256=prompt_sha256,
    )
    after_key = artwork._generation_key(
        uid="owner-a",
        authority=after,
        memory_id="memory-stable",
        enrichment_revision=memory["active_summary_version_id"],
        style_version=artwork.DEFAULT_STYLE_VERSION,
        prompt_sha256=prompt_sha256,
    )

    assert before_key == after_key


def test_automatic_source_refresh_retains_ready_artwork_without_job_or_runtime_lookup():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    memory = _terminal_memory("memory-1")
    memory["artwork"] = _ready_artwork(
        memory,
        authority=authority,
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
    published = copy.deepcopy(memory["artwork"])
    memory["active_summary_version_id"] = "summary-edited"
    memory["structured"]["title"] = "Edited title"
    repository.conversations[("owner-a", "memory-1")] = memory

    async def runtime_must_not_run(uid):
        raise AssertionError("automatic stale retention must not resolve runtime authority")

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=runtime_must_not_run,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    result = asyncio.run(service.enqueue("owner-a", "memory-1", request_mode="automatic"))

    assert result == {"outcome": "stale_retained", "status": "ready", "stale": True}
    assert repository.conversations[("owner-a", "memory-1")]["artwork"] == published
    assert repository.jobs == {}


def test_responsive_renditions_are_three_by_two_and_strip_exif():
    source = Image.new("RGB", (1800, 1200), color=(100, 80, 60))
    exif = Image.Exif()
    exif[0x010E] = "private description"
    encoded = io.BytesIO()
    source.save(encoded, format="JPEG", quality=90, exif=exif)

    renditions = artwork._responsive_renditions(encoded.getvalue())

    assert [(item.name, item.pixel_width, item.pixel_height) for item in renditions] == [
        ("master", 1536, 1024),
        ("w384", 384, 256),
        ("w768", 768, 512),
        ("w1536", 1536, 1024),
    ]
    for rendition in renditions:
        with Image.open(io.BytesIO(rendition.image_bytes)) as decoded:
            assert decoded.size == (rendition.pixel_width, rendition.pixel_height)
            assert len(decoded.getexif()) == 0


def test_generation_releases_rendition_objects_before_final_authority_await(monkeypatch):
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    rendition_refs = []
    original_responsive_renditions = artwork._responsive_renditions

    def tracked_renditions(image_bytes):
        renditions = original_responsive_renditions(image_bytes)
        rendition_refs.extend(weakref.ref(rendition) for rendition in renditions)
        return renditions

    class NonRetainingStore(FakeStore):
        def put(self, **kwargs):
            content_digest = hashlib.sha256(kwargs["image_bytes"]).hexdigest()
            rendition = kwargs.get("rendition", "master")
            return artwork.StoredArtwork(
                object_key=f"private/{content_digest}-{rendition}",
                object_generation="7",
                content_type=kwargs["content_type"],
                byte_size=len(kwargs["image_bytes"]),
                cache_key=content_digest,
            )

    async def resolving_authority(uid):
        if rendition_refs:
            gc.collect()
            assert all(reference() is None for reference in rendition_refs)
        return authority

    monkeypatch.setattr(artwork, "_responsive_renditions", tracked_renditions)
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=resolving_authority,
        provider_factory=FakeProvider,
        store_factory=NonRetainingStore,
        config=_enabled_config(),
    )

    assert asyncio.run(service.enqueue("owner-a", "memory-1"))["outcome"] == "reserved"
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    assert rendition_refs
    assert all(reference() is None for reference in rendition_refs)


def test_legacy_job_metadata_is_recovered_only_from_its_exact_generation():
    legacy = {"generation_key": "a" * 64}
    conversation = {
        "artwork": {
            "generation_key": "a" * 64,
            "authority_digest": "digest-a",
            "style_version": artwork.DEFAULT_STYLE_VERSION,
        }
    }

    assert artwork_database._legacy_job_metadata(legacy, conversation) == {
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "origin": artwork.HISTORICAL_BACKFILL_ORIGIN,
    }
    conversation["artwork"]["generation_key"] = "b" * 64
    assert artwork_database._legacy_job_metadata(legacy, conversation) == {}


def test_get_job_rejects_a_hash_addressed_record_with_mismatched_subject_fields(monkeypatch):
    class Snapshot:
        exists = True
        id = "job-id"

        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return copy.deepcopy(self.payload)

    class Reference:
        def __init__(self, payload):
            self.payload = payload

        def get(self):
            return Snapshot(self.payload)

    payload = {
        "uid": "owner-a",
        "memory_id": "memory-a",
        "generation_key": "a" * 64,
        "status": "pending",
    }
    reference = Reference(payload)
    monkeypatch.setattr(artwork_database, "_job_ref", lambda *args: reference)

    assert artwork_database.get_job("owner-a", "memory-a", "a" * 64) == {
        **payload,
        "job_id": "job-id",
    }

    reference.payload = {**payload, "uid": "owner-b"}
    assert artwork_database.get_job("owner-a", "memory-a", "a" * 64) is None


def test_pause_blocks_new_reconciliation_and_generation_leases_but_does_not_revoke_active_claim():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    memory = _terminal_memory("memory-1")
    repository.conversations[("owner-a", "memory-1")] = memory
    asyncio.run(service.enqueue("owner-a", "memory-1", origin=artwork.HISTORICAL_BACKFILL_ORIGIN))
    generation_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"]
    reconciliation = asyncio.run(service.start_reconciliation("owner-a"))
    generation_id = reconciliation["job_id"]
    claimed = repository.claim_job(
        "owner-a",
        "memory-1",
        generation_key,
        lease_token="active-lease",
        now=datetime.now(timezone.utc),
        lease_seconds=120,
    )
    assert claimed is not None

    paused = asyncio.run(service.set_queue_control("owner-a", action="pause", generation_id=generation_id))

    assert paused["control_state"] == "paused"
    assert (
        repository.claim_reconciliation_job(
            "owner-a",
            generation_id,
            lease_token="scan-lease",
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        is None
    )
    assert (
        repository.claim_job(
            "owner-a",
            "memory-1",
            generation_key,
            lease_token="replacement-lease",
            now=datetime.now(timezone.utc) + timedelta(minutes=3),
            lease_seconds=120,
        )
        is None
    )
    assert repository.complete_job(
        "owner-a",
        "memory-1",
        generation_key,
        lease_token="active-lease",
    )


def test_cancel_is_non_destructive_and_resume_restarts_exact_style_generation():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    ready = _queue_job(status="completed")
    pending = _queue_job(status="pending")
    repository.jobs[("owner-a", "ready", ready["generation_key"])] = ready
    repository.jobs[("owner-a", "pending", pending["generation_key"])] = pending
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))
    generation_id = started["job_id"]
    repository.reconciliation_jobs[generation_id]["status"] = "completed"

    cancelled = asyncio.run(service.set_queue_control("owner-a", action="cancel", generation_id=generation_id))
    assert cancelled["control_state"] == "cancelled"
    assert cancelled["ready"] == 1
    assert cancelled["queued"] == 1
    assert repository.jobs[("owner-a", "ready", ready["generation_key"])]["status"] == "completed"

    resumed = asyncio.run(service.set_queue_control("owner-a", action="resume", generation_id=generation_id))
    assert resumed["control_state"] == "running"
    assert repository.reconciliation_jobs[generation_id]["status"] == "completed"


def test_queue_control_rejects_stale_generation_after_style_switch():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    stale_generation = artwork.artwork_db.reconciliation_job_id(
        "owner-a",
        "digest-a",
        "ella.memory_artwork.style.paper-collage.v1",
    )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(service.set_queue_control("owner-a", action="pause", generation_id=stale_generation))

    assert failure.value.code == "memory_artwork_queue_generation_stale"


def test_historical_backfill_stops_after_ten_claims_while_new_enrichment_remains_immediate():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    generation_keys = []
    for index in range(12):
        memory_id = f"history-{index}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(memory_id)
        asyncio.run(service.enqueue("owner-a", memory_id, origin=artwork.HISTORICAL_BACKFILL_ORIGIN))
        job = next(job for (owner, item, _), job in repository.jobs.items() if owner == "owner-a" and item == memory_id)
        assert job["authority_digest"] == "digest-a"
        assert job["style_version"] == artwork.DEFAULT_STYLE_VERSION
        assert job["origin"] == artwork.HISTORICAL_BACKFILL_ORIGIN
        generation_keys.append((memory_id, job["generation_key"]))

    for index, (memory_id, generation_key) in enumerate(generation_keys[:10]):
        lease_token = f"batch-{index}"
        assert repository.claim_job(
            "owner-a",
            memory_id,
            generation_key,
            lease_token=lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        assert repository.complete_job("owner-a", memory_id, generation_key, lease_token=lease_token)

    control = repository.backfill_controls["owner-a"]
    assert control["state"] == "paused"
    assert control["batch_remaining"] == 0
    assert control["pause_reason"] == "batch_complete"
    memory_id, generation_key = generation_keys[10]
    assert (
        repository.claim_job(
            "owner-a",
            memory_id,
            generation_key,
            lease_token="blocked-eleventh",
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        is None
    )

    repository.conversations[("owner-a", "new-memory")] = _terminal_memory("new-memory")
    asyncio.run(service.enqueue("owner-a", "new-memory"))
    new_job = next(
        job for (owner, item, _), job in repository.jobs.items() if owner == "owner-a" and item == "new-memory"
    )
    assert new_job["origin"] == artwork.TERMINAL_ENRICHMENT_ORIGIN
    assert repository.claim_job(
        "owner-a",
        "new-memory",
        new_job["generation_key"],
        lease_token="new-memory-lease",
        now=datetime.now(timezone.utc),
        lease_seconds=120,
    )


def test_terminal_event_promotes_an_existing_pending_historical_job():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )

    asyncio.run(service.enqueue("owner-a", "memory-1", origin=artwork.HISTORICAL_BACKFILL_ORIGIN))
    first_job = next(iter(repository.jobs.values()))
    assert first_job["origin"] == artwork.HISTORICAL_BACKFILL_ORIGIN

    result = asyncio.run(service.enqueue("owner-a", "memory-1", origin=artwork.TERMINAL_ENRICHMENT_ORIGIN))

    assert result["outcome"] == "existing"
    assert next(iter(repository.jobs.values()))["origin"] == artwork.TERMINAL_ENRICHMENT_ORIGIN


def test_preview_promotes_recent_existing_historical_work_without_a_duplicate_generation():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )

    asyncio.run(service.enqueue("owner-a", "memory-1", origin=artwork.HISTORICAL_BACKFILL_ORIGIN))
    result = asyncio.run(service.enqueue("owner-a", "memory-1", origin=artwork.PREVIEW_BACKFILL_ORIGIN))

    assert result["outcome"] == "existing"
    job = next(iter(repository.jobs.values()))
    assert job["origin"] == artwork.PREVIEW_BACKFILL_ORIGIN
    assert repository.reserve_writes == 1


def test_fresh_owner_auto_continue_resume_persists_receipt_through_status_and_claims():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    generation_id = artwork.artwork_db.reconciliation_job_id("owner-a", "digest-a", artwork.DEFAULT_STYLE_VERSION)
    repository.backfill_controls["owner-a"] = {
        "schema_version": "ella.memory_artwork.queue_control.v1",
        "generation_id": generation_id,
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "state": "paused",
        "auto_continue": False,
        "batch_size": 10,
        "batch_remaining": 0,
        "pause_reason": "batch_complete",
    }

    manual = asyncio.run(service.set_queue_control("owner-a", action="resume", generation_id=generation_id))
    assert manual["control_state"] == "running"
    assert manual["auto_continue"] is False
    assert manual["batch_remaining"] == 10
    assert repository.reconciliation_jobs == {}

    asyncio.run(service.set_queue_control("owner-a", action="pause", generation_id=generation_id))
    automatic = asyncio.run(
        service.set_queue_control("owner-a", action="resume", generation_id=generation_id, auto_continue=True)
    )
    assert automatic["control_state"] == "running"
    assert automatic["auto_continue"] is True
    assert automatic["batch_remaining"] == 0
    assert repository.backfill_controls["owner-a"]["auto_continue_receipt"] == {
        "schema_version": "ella.memory_artwork.auto_continue.v1",
        "generation_id": generation_id,
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "granted_at": repository.backfill_controls["owner-a"]["updated_at"],
    }
    status = asyncio.run(service.queue_status("owner-a"))
    assert status["control_state"] == "running"
    assert status["auto_continue"] is True
    assert status["batch_remaining"] == 0
    assert repository.reconciliation_jobs == {}

    for index in range(2):
        memory_id = f"auto-history-{index}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(memory_id)
        asyncio.run(service.enqueue("owner-a", memory_id, origin=artwork.HISTORICAL_BACKFILL_ORIGIN))
        job = next(job for (owner, item, _), job in repository.jobs.items() if owner == "owner-a" and item == memory_id)
        assert repository.claim_job(
            "owner-a",
            memory_id,
            job["generation_key"],
            lease_token=f"auto-lease-{index}",
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        assert repository.complete_job("owner-a", memory_id, job["generation_key"], lease_token=f"auto-lease-{index}")

    assert repository.backfill_controls["owner-a"]["state"] == "running"
    assert repository.backfill_controls["owner-a"]["auto_continue"] is True
    assert repository.backfill_controls["owner-a"]["batch_remaining"] == 0


def test_queue_status_pauses_a_persisted_legacy_automatic_run():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    generation_id = artwork.artwork_db.reconciliation_job_id("owner-a", "digest-a", artwork.DEFAULT_STYLE_VERSION)
    repository.backfill_controls["owner-a"] = {
        "schema_version": "ella.memory_artwork.queue_control.v1",
        "generation_id": generation_id,
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "state": "running",
        "auto_continue": True,
        "batch_size": 10,
        "batch_remaining": 0,
        "pause_reason": "",
    }

    status = asyncio.run(service.queue_status("owner-a"))

    assert status["control_state"] == "paused"
    assert status["auto_continue"] is False
    assert status["batch_remaining"] == 0


def test_concurrent_owner_auto_continue_resume_wins_over_stale_queue_status_pause():
    class InterleavingRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.resume_committed_before_stale_pause = False

        def pause_observed_legacy_auto_continue_control(
            self,
            uid,
            *,
            observed_control,
            expected_generation_id,
            authority_digest,
            style_version,
        ):
            if not self.resume_committed_before_stale_pause:
                self.resume_committed_before_stale_pause = True
                resume = self.set_backfill_control(
                    uid,
                    expected_generation_id=expected_generation_id,
                    state="running",
                    auto_continue=True,
                )
                assert resume["outcome"] == "updated"
                assert artwork.artwork_db._auto_continue_receipt_is_current(resume["control"])
            return super().pause_observed_legacy_auto_continue_control(
                uid,
                observed_control=observed_control,
                expected_generation_id=expected_generation_id,
                authority_digest=authority_digest,
                style_version=style_version,
            )

    repository = InterleavingRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    generation_id = artwork.artwork_db.reconciliation_job_id("owner-a", "digest-a", artwork.DEFAULT_STYLE_VERSION)
    repository.backfill_controls["owner-a"] = {
        "schema_version": "ella.memory_artwork.queue_control.v1",
        "generation_id": generation_id,
        "authority_digest": "digest-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "state": "running",
        "auto_continue": True,
        "batch_size": 10,
        "batch_remaining": 0,
        "pause_reason": "",
    }

    status = asyncio.run(service.queue_status("owner-a"))

    control = repository.backfill_controls["owner-a"]
    assert repository.resume_committed_before_stale_pause is True
    assert status["control_state"] == "running"
    assert status["auto_continue"] is True
    assert status["batch_remaining"] == 0
    assert control["state"] == "running"
    assert control["auto_continue"] is True
    assert artwork.artwork_db._auto_continue_receipt_is_current(control)


def test_database_legacy_auto_continue_pause_cas_preserves_concurrent_receipt():
    class Snapshot:
        def __init__(self, payload):
            self.exists = True
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    class Reference:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._snapshot = Snapshot(payload)

        def get(self, transaction=None):
            return self._snapshot

    class Transaction:
        def __init__(self):
            self.sets = []

        def set(self, reference, payload, merge=False):
            self.sets.append((reference, copy.deepcopy(payload), merge))

    preferences = _accepted_preferences(_authority())
    generation_id = artwork_database.reconciliation_job_id(
        "owner-a",
        preferences["authority_digest"],
        preferences["style_version"],
    )
    observed_stale_control = {
        "schema_version": artwork_database.QUEUE_CONTROL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "authority_digest": preferences["authority_digest"],
        "style_version": preferences["style_version"],
        "state": "running",
        "auto_continue": True,
        "batch_size": 10,
        "batch_remaining": 0,
    }
    current_receipted_control = {
        **observed_stale_control,
        "auto_continue_receipt": {
            "schema_version": artwork_database.AUTO_CONTINUE_RECEIPT_VERSION,
            "generation_id": generation_id,
            "authority_digest": preferences["authority_digest"],
            "style_version": preferences["style_version"],
            "granted_at": datetime(2026, 9, 24, tzinfo=timezone.utc),
        },
    }
    user_ref = Reference(
        "owner-a",
        {
            artwork_database.PREFERENCES_FIELD: preferences,
            artwork_database.BACKFILL_CONTROL_FIELD: current_receipted_control,
        },
    )
    transaction = Transaction()

    result = artwork_database._pause_observed_legacy_auto_continue_control_transaction(
        transaction,
        user_ref,
        observed_control=observed_stale_control,
        expected_generation_id=generation_id,
        authority_digest=preferences["authority_digest"],
        style_version=preferences["style_version"],
        now=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )

    assert result["outcome"] == "stale"
    assert transaction.sets == []
    assert result["control"]["state"] == "running"
    assert result["control"]["auto_continue"] is True
    assert artwork_database._auto_continue_receipt_is_current(result["control"])


def test_database_claim_pauses_legacy_automatic_run_before_provider_work():
    class Snapshot:
        def __init__(self, payload):
            self.exists = True
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    class Reference:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._snapshot = Snapshot(payload)

        def get(self, transaction=None):
            return self._snapshot

    class Transaction:
        def __init__(self):
            self.sets = []
            self.updates = []

        def set(self, reference, payload, merge=False):
            self.sets.append((reference, copy.deepcopy(payload), merge))

        def update(self, reference, payload):
            self.updates.append((reference, copy.deepcopy(payload)))

    preferences = _accepted_preferences(_authority())
    generation_id = artwork_database.reconciliation_job_id(
        "owner-a",
        preferences["authority_digest"],
        preferences["style_version"],
    )
    user_ref = Reference(
        "owner-a",
        {
            artwork_database.PREFERENCES_FIELD: preferences,
            artwork_database.BACKFILL_CONTROL_FIELD: {
                "schema_version": "ella.memory_artwork.queue_control.v1",
                "generation_id": generation_id,
                "authority_digest": preferences["authority_digest"],
                "style_version": preferences["style_version"],
                "state": "running",
                "auto_continue": True,
                "batch_size": 10,
                "batch_remaining": 0,
            },
        },
    )
    job_ref = Reference(
        "job-a",
        {
            "uid": "owner-a",
            "status": "pending",
            "available_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
            "authority_digest": preferences["authority_digest"],
            "style_version": preferences["style_version"],
            "origin": artwork_database.HISTORICAL_BACKFILL_ORIGIN,
        },
    )
    transaction = Transaction()

    claimed = artwork_database._claim_job_transaction(
        transaction,
        user_ref,
        job_ref,
        lease_token="lease-a",
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        lease_seconds=600,
    )

    assert claimed is None
    assert transaction.updates == []
    assert len(transaction.sets) == 1
    control = transaction.sets[0][1][artwork_database.BACKFILL_CONTROL_FIELD]
    assert control["state"] == "paused"
    assert control["auto_continue"] is False
    assert control["batch_remaining"] == 0
    assert control["pause_reason"] == "manual_batches_required"


def test_database_claim_honors_fresh_auto_continue_receipt():
    class Snapshot:
        def __init__(self, payload):
            self.exists = True
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    class Reference:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._snapshot = Snapshot(payload)

        def get(self, transaction=None):
            return self._snapshot

    class Transaction:
        def __init__(self):
            self.sets = []
            self.updates = []

        def set(self, reference, payload, merge=False):
            self.sets.append((reference, copy.deepcopy(payload), merge))

        def update(self, reference, payload):
            self.updates.append((reference, copy.deepcopy(payload)))

    preferences = _accepted_preferences(_authority())
    generation_id = artwork_database.reconciliation_job_id(
        "owner-a",
        preferences["authority_digest"],
        preferences["style_version"],
    )
    granted_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    user_ref = Reference(
        "owner-a",
        {
            artwork_database.PREFERENCES_FIELD: preferences,
            artwork_database.BACKFILL_CONTROL_FIELD: {
                "schema_version": artwork_database.QUEUE_CONTROL_SCHEMA_VERSION,
                "generation_id": generation_id,
                "authority_digest": preferences["authority_digest"],
                "style_version": preferences["style_version"],
                "state": "running",
                "auto_continue": True,
                "batch_size": 10,
                "batch_remaining": 0,
                "auto_continue_receipt": {
                    "schema_version": artwork_database.AUTO_CONTINUE_RECEIPT_VERSION,
                    "generation_id": generation_id,
                    "authority_digest": preferences["authority_digest"],
                    "style_version": preferences["style_version"],
                    "granted_at": granted_at,
                },
            },
        },
    )
    job_ref = Reference(
        "job-a",
        {
            "uid": "owner-a",
            "status": "pending",
            "available_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
            "authority_digest": preferences["authority_digest"],
            "style_version": preferences["style_version"],
            "origin": artwork_database.HISTORICAL_BACKFILL_ORIGIN,
        },
    )
    transaction = Transaction()

    claimed = artwork_database._claim_job_transaction(
        transaction,
        user_ref,
        job_ref,
        lease_token="lease-a",
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        lease_seconds=600,
    )

    assert claimed is not None
    assert claimed["lease_token"] == "lease-a"
    assert transaction.sets == []
    assert len(transaction.updates) == 1
    assert transaction.updates[0][1]["status"] == "processing"


def test_manual_resume_restarts_only_a_failed_reconciliation_job():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))
    generation_id = started["job_id"]
    repository.reconciliation_jobs[generation_id]["status"] = "failed"

    resumed = asyncio.run(service.set_queue_control("owner-a", action="resume", generation_id=generation_id))

    assert resumed["control_state"] == "running"
    assert repository.reconciliation_jobs[generation_id]["status"] == "pending"

    repository.reconciliation_jobs[generation_id]["status"] = "completed"
    asyncio.run(service.set_queue_control("owner-a", action="pause", generation_id=generation_id))
    asyncio.run(service.set_queue_control("owner-a", action="resume", generation_id=generation_id))
    assert repository.reconciliation_jobs[generation_id]["status"] == "completed"


def test_provider_retry_backoff_is_bounded_and_not_a_tight_loop():
    assert [artwork._worker_retry_delay_seconds(attempt) for attempt in range(1, 7)] == [30, 120, 300, 900, 900, 900]


def test_prompt_is_semantically_specific_and_varies_composition_by_memory():
    garden = _terminal_memory("garden-walk")
    garden["structured"] = {
        "title": "Morning at the community garden",
        "overview": "We planted rosemary beside the blue watering can and talked about next spring.",
    }
    museum = _terminal_memory("museum-visit")
    museum["structured"] = {
        "title": "Looking at Calder sculptures",
        "overview": "A quiet afternoon studying a red mobile in the sculpture gallery.",
    }

    garden_prompt, garden_hash = artwork._prompt_for(garden, artwork.DEFAULT_STYLE_VERSION)
    museum_prompt, museum_hash = artwork._prompt_for(museum, artwork.DEFAULT_STYLE_VERSION)

    assert artwork.ARTWORK_PROMPT_CONTRACT_VERSION in garden_prompt
    assert "community garden" in garden_prompt
    assert "blue watering can" in garden_prompt
    assert "soft gouache" in garden_prompt.lower()
    assert "Do not default to a generic family gathering" in garden_prompt
    assert "Composition direction:" in garden_prompt
    assert "Light and palette direction:" in garden_prompt
    assert garden_hash != museum_hash
    assert garden_prompt != museum_prompt


def test_prompt_contract_is_deterministic_and_style_specific():
    memory = _terminal_memory("style-memory")

    first_prompt, first_hash = artwork._prompt_for(memory, "ella.memory_artwork.style.paper-collage.v1")
    second_prompt, second_hash = artwork._prompt_for(memory, "ella.memory_artwork.style.paper-collage.v1")

    assert first_prompt == second_prompt
    assert first_hash == second_hash
    assert "cut-paper collage" in first_prompt.lower()
    assert "quiet walk near a garden" in first_prompt.lower()


@pytest.mark.parametrize(
    ("style_version", "designer_style", "prompt_fragment"),
    [
        ("ella.memory_artwork.style.watercolor-journal.v1", "watercolor", "watercolor journal"),
        ("ella.memory_artwork.style.anime-storybook.v1", "anime-storybook", "anime-inspired storybook"),
        ("ella.memory_artwork.style.cinematic-still.v1", "cinematic", "cinematic editorial still"),
    ],
)
def test_expanded_styles_are_bounded_to_reviewed_designer_modes(style_version, designer_style, prompt_fragment):
    prompt, _ = artwork._prompt_for(_terminal_memory("style-memory"), style_version)

    assert artwork.DESIGNER_STYLE_NAMES[style_version] == designer_style
    assert prompt_fragment in prompt.lower()


def test_prompt_does_not_invent_time_weather_or_palette():
    memory = _terminal_memory("snowy-night")
    memory["structured"] = {
        "title": "Walking home on a snowy night",
        "overview": "Blue streetlights reflected on fresh snow beside a red scarf.",
    }

    prompt, _ = artwork._prompt_for(memory, artwork.DEFAULT_STYLE_VERSION)

    assert "snowy night" in prompt.lower()
    assert "blue streetlights" in prompt.lower()
    assert "red scarf" in prompt.lower()
    assert "morning light" not in prompt.lower()
    assert "afternoon" not in prompt.lower()
    assert "late-day" not in prompt.lower()
    assert "botanical greens" not in prompt.lower()
    assert "warm soft gouache" not in prompt.lower()


def test_prompt_does_not_invent_compositional_objects():
    memory = _terminal_memory("sea-swim")
    memory["structured"] = {
        "title": "Swimming in the sea",
        "overview": "A long swim through clear water with sunlight moving across the waves.",
    }

    prompt, _ = artwork._prompt_for(memory, artwork.DEFAULT_STYLE_VERSION)

    assert "swimming in the sea" in prompt.lower()
    assert "clear water" in prompt.lower()
    for invented_cue in ("strong path", "table edge", "shelf", "architectural line"):
        assert invented_cue not in prompt.lower()
    assert "named" in prompt.lower()


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


def test_declining_artwork_consent_erases_user_prefix_without_runtime_resolution(monkeypatch):
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    erasures = []

    def erase(uid, *, lock_proof, repository):
        erasures.append(uid)
        return 4

    async def runtime_must_not_run(uid):
        raise AssertionError("declining consent must not depend on runtime availability")

    monkeypatch.setattr(artwork, "delete_user_artwork_for_consent", erase)
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=runtime_must_not_run,
        config=_enabled_config(),
    )

    result = asyncio.run(
        service.set_preferences(
            "owner-a",
            consent="declined",
            consent_version=artwork.ARTWORK_CONSENT_VERSION,
            style_version=artwork.DEFAULT_STYLE_VERSION,
        )
    )

    assert result["consent"] == "declined"
    assert repository.preferences_by_uid["owner-a"]["consent"] == "declined"
    assert erasures == ["owner-a"]


def test_environment_owner_gate_fails_closed_before_repository_or_provider(monkeypatch):
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_RELEASE_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_BACKFILL_ENABLED", "true")
    monkeypatch.setenv(artwork.INTERNAL_OWNER_UIDS_ENV, "owner-a")

    class NeverReadRepository(FakeRepository):
        def get_preferences(self, uid):
            raise AssertionError("unauthorized owner must not reach preferences")

        def get_conversation(self, uid, memory_id):
            raise AssertionError("unauthorized owner must not reach memories")

    repository = NeverReadRepository()
    provider = FakeProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
    )

    preferences = asyncio.run(service.preferences("owner-b"))
    enqueue = asyncio.run(service.enqueue("owner-b", "memory-1"))
    signed = asyncio.run(service.signed_url("owner-b", "memory-1"))

    assert preferences["release_enabled"] is False
    assert enqueue == {"outcome": "disabled", "status": "unavailable"}
    assert signed == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "unavailable",
        "failure_code": "memory_artwork_internal_owner_required",
    }
    assert provider.calls == 0


def test_environment_owner_gate_requires_nonempty_allowlist(monkeypatch):
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_RELEASE_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_ARTWORK_PROVIDER_ENABLED", "true")
    monkeypatch.setenv(artwork.INTERNAL_OWNER_UIDS_ENV, "")

    config = artwork.MemoryArtworkConfig.from_env()

    assert config.allows_uid("owner-a") is False


@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout", "expected_lease"),
    [
        ("15", 60.0, 360),
        ("300", 300.0, 600),
        ("1200", 900.0, 1200),
        ("not-a-number", 600.0, 900),
    ],
)
def test_provider_timeout_is_bounded_and_lease_includes_completion_margin(
    monkeypatch,
    configured_timeout,
    expected_timeout,
    expected_lease,
):
    monkeypatch.setenv(artwork.PROVIDER_TIMEOUT_SECONDS_ENV, configured_timeout)

    assert artwork._provider_timeout_seconds() == expected_timeout
    assert artwork._artwork_lease_seconds() == expected_lease


def test_delayed_provider_uses_job_and_generation_leases_longer_than_request_deadline(monkeypatch):
    monkeypatch.setenv(artwork.PROVIDER_TIMEOUT_SECONDS_ENV, "300")

    class LeaseRecordingRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.job_lease_seconds = []
            self.generation_lease_seconds = []
            self.publication_lease_seconds = []

        def claim_job(self, uid, memory_id, generation_key, *, lease_token, now, lease_seconds):
            self.job_lease_seconds.append(lease_seconds)
            return super().claim_job(
                uid,
                memory_id,
                generation_key,
                lease_token=lease_token,
                now=now,
                lease_seconds=lease_seconds,
            )

        def claim_generation(self, uid, memory_id, *, generation_key, lease_token, now, lease_seconds):
            self.generation_lease_seconds.append(lease_seconds)
            return super().claim_generation(
                uid,
                memory_id,
                generation_key=generation_key,
                lease_token=lease_token,
                now=now,
                lease_seconds=lease_seconds,
            )

        def renew_publication_claim(self, uid, memory_id, generation_key, **kwargs):
            self.publication_lease_seconds.append(kwargs["lease_seconds"])
            return super().renew_publication_claim(uid, memory_id, generation_key, **kwargs)

    class DelayedProvider(FakeProvider):
        async def generate(self, **kwargs):
            await asyncio.sleep(0)
            return await super().generate(**kwargs)

    repository = LeaseRecordingRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    provider = DelayedProvider()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=lambda: provider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    reservation = asyncio.run(service.enqueue("owner-a", "memory-1"))
    generation_key = str(repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"])
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )

    result = asyncio.run(worker.run_job("owner-a", "memory-1", generation_key))

    assert reservation["status"] == "generating"
    assert result == {"outcome": "ready", "status": "ready"}
    assert repository.job_lease_seconds == [600]
    assert repository.generation_lease_seconds == [600]
    assert repository.publication_lease_seconds == [artwork.PUBLICATION_LEASE_SECONDS]
    assert provider.calls == 1


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
    assert len(store.puts) == 4
    assert {entry["rendition"] for entry in store.puts} == {"master", "w384", "w768", "w1536"}
    assert repository.storage_cleanup_required_uids == {"owner-a"}

    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-b", "memory-1"))
    assert missing.value.code == "memory_artwork_memory_not_found"
    assert len(store.signed) == 4


def test_style_refresh_keeps_previous_artwork_published_until_atomic_swap():
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

    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    first_ready = copy.deepcopy(repository.conversations[("owner-a", "memory-1")]["artwork"])

    selected_style = "ella.memory_artwork.style.paper-collage.v1"
    repository.preferences_by_uid["owner-a"]["style_version"] = selected_style
    reservation = asyncio.run(service.enqueue("owner-a", "memory-1"))

    assert reservation["status"] == "generating"
    assert repository.conversations[("owner-a", "memory-1")]["published_artwork"] == first_ready
    while_refreshing = asyncio.run(service.signed_url("owner-a", "memory-1"))
    assert while_refreshing["status"] == "ready"
    assert while_refreshing["style_version"] == artwork.DEFAULT_STYLE_VERSION
    assert while_refreshing["requested_style_version"] == selected_style
    assert while_refreshing["refresh_pending"] is True

    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    after_swap = asyncio.run(service.signed_url("owner-a", "memory-1"))
    assert after_swap["status"] == "ready"
    assert after_swap["style_version"] == selected_style
    assert after_swap["requested_style_version"] == selected_style
    assert after_swap["refresh_pending"] is False
    assert repository.conversations[("owner-a", "memory-1")]["published_artwork"] == first_ready
    assert store.deletes == []


def test_repeated_binding_refreshes_never_delete_prior_artwork_versions():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    current_authority = [_authority()]

    async def resolve_authority(_uid):
        return current_authority[0]

    repository.preferences_by_uid["owner-a"] = _accepted_preferences(current_authority[0])
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=resolve_authority,
        provider_factory=FakeProvider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    first_object_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["object_key"]

    current_authority[0] = artwork.ArtworkRuntimeAuthority(
        uid="owner-a",
        binding_id="binding-owner-a-v2",
        profile_id="profile-owner-a-v2",
        revision=8,
        authority_digest="digest-a-v2",
    )
    repository.preferences_by_uid["owner-a"] = {
        **_accepted_preferences(current_authority[0]),
        "style_version": "ella.memory_artwork.style.paper-collage.v1",
    }
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    second_object_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["object_key"]

    current_authority[0] = artwork.ArtworkRuntimeAuthority(
        uid="owner-a",
        binding_id="binding-owner-a-v3",
        profile_id="profile-owner-a-v3",
        revision=9,
        authority_digest="digest-a-v3",
    )
    repository.preferences_by_uid["owner-a"] = {
        **_accepted_preferences(current_authority[0]),
        "style_version": "ella.memory_artwork.style.cinematic-still.v1",
    }
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}

    assert first_object_key != second_object_key
    assert store.deletes == []
    assert repository.conversations[("owner-a", "memory-1")]["published_artwork"]["object_key"] == second_object_key


def test_storage_delete_failure_cannot_block_a_later_refresh():
    class CleanupStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.fail_delete = False

        def delete(self, **kwargs):
            if self.fail_delete:
                raise artwork.MemoryArtworkStorageError("storage_delete_failed")
            super().delete(**kwargs)

    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = CleanupStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    repository.preferences_by_uid["owner-a"]["style_version"] = "ella.memory_artwork.style.paper-collage.v1"
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    store.fail_delete = True
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    preserved = copy.deepcopy(repository.conversations[("owner-a", "memory-1")]["published_artwork"])
    current_object_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["object_key"]
    writes_before_retry = repository.reserve_writes

    repository.preferences_by_uid["owner-a"]["style_version"] = "ella.memory_artwork.style.cinematic-still.v1"
    assert asyncio.run(service.enqueue("owner-a", "memory-1"))["outcome"] == "reserved"
    assert repository.reserve_writes == writes_before_retry + 1
    replacement_published = repository.conversations[("owner-a", "memory-1")]["published_artwork"]
    assert replacement_published["object_key"] == current_object_key
    assert replacement_published["object_key"] != preserved["object_key"]
    assert store.deletes == []


def test_failed_style_refresh_retains_previous_ready_artwork():
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

    asyncio.run(service.enqueue("owner-a", "memory-1"))
    assert _run_claimed_process(service, repository) == {"outcome": "ready", "status": "ready"}
    repository.preferences_by_uid["owner-a"]["style_version"] = "ella.memory_artwork.style.cinematic-still.v1"
    asyncio.run(service.enqueue("owner-a", "memory-1"))
    generation_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"]
    assert repository.mark_generation_unavailable(
        "owner-a",
        "memory-1",
        generation_key=generation_key,
        failure_code="provider_unavailable",
    )

    signed = asyncio.run(service.signed_url("owner-a", "memory-1"))
    assert signed["status"] == "ready"
    assert signed["style_version"] == artwork.DEFAULT_STYLE_VERSION
    assert signed["refresh_pending"] is False
    assert signed["refresh_failure_code"] == "provider_unavailable"


def test_reconciliation_job_advances_durably_until_every_terminal_memory_is_queued():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index in range(23):
        memory_id = f"memory-{index:02d}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(
            memory_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=index),
        )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )

    started = asyncio.run(service.start_reconciliation("owner-a"))
    duplicate = asyncio.run(service.start_reconciliation("owner-a"))
    assert started["status"] == "pending"
    assert duplicate["job_id"] == started["job_id"]

    outcomes = []
    while True:
        job = repository.get_reconciliation_job("owner-a", started["job_id"])
        if job["status"] == "completed":
            break
        outcomes.append(asyncio.run(worker.run_reconciliation_job(job))["outcome"])

    status = asyncio.run(service.reconciliation_status("owner-a"))
    assert outcomes == ["continued", "continued", "completed"]
    assert status["status"] == "completed"
    assert status["pages_processed"] == 3
    assert status["scanned"] == 23
    assert status["queued"] == 23
    assert status["existing"] == 0
    assert status["skipped"] == 0
    assert len(repository.jobs) == 23


def test_reconciliation_retains_page_until_nonterminal_enrichment_recovers():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    memory = _terminal_memory("memory-1")
    memory["enrichment_state"] = {"status": "pending"}
    repository.conversations[("owner-a", "memory-1")] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    recovery_calls = []

    async def recover_enrichment(uid, memory_id):
        recovery_calls.append((uid, memory_id))
        recovered = repository.conversations[(uid, memory_id)]
        recovered["enrichment_state"] = {"status": "writeback_applied", "kind": "hermes_enriched"}
        return {"outcome": "processing"}

    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
        enrichment_recovery=recover_enrichment,
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    first = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    held = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert first == {"outcome": "enrichment_recovery_pending", "status": "pending"}
    assert recovery_calls == [("owner-a", "memory-1")]
    assert held["cursor"] is None
    assert held["pages_processed"] == 0
    assert held["scanned"] == 0
    assert repository.jobs == {}

    held["available_at"] = datetime.now(timezone.utc)
    repository.reconciliation_jobs[started["job_id"]] = held
    second = asyncio.run(worker.run_reconciliation_job(held))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert second == {"outcome": "completed", "status": "completed"}
    assert completed["pages_processed"] == 1
    assert completed["scanned"] == 1
    assert completed["queued"] == 1
    assert len(repository.jobs) == 1


def test_reconciliation_checkpoints_mixed_recovery_page_without_losing_or_double_counting():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index, memory_id in enumerate(("memory-first", "memory-second")):
        memory = _terminal_memory(
            memory_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=index),
        )
        memory["enrichment_state"] = {"status": "pending"}
        repository.conversations[("owner-a", memory_id)] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    recovery_calls = []

    async def recover_enrichment(uid, memory_id):
        recovery_calls.append((uid, memory_id))
        if memory_id == "memory-second" and recovery_calls.count((uid, memory_id)) == 1:
            return {"outcome": "processing"}
        recovered = repository.conversations[(uid, memory_id)]
        recovered["enrichment_state"] = {"status": "writeback_applied", "kind": "hermes_enriched"}
        return {"outcome": "completed"}

    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
        enrichment_recovery=recover_enrichment,
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    first = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    held = repository.get_reconciliation_job("owner-a", started["job_id"])
    assert first == {"outcome": "enrichment_recovery_pending", "status": "pending"}
    assert held["recovery_page"]["result"]["queued"] == 1
    assert held["recovery_page"]["result"]["skipped"] == 1
    assert held["recovery_page"]["memory_ids"] == ["memory-second"]

    held["available_at"] = datetime.now(timezone.utc)
    repository.reconciliation_jobs[started["job_id"]] = held
    second = asyncio.run(worker.run_reconciliation_job(held))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert second == {"outcome": "completed", "status": "completed"}
    assert completed["pages_processed"] == 1
    assert completed["scanned"] == 2
    assert completed["queued"] == 2
    assert completed["existing"] == 0
    assert completed["skipped"] == 0
    assert completed["recovery_page"] is None
    assert recovery_calls == [
        ("owner-a", "memory-first"),
        ("owner-a", "memory-second"),
        ("owner-a", "memory-second"),
    ]


def test_reconciliation_checkpoints_partial_page_before_retryable_recovery_error():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index, memory_id in enumerate(("memory-first", "memory-second")):
        memory = _terminal_memory(
            memory_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=index),
        )
        memory["enrichment_state"] = {"status": "pending"}
        repository.conversations[("owner-a", memory_id)] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    recovery_calls = []

    async def recover_enrichment(uid, memory_id):
        recovery_calls.append((uid, memory_id))
        if memory_id == "memory-second" and recovery_calls.count((uid, memory_id)) == 1:
            raise artwork.MemoryArtworkError("provider_busy", retryable=True)
        recovered = repository.conversations[(uid, memory_id)]
        recovered["enrichment_state"] = {"status": "writeback_applied", "kind": "hermes_enriched"}
        return {"outcome": "completed"}

    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
        enrichment_recovery=recover_enrichment,
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    first = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    held = repository.get_reconciliation_job("owner-a", started["job_id"])
    assert first == {"outcome": "retry", "status": "pending", "failure_code": "provider_busy"}
    assert held["attempt_count"] == 1
    assert held["recovery_page"]["result"]["queued"] == 1
    assert held["recovery_page"]["result"]["existing"] == 0
    assert held["recovery_page"]["result"]["skipped"] == 1
    assert held["recovery_page"]["memory_ids"] == ["memory-second"]

    held["available_at"] = datetime.now(timezone.utc)
    repository.reconciliation_jobs[started["job_id"]] = held
    second = asyncio.run(worker.run_reconciliation_job(held))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert second == {"outcome": "completed", "status": "completed"}
    assert completed["attempt_count"] == 0
    assert completed["pages_processed"] == 1
    assert completed["queued"] == 2
    assert completed["existing"] == 0
    assert completed["skipped"] == 0
    assert recovery_calls == [
        ("owner-a", "memory-first"),
        ("owner-a", "memory-second"),
        ("owner-a", "memory-second"),
    ]


def test_reconciliation_resets_retry_budget_after_successful_page_progress():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index in range(11):
        memory_id = f"memory-{index:02d}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(
            memory_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=index),
        )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))
    pending = repository.get_reconciliation_job("owner-a", started["job_id"])
    pending["attempt_count"] = 3
    repository.reconciliation_jobs[started["job_id"]] = pending

    result = asyncio.run(worker.run_reconciliation_job(pending))
    continued = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert result == {"outcome": "continued", "status": "pending"}
    assert continued["attempt_count"] == 0
    assert continued["pages_processed"] == 1
    assert continued["queued"] == 10


def test_reconciliation_checkpoints_reservations_when_backfill_enqueue_raises():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index, memory_id in enumerate(("memory-first", "memory-second")):
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(
            memory_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=index),
        )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    enqueue = service.enqueue
    attempts = []

    async def flaky_enqueue(uid, memory_id, **kwargs):
        attempts.append((uid, memory_id))
        if memory_id == "memory-second" and attempts.count((uid, memory_id)) == 1:
            raise artwork.MemoryArtworkError("authority_unavailable", retryable=True)
        return await enqueue(uid, memory_id, **kwargs)

    service.enqueue = flaky_enqueue
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    first = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    held = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert first == {"outcome": "retry", "status": "pending", "failure_code": "authority_unavailable"}
    assert held["recovery_page"]["result"]["queued"] == 1
    assert held["recovery_page"]["result"]["existing"] == 0
    assert held["recovery_page"]["result"]["skipped"] == 1
    assert held["recovery_page"]["memory_ids"] == ["memory-second"]

    held["available_at"] = datetime.now(timezone.utc)
    repository.reconciliation_jobs[started["job_id"]] = held
    second = asyncio.run(worker.run_reconciliation_job(held))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert second == {"outcome": "completed", "status": "completed"}
    assert completed["attempt_count"] == 0
    assert completed["pages_processed"] == 1
    assert completed["scanned"] == 2
    assert completed["queued"] == 2
    assert completed["existing"] == 0
    assert completed["skipped"] == 0


def test_reconciliation_rejects_creation_when_provider_worker_is_disabled():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(provider=False),
    )

    with pytest.raises(artwork.MemoryArtworkError, match="memory_artwork_backfill_disabled"):
        asyncio.run(service.start_reconciliation("owner-a"))

    assert repository.reconciliation_jobs == {}


@pytest.mark.parametrize(
    ("queue_before_return", "expected_queued", "expected_existing"),
    [(False, 1, 0), (True, 0, 1)],
)
def test_reconciliation_reclassifies_recovered_memory_after_enqueue(
    queue_before_return, expected_queued, expected_existing
):
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    memory = _terminal_memory("memory-1")
    memory["enrichment_state"] = {"status": "pending"}
    repository.conversations[("owner-a", "memory-1")] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    async def recover_enrichment(uid, memory_id):
        recovered = repository.conversations[(uid, memory_id)]
        recovered["enrichment_state"] = {"status": "writeback_applied", "kind": "hermes_enriched"}
        if queue_before_return:
            assert (await service.enqueue(uid, memory_id))["outcome"] == "reserved"
        return {"outcome": "completed"}

    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
        enrichment_recovery=recover_enrichment,
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    result = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert result == {"outcome": "completed", "status": "completed"}
    assert completed["scanned"] == 1
    assert completed["queued"] == expected_queued
    assert completed["existing"] == expected_existing
    assert completed["skipped"] == 0
    assert len(repository.jobs) == 1


@pytest.mark.parametrize("terminal_outcome", ["not_found", "invalid_state", "not_retryable", "failed"])
def test_reconciliation_advances_past_terminal_enrichment_recovery_outcomes(terminal_outcome):
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    pending = _terminal_memory("memory-pending", created_at=datetime.now(timezone.utc))
    pending["enrichment_state"] = {"status": "pending"}
    repository.conversations[("owner-a", "memory-pending")] = pending
    repository.conversations[("owner-a", "memory-older")] = _terminal_memory(
        "memory-older",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    async def recover_enrichment(uid, memory_id):
        assert (uid, memory_id) == ("owner-a", "memory-pending")
        return {"outcome": terminal_outcome}

    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
        enrichment_recovery=recover_enrichment,
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))

    result = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))
    completed = repository.get_reconciliation_job("owner-a", started["job_id"])

    assert result == {"outcome": "completed", "status": "completed"}
    assert completed["pages_processed"] == 1
    assert completed["scanned"] == 2
    assert completed["queued"] == 1
    assert completed["skipped"] == 1
    assert len(repository.jobs) == 1
    assert next(iter(repository.jobs.values()))["memory_id"] == "memory-older"


def test_reconciliation_claim_deletes_stale_job_for_deleted_owner():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))
    repository.deletion_pending.add("owner-a")
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )

    result = asyncio.run(worker.run_reconciliation_job(repository.get_reconciliation_job("owner-a", started["job_id"])))

    assert result == {"outcome": "not_claimed", "status": "unavailable"}
    assert started["job_id"] not in repository.reconciliation_jobs


def test_reconciliation_fails_closed_when_style_authority_changes_before_worker_claim():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        store_factory=FakeStore,
        config=_enabled_config(),
    )
    worker = artwork.MemoryArtworkWorker(
        repository=repository,
        service_factory=lambda: service,
        config=_enabled_config(),
    )
    started = asyncio.run(service.start_reconciliation("owner-a"))
    repository.preferences_by_uid["owner-a"]["style_version"] = "ella.memory_artwork.style.paper-collage.v1"

    job = repository.get_reconciliation_job("owner-a", started["job_id"])
    result = asyncio.run(worker.run_reconciliation_job(job))

    assert result == {
        "outcome": "failed",
        "status": "failed",
        "failure_code": "memory_artwork_preference_authority_stale",
    }
    assert repository.jobs == {}


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

    if drift == "style":
        conversation = repository.get_conversation("owner-a", "memory-1") or {}
        generation_key = str(((conversation.get("artwork") or {}).get("generation_key") or ""))
        assert (
            repository.claim_job(
                "owner-a",
                "memory-1",
                generation_key,
                lease_token="stale-style-lease",
                now=datetime.now(timezone.utc),
                lease_seconds=120,
            )
            is None
        )
        assert provider.calls == 0
        return

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
    assert repository.storage_cleanup_required_uids == {"owner-a"}


def test_cleanup_marker_failure_prevents_object_upload():
    class FailingMarkerRepository(FakeRepository):
        def renew_publication_claim(
            self,
            uid,
            memory_id,
            generation_key,
            *,
            generation_lease_token,
            job_lease_token,
            now,
            lease_seconds,
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
        if calls < 4:
            return _authority(uid, "digest-a")
        return artwork.ArtworkRuntimeAuthority(
            uid=uid,
            binding_id="other-binding",
            profile_id="other-profile",
            revision=99,
            authority_digest="digest-b",
        )

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


def test_expired_claim_after_object_write_never_deletes_shared_idempotent_object():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    class ClaimExpiringStore(FakeStore):
        def put(self, **kwargs):
            stored = super().put(**kwargs)
            next(iter(repository.jobs.values()))["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            return stored

    store = ClaimExpiringStore()
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

    assert failure.value.code == "memory_artwork_job_claim_invalid"
    assert len(store.puts) == 4
    assert store.deletes == []


def test_deletion_marker_after_cleanup_reservation_blocks_object_upload():
    class DeletingRepository(FakeRepository):
        def renew_publication_claim(self, uid, memory_id, generation_key, **kwargs):
            result = super().renew_publication_claim(uid, memory_id, generation_key, **kwargs)
            if result:
                self.deletion_pending.add(uid)
            return result

    repository = DeletingRepository()
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
        ({"style_version": "ella.memory_artwork.style.paper-collage.v1"}, "ready_stale"),
        ({"binding_id": "other-binding"}, "authority_stale"),
        ({"profile_id": "other-profile"}, "authority_stale"),
        ({"authority_digest": "other-digest"}, "ready"),
    ],
)
def test_signed_url_requires_current_bound_consent(preference_change, expected):
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["artwork"] = _ready_artwork(
        memory,
        authority=_authority(),
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
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
    elif expected == "authority_stale":
        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        assert result == {
            "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
            "status": "unavailable",
            "failure_code": "memory_artwork_preference_authority_stale",
        }
    else:
        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        assert result["status"] == "ready"
        assert result["stale"] is (expected == "ready_stale")
    assert bool(store.signed) is expected.startswith("ready")


def test_signed_url_rechecks_sensitive_source_before_release(monkeypatch):
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

    def erase_sensitive(uid, memory_id, conversation, *, lock_proof, store):
        store.delete_memory_all_bindings(uid=uid, memory_id=memory_id)

    monkeypatch.setattr(artwork, "delete_memory_artwork_for_exclusion", erase_sensitive)
    result = asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert result == {
        "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
        "status": "unavailable",
        "failure_code": "memory_artwork_sensitive_source_excluded",
    }
    assert store.signed == []
    assert store.prefix_deletes == [{"uid": "owner-a", "memory_id": "memory-1"}]

    for drift in ("enrichment_revision", "prompt", "discarded"):
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
        else:
            conversation["discarded"] = True

        result = asyncio.run(service.signed_url("owner-a", "memory-1"))
        if drift == "discarded":
            assert result == {
                "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_discarded",
            }
            assert store.signed == []
        else:
            assert result["status"] == "ready"
            assert result["stale"] is True
            assert store.signed


def test_signed_url_rejects_conversation_deletion_before_signing():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    memory["deletion_pending"] = True
    memory["artwork"] = _ready_artwork(
        memory,
        authority=_authority(),
        style_version=artwork.DEFAULT_STYLE_VERSION,
    )
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert failure.value.code == "memory_artwork_deletion_pending"
    assert store.signed == []


def test_signed_url_does_not_depend_on_runtime_resolution_and_honors_declined_consent():
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
    repository.preferences_by_uid["owner-a"] = {
        **_accepted_preferences(_authority()),
        "consent": "declined",
    }
    store = FakeStore()

    async def never_resolve(uid):
        raise AssertionError("permanent artwork reads must not depend on runtime health or credentials")

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=never_resolve,
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


def test_backfill_advances_past_existing_artwork_with_bounded_cursor(monkeypatch):
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
    second = asyncio.run(service.backfill("owner-a", cursor_memory_id=first["next_cursor"]))
    assert first["queued"] == 10
    assert first["existing"] == 0
    assert first["has_more"] is True
    assert second["queued"] == 3
    assert second["existing"] == 0
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert first["memory_ids"] == [f"memory-{index:02d}" for index in range(12, 2, -1)]
    assert second["memory_ids"] == ["memory-02", "memory-01", "memory-00"]
    assert repository.reserve_writes == 13
    assert len(repository.jobs) == 13

    retry = asyncio.run(service.backfill("owner-a"))
    assert retry["queued"] == 0
    assert retry["existing"] == 13


def test_preview_backfill_is_limited_to_three_recent_memory_days():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for day in range(5):
        for item in range(2):
            memory_id = f"day-{day}-memory-{item}"
            repository.conversations[("owner-a", memory_id)] = _terminal_memory(
                memory_id,
                created_at=datetime(2026, 8, 30 - day, 12, item, tzinfo=timezone.utc),
            )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        config=_enabled_config(),
    )

    preview = asyncio.run(service.backfill("owner-a", origin=artwork.PREVIEW_BACKFILL_ORIGIN))
    older = asyncio.run(
        service.backfill(
            "owner-a",
            cursor_memory_id=preview["next_cursor"],
            origin=artwork.HISTORICAL_BACKFILL_ORIGIN,
        )
    )

    assert preview["preview_day_limit"] == 3
    assert preview["preview_days"] == 3
    assert preview["queued"] == 6
    assert preview["memory_ids"] == [
        "day-0-memory-1",
        "day-0-memory-0",
        "day-1-memory-1",
        "day-1-memory-0",
        "day-2-memory-1",
        "day-2-memory-0",
    ]
    assert preview["has_more"] is True
    assert older["memory_ids"] == ["day-3-memory-1", "day-3-memory-0", "day-4-memory-1", "day-4-memory-0"]


def test_libraries_count_only_ready_owner_bound_objects_by_style_and_day():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    anime = "ella.memory_artwork.style.anime-storybook.v1"
    memories = [
        ("memory-1", datetime(2026, 8, 30, 9, tzinfo=timezone.utc), anime),
        ("memory-2", datetime(2026, 8, 30, 14, tzinfo=timezone.utc), anime),
        ("memory-3", datetime(2026, 8, 29, 9, tzinfo=timezone.utc), artwork.DEFAULT_STYLE_VERSION),
    ]
    for memory_id, created_at, style_version in memories:
        memory = _terminal_memory(memory_id, created_at=created_at)
        memory["artwork"] = _ready_artwork(memory, authority=authority, style_version=style_version)
        repository.conversations[("owner-a", memory_id)] = memory
    stale = _terminal_memory("memory-stale")
    stale["artwork"] = _ready_artwork(stale, authority=authority, style_version=anime)
    stale["artwork"]["authority_digest"] = "other-owner"
    repository.conversations[("owner-a", "memory-stale")] = stale
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )

    result = asyncio.run(service.libraries("owner-a"))
    by_style = {library["style_version"]: library for library in result["libraries"]}

    assert result["schema_version"] == artwork.ARTWORK_LIBRARIES_SCHEMA_VERSION
    assert result["default_preview_days"] == 3
    assert result["historical_batch_size"] == 10
    assert by_style[anime]["ready_memories"] == 3
    assert by_style[anime]["ready_days"] == 2
    assert by_style[artwork.DEFAULT_STYLE_VERSION]["ready_memories"] == 1
    assert by_style[artwork.DEFAULT_STYLE_VERSION]["ready_days"] == 1
    assert sum(library["ready_memories"] for library in result["libraries"]) == 4


def test_libraries_exclude_retained_ready_objects_that_signed_url_cannot_display():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    memories = {}
    for suffix in ("ready", "discarded", "deleting", "sensitive", "source-stale"):
        memory = _terminal_memory(f"memory-{suffix}")
        memory["artwork"] = _ready_artwork(memory, authority=authority, style_version=artwork.DEFAULT_STYLE_VERSION)
        memories[suffix] = memory
    memories["discarded"]["discarded"] = True
    memories["deleting"]["deletion_pending"] = True
    memories["sensitive"]["ella_tags"] = ["caregiver-private"]
    memories["source-stale"]["structured"]["overview"] = "The source changed after this artwork was created."
    for suffix, memory in memories.items():
        repository.conversations[("owner-a", f"memory-{suffix}")] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        config=_enabled_config(),
    )

    result = asyncio.run(service.libraries("owner-a"))
    selected = next(library for library in result["libraries"] if library["selected"])

    assert selected["ready_memories"] == 2
    assert selected["ready_days"] == 1


def test_day_artwork_batches_only_the_requested_local_calendar_day():
    repository = FakeRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    early_utc = _terminal_memory("early", created_at=datetime(2026, 9, 25, 1, tzinfo=timezone.utc))
    midday_utc = _terminal_memory("midday", created_at=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    for memory in (early_utc, midday_utc):
        memory["artwork"] = {
            **_ready_artwork(memory, authority=authority, style_version=artwork.DEFAULT_STYLE_VERSION),
            "generation_key": hashlib.sha256(memory["id"].encode()).hexdigest(),
        }
        repository.conversations[("owner-a", memory["id"])] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: (_ for _ in ()).throw(AssertionError("read must not resolve runtime")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    result = asyncio.run(service.day_artwork("owner-a", "2026-09-24", utc_offset_minutes=-420))

    assert result["day"] == "2026-09-24"
    assert result["utc_offset_minutes"] == -420
    assert [item["memory_id"] for item in result["items"]] == ["early"]
    assert result["items"][0]["artwork"]["status"] == "ready"


def test_day_artwork_paginates_past_two_hundred_newer_memories():
    class PagingRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.page_calls = 0

        def list_conversations_page(self, uid, *, limit, cursor_memory_id=None):
            self.page_calls += 1
            return super().list_conversations_page(
                uid,
                limit=limit,
                cursor_memory_id=cursor_memory_id,
            )

    repository = PagingRepository()
    authority = _authority()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(authority)
    target = _terminal_memory("target", created_at=datetime(2026, 1, 15, 12, tzinfo=timezone.utc))
    target["artwork"] = {
        **_ready_artwork(target, authority=authority, style_version=artwork.DEFAULT_STYLE_VERSION),
        "generation_key": "a" * 64,
    }
    repository.conversations[("owner-a", "target")] = target
    for index in range(201):
        memory_id = f"newer-{index:03d}"
        repository.conversations[("owner-a", memory_id)] = _terminal_memory(
            memory_id,
            created_at=datetime(2026, 2, 1, 12, tzinfo=timezone.utc) + timedelta(minutes=index),
        )
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=lambda uid: (_ for _ in ()).throw(AssertionError("read must not resolve runtime")),
        store_factory=FakeStore,
        config=_enabled_config(),
    )

    result = asyncio.run(service.day_artwork("owner-a", "2026-01-15", utc_offset_minutes=0))

    assert repository.page_calls == 2
    assert [item["memory_id"] for item in result["items"]] == ["target"]


def test_backfill_limits_enrichment_recovery_candidates_and_rejects_stale_cursor():
    repository = FakeRepository()
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    for index in range(6):
        memory_id = f"memory-{index}"
        memory = _terminal_memory(memory_id, created_at=datetime(2026, 8, 22, 12, index, tzinfo=timezone.utc))
        memory["enrichment_state"] = {"status": "pending"}
        repository.conversations[("owner-a", memory_id)] = memory
    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=_resolver,
        provider_factory=FakeProvider,
        config=_enabled_config(),
    )

    result = asyncio.run(service.backfill("owner-a"))
    remaining = asyncio.run(service.backfill("owner-a", cursor_memory_id=result["next_cursor"]))
    assert result["queued"] == 0
    assert result["skipped"] == 3
    assert result["_recovery_memory_ids"] == ["memory-5", "memory-4", "memory-3"]
    assert result["next_cursor"] == "memory-3"
    assert result["has_more"] is True
    assert remaining["skipped"] == 3
    assert remaining["_recovery_memory_ids"] == ["memory-2", "memory-1", "memory-0"]
    assert remaining["next_cursor"] is None
    assert remaining["has_more"] is False

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(service.backfill("owner-a", cursor_memory_id="missing-memory"))
    assert failure.value.code == "memory_artwork_backfill_cursor_invalid"


def test_firestore_backfill_cursor_is_owner_scoped_and_uses_snapshot(monkeypatch):

    class Snapshot:
        id = "memory-visible"

        def to_dict(self):
            return {"id": "caller-supplied-id", "discarded": False}

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

        def start_after(self, snapshot):
            self.operations.append(("start_after", snapshot.id))
            return self

        def stream(self):
            return iter([Snapshot()])

    query = Query()

    class CursorDocument:
        def __init__(self, memory_id):
            self.id = memory_id
            self.exists = True

        def get(self):
            return self

    class Conversations:
        def collection(self, name):
            assert name == "conversations"
            return self

        def where(self, field, operator, value):
            return query.where(field, operator, value)

        def document(self, memory_id):
            assert memory_id == "memory-cursor"
            return CursorDocument(memory_id)

    class Users:
        def document(self, uid):
            assert uid == "owner-a"
            return Conversations()

    class Database:
        def collection(self, name):
            assert name == "users"
            return Users()

    monkeypatch.setattr(artwork_database, "db", Database())
    assert artwork_database.list_conversations_page(
        "owner-a",
        limit=10,
        cursor_memory_id="memory-cursor",
    ) == [{"id": "memory-visible", "discarded": False}]
    assert query.operations == [
        ("where", "discarded", "==", False),
        ("order_by", "created_at", artwork_database.firestore.Query.DESCENDING),
        ("start_after", "memory-cursor"),
        ("limit", 10),
    ]


def test_absent_artwork_preferences_are_not_made_truthy_by_false_housekeeping_flags(monkeypatch):
    class Snapshot:
        exists = True

        def to_dict(self):
            return {
                artwork_database.STORAGE_CLEANUP_REQUIRED_FIELD: False,
                artwork_database.DELETION_PENDING_FIELD: False,
            }

    class User:
        def get(self):
            return Snapshot()

    class Users:
        def document(self, uid):
            assert uid == "owner-a"
            return User()

    class Database:
        def collection(self, name):
            assert name == "users"
            return Users()

    monkeypatch.setattr(artwork_database, "db", Database())

    assert artwork_database.get_preferences("owner-a") == {}


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
                image_bytes=_valid_test_image_bytes(),
                content_type="image/jpeg",
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
                image_bytes=_valid_test_image_bytes(),
                content_type="image/jpeg",
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
    publication_now = datetime.now(timezone.utc)
    reference.state[artwork_database.ARTWORK_FIELD]["lease_expires_at"] = publication_now - timedelta(seconds=1)
    job_reference.state["lease_expires_at"] = publication_now - timedelta(seconds=1)
    assert (
        artwork_database._renew_publication_claim_transaction(
            transaction,
            user_reference,
            reference,
            job_reference,
            generation_key="a" * 64,
            generation_lease_token="lease-a",
            job_lease_token="lease-a",
            now=publication_now,
            lease_seconds=600,
        )
        is True
    )
    publication_expiry = publication_now + timedelta(seconds=600)
    assert user_reference.state[artwork_database.STORAGE_CLEANUP_REQUIRED_FIELD] is True
    assert reference.state[artwork_database.ARTWORK_FIELD]["lease_expires_at"] == publication_expiry
    assert job_reference.state["lease_expires_at"] == publication_expiry
    updates_before_deletion_rejection = transaction.updates
    user_reference.state[artwork_database.DELETION_PENDING_FIELD] = True
    assert (
        artwork_database._renew_publication_claim_transaction(
            transaction,
            user_reference,
            reference,
            job_reference,
            generation_key="a" * 64,
            generation_lease_token="lease-a",
            job_lease_token="lease-a",
            now=publication_now,
            lease_seconds=600,
        )
        is False
    )
    assert transaction.updates == updates_before_deletion_rejection
    user_reference.state[artwork_database.DELETION_PENDING_FIELD] = False
    reference.state["deletion_pending"] = True
    assert (
        artwork_database._renew_publication_claim_transaction(
            transaction,
            user_reference,
            reference,
            job_reference,
            generation_key="a" * 64,
            generation_lease_token="lease-a",
            job_lease_token="lease-a",
            now=publication_now,
            lease_seconds=600,
        )
        is False
    )
    assert transaction.updates == updates_before_deletion_rejection
    reference.state["deletion_pending"] = False
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


def test_firestore_permanent_restore_is_transactional_and_runtime_failure_only():
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
        def __init__(self):
            self.updates = 0

        def update(self, reference, payload):
            self.updates += 1
            reference.state.update(copy.deepcopy(payload))

        def set(self, reference, payload, merge=False):
            self.updates += 1
            if merge:
                reference.state.update(copy.deepcopy(payload))
            else:
                reference.state = copy.deepcopy(payload)

    preferences = {
        "consent": "accepted",
        "consent_version": artwork.ARTWORK_CONSENT_VERSION,
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "authority_digest": "legacy-runtime-fingerprint",
    }
    user_ref = Reference({artwork_database.PREFERENCES_FIELD: preferences})
    conversation_ref = Reference(
        {
            artwork_database.ARTWORK_FIELD: {
                "status": "unavailable",
                "failure_code": "authority_changed",
                "binding_id": "binding-owner-a",
                "profile_id": "profile-owner-a",
                "authority_digest": "legacy-runtime-fingerprint",
                "object_key": "private/object.jpg",
                "object_generation": "7",
            }
        }
    )
    transaction = Transaction()

    result = artwork_database._restore_permanent_artwork_transaction(
        transaction,
        user_ref,
        conversation_ref,
        binding_id="binding-owner-a",
        profile_id="profile-owner-a",
        authority_digest="stable-owner-profile-digest",
        consent_version=artwork.ARTWORK_CONSENT_VERSION,
    )

    assert result["outcome"] == "restored"
    assert conversation_ref.state[artwork_database.ARTWORK_FIELD]["status"] == "ready"
    assert "failure_code" not in conversation_ref.state[artwork_database.ARTWORK_FIELD]
    assert user_ref.state[artwork_database.PREFERENCES_FIELD]["authority_digest"] == ("stable-owner-profile-digest")
    assert transaction.updates == 2

    conversation_ref.state[artwork_database.ARTWORK_FIELD].update(
        {"status": "unavailable", "failure_code": "source_changed"}
    )
    denied = artwork_database._restore_permanent_artwork_transaction(
        Transaction(),
        user_ref,
        conversation_ref,
        binding_id="binding-owner-a",
        profile_id="profile-owner-a",
        authority_digest="stable-owner-profile-digest",
        consent_version=artwork.ARTWORK_CONSENT_VERSION,
    )
    assert denied["outcome"] == "not_restorable"

    conversation_ref.state[artwork_database.ARTWORK_FIELD].update(
        {"status": "unavailable", "failure_code": "authority_changed"}
    )
    user_ref.state[artwork_database.PREFERENCES_FIELD]["consent"] = "declined"
    declined_transaction = Transaction()
    declined = artwork_database._restore_permanent_artwork_transaction(
        declined_transaction,
        user_ref,
        conversation_ref,
        binding_id="binding-owner-a",
        profile_id="profile-owner-a",
        authority_digest="stable-owner-profile-digest",
        consent_version=artwork.ARTWORK_CONSENT_VERSION,
    )
    assert declined["outcome"] == "blocked"
    assert conversation_ref.state[artwork_database.ARTWORK_FIELD]["status"] == "unavailable"
    assert declined_transaction.updates == 0

    user_ref.state[artwork_database.PREFERENCES_FIELD]["consent"] = "accepted"
    conversation_ref.state["deletion_pending"] = True
    deleting_transaction = Transaction()
    deleting = artwork_database._restore_permanent_artwork_transaction(
        deleting_transaction,
        user_ref,
        conversation_ref,
        binding_id="binding-owner-a",
        profile_id="profile-owner-a",
        authority_digest="stable-owner-profile-digest",
        consent_version=artwork.ARTWORK_CONSENT_VERSION,
    )
    assert deleting["outcome"] == "blocked"
    assert conversation_ref.state[artwork_database.ARTWORK_FIELD]["status"] == "unavailable"
    assert deleting_transaction.updates == 0


def test_firestore_authority_stabilization_preserves_latest_consent_and_style():
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
            self.writes = 0

        def set(self, reference, payload, merge=False):
            self.writes += 1
            assert merge is True
            reference.state.update(copy.deepcopy(payload))

    latest_preferences = {
        "consent": "declined",
        "consent_version": artwork.ARTWORK_CONSENT_VERSION,
        "style_version": "ella.memory_artwork.style.paper-collage.v1",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
        "authority_digest": "legacy-runtime-digest",
    }
    user_ref = Reference({artwork_database.PREFERENCES_FIELD: latest_preferences})
    transaction = Transaction()

    result = artwork_database._stabilize_preferences_authority_transaction(
        transaction,
        user_ref,
        binding_id="binding-owner-a",
        profile_id="profile-owner-a",
        authority_digest="stable-owner-profile-digest",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result["consent"] == "declined"
    assert result["style_version"] == "ella.memory_artwork.style.paper-collage.v1"
    assert result["authority_digest"] == "stable-owner-profile-digest"
    assert user_ref.state[artwork_database.PREFERENCES_FIELD]["consent"] == "declined"
    assert user_ref.state[artwork_database.PREFERENCES_FIELD]["style_version"] == (
        "ella.memory_artwork.style.paper-collage.v1"
    )
    assert transaction.writes == 1


def test_firestore_terminal_reservation_promotes_existing_pending_historical_job():
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

    generation_key = "a" * 64
    conversation = _terminal_memory("memory-1")
    conversation[artwork_database.ARTWORK_FIELD] = {
        "status": "generating",
        "generation_key": generation_key,
        "enrichment_revision": "summary-memory-1",
    }
    job_ref = Reference(
        {
            "status": "pending",
            "uid": "owner-a",
            "memory_id": "memory-1",
            "generation_key": generation_key,
            "origin": artwork.HISTORICAL_BACKFILL_ORIGIN,
        }
    )
    now = datetime.now(timezone.utc)

    result = artwork_database._reserve_generation_transaction(
        Transaction(),
        Reference({"id": "owner-a"}),
        Reference(conversation),
        enrichment_revision="summary-memory-1",
        generation_key=generation_key,
        artwork_state=conversation[artwork_database.ARTWORK_FIELD],
        job_ref=job_ref,
        job_state={
            **job_ref.state,
            "origin": artwork.TERMINAL_ENRICHMENT_ORIGIN,
            "updated_at": now,
        },
    )

    assert result["outcome"] == "existing"
    assert job_ref.state["origin"] == artwork.TERMINAL_ENRICHMENT_ORIGIN
    assert job_ref.state["updated_at"] == now


def test_firestore_preview_reservation_promotes_existing_pending_historical_job():
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

    generation_key = "b" * 64
    conversation = _terminal_memory("memory-1")
    conversation[artwork_database.ARTWORK_FIELD] = {
        "status": "generating",
        "generation_key": generation_key,
        "enrichment_revision": "summary-memory-1",
    }
    job_ref = Reference(
        {
            "status": "pending",
            "uid": "owner-a",
            "memory_id": "memory-1",
            "generation_key": generation_key,
            "origin": artwork.HISTORICAL_BACKFILL_ORIGIN,
        }
    )
    now = datetime.now(timezone.utc)

    result = artwork_database._reserve_generation_transaction(
        Transaction(),
        Reference({"id": "owner-a"}),
        Reference(conversation),
        enrichment_revision="summary-memory-1",
        generation_key=generation_key,
        artwork_state=conversation[artwork_database.ARTWORK_FIELD],
        job_ref=job_ref,
        job_state={
            **job_ref.state,
            "origin": artwork.PREVIEW_BACKFILL_ORIGIN,
            "updated_at": now,
        },
    )

    assert result["outcome"] == "existing"
    assert job_ref.state["origin"] == artwork.PREVIEW_BACKFILL_ORIGIN
    assert "available_at" not in job_ref.state
    assert job_ref.state["updated_at"] == now


def test_firestore_style_refresh_keeps_published_art_until_atomic_finalize():
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
            for key, value in payload.items():
                if value is artwork_database.firestore.DELETE_FIELD:
                    reference.state.pop(key, None)
                else:
                    reference.state[key] = copy.deepcopy(value)

        def set(self, reference, payload):
            self.operations.append(("set", reference, copy.deepcopy(payload)))
            reference.state = copy.deepcopy(payload)

    published = {
        "status": "ready",
        "generation_key": "a" * 64,
        "authority_digest": "digest-a",
        "binding_id": "binding-a",
        "profile_id": "profile-a",
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "object_key": "private/old.png",
        "prompt_sha256": "b" * 64,
    }
    conversation = _terminal_memory("memory-1")
    conversation[artwork_database.ARTWORK_FIELD] = copy.deepcopy(published)
    conversation_ref = Reference(conversation)
    transaction = Transaction()
    generation = {
        "status": "generating",
        "generation_key": "c" * 64,
        "authority_digest": "digest-a",
        "binding_id": "binding-a",
        "profile_id": "profile-a",
        "style_version": "ella.memory_artwork.style.anime-storybook.v1",
        "enrichment_revision": "summary-memory-1",
    }

    reserved = artwork_database._reserve_generation_transaction(
        transaction,
        Reference({"id": "owner-a"}),
        conversation_ref,
        enrichment_revision="summary-memory-1",
        generation_key="c" * 64,
        artwork_state=generation,
    )

    assert reserved["outcome"] == "reserved"
    assert conversation_ref.state[artwork_database.ARTWORK_FIELD] == generation
    assert conversation_ref.state[artwork_database.PUBLISHED_ARTWORK_FIELD] == published

    conversation_ref.state[artwork_database.ARTWORK_FIELD].update(
        {"lease_token": "lease-c", "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1)}
    )
    replacement = {
        **generation,
        "status": "ready",
        "object_key": "private/new.png",
        "prompt_sha256": "d" * 64,
    }
    assert artwork_database._finalize_generation_transaction(
        transaction,
        conversation_ref,
        generation_key="c" * 64,
        authority_digest="digest-a",
        lease_token="lease-c",
        ready_state=replacement,
    )
    assert conversation_ref.state[artwork_database.ARTWORK_FIELD] == replacement
    assert conversation_ref.state[artwork_database.PUBLISHED_ARTWORK_FIELD] == published
    assert artwork_database._clear_published_artwork_transaction(
        transaction,
        conversation_ref,
        object_key="private/old.png",
        object_generation="",
    )
    assert artwork_database.PUBLISHED_ARTWORK_FIELD not in conversation_ref.state


@pytest.mark.parametrize("terminal_job_status", ("failed", "completed"))
def test_firestore_automatic_reservation_does_not_reopen_terminal_generating_job(terminal_job_status):
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
        def __init__(self):
            self.operations = []

        def update(self, reference, payload):
            self.operations.append(("update", reference, payload))

        def set(self, reference, payload):
            self.operations.append(("set", reference, payload))

    generation_key = "a" * 64
    conversation = _terminal_memory("memory-1")
    conversation["artwork"] = {
        "status": "generating",
        "generation_key": generation_key,
        "enrichment_revision": "summary-memory-1",
    }
    transaction = Transaction()
    job = {"status": terminal_job_status, "attempt_count": 5}

    result = artwork_database._reserve_generation_transaction(
        transaction,
        Reference({"id": "owner-a"}),
        Reference(conversation),
        enrichment_revision="summary-memory-1",
        generation_key=generation_key,
        artwork_state={"status": "generating", "generation_key": generation_key},
        job_ref=Reference(job),
        job_state={"status": "pending", "attempt_count": 0},
        allow_retry=False,
    )

    assert result["outcome"] == "automatic_attempt_already_used"
    assert job == {"status": terminal_job_status, "attempt_count": 5}
    assert transaction.operations == []


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


def test_pending_job_query_filters_control_before_applying_worker_limit(monkeypatch):
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
                {
                    "status": "pending",
                    "uid": "owner-a",
                    "memory_id": "memory-1",
                    "authority_digest": "digest-a",
                    "style_version": artwork.DEFAULT_STYLE_VERSION,
                    "origin": artwork.TERMINAL_ENRICHMENT_ORIGIN,
                    "available_at": now - timedelta(minutes=2),
                },
            )
        ]
    )
    processing_query = Query(
        [
            Snapshot(
                "processing-first",
                {
                    "status": "processing",
                    "uid": "owner-a",
                    "memory_id": "memory-2",
                    "authority_digest": "digest-a",
                    "style_version": artwork.DEFAULT_STYLE_VERSION,
                    "origin": artwork.TERMINAL_ENRICHMENT_ORIGIN,
                    "lease_expires_at": now - timedelta(minutes=1),
                },
            )
        ]
    )

    class Database:
        def collection(self, name):
            if name == artwork_database.JOB_COLLECTION:
                return Collection()
            assert name == "users"
            return UserCollection()

    class Collection:
        def where(self, field, operator, value):
            query = pending_query if value == "pending" else processing_query
            return query.where(field, operator, value)

    class UserDocument:
        id = "owner-a"

        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {artwork_database.PREFERENCES_FIELD: _accepted_preferences(_authority())},
            )

    class UserCollection:
        def document(self, uid):
            assert uid == "owner-a"
            return UserDocument()

    monkeypatch.setattr(artwork_database, "db", Database())

    jobs = artwork_database.list_pending_jobs(limit=2, now=now)

    assert [job["job_id"] for job in jobs] == ["pending-first", "processing-first"]
    assert ("where", "origin", "==", artwork_database.TERMINAL_ENRICHMENT_ORIGIN) in pending_query.operations
    assert ("where", "origin", "==", artwork_database.PREVIEW_BACKFILL_ORIGIN) in pending_query.operations
    assert ("where", "origin", "==", artwork_database.HISTORICAL_BACKFILL_ORIGIN) in pending_query.operations
    assert ("where", "available_at", "<=", now) in pending_query.operations
    assert ("order_by", "available_at", artwork_database.firestore.Query.ASCENDING) in pending_query.operations
    assert ("where", "lease_expires_at", "<=", now) in processing_query.operations
    assert ("order_by", "lease_expires_at", artwork_database.firestore.Query.ASCENDING) in processing_query.operations
    indexes = json.loads((BACKEND_ROOT.parent / "firestore.indexes.json").read_text())["indexes"]
    artwork_indexes = {
        tuple((field["fieldPath"], field["order"]) for field in index["fields"])
        for index in indexes
        if index.get("collectionGroup") == artwork_database.JOB_COLLECTION
    }
    assert (
        ("status", "ASCENDING"),
        ("origin", "ASCENDING"),
        ("available_at", "ASCENDING"),
    ) in artwork_indexes
    assert (
        ("status", "ASCENDING"),
        ("origin", "ASCENDING"),
        ("lease_expires_at", "ASCENDING"),
    ) in artwork_indexes
    assert (
        ("status", "ASCENDING"),
        ("available_at", "ASCENDING"),
    ) in artwork_indexes
    assert (
        ("status", "ASCENDING"),
        ("lease_expires_at", "ASCENDING"),
    ) in artwork_indexes


def test_pending_job_selection_skips_paused_history_to_reach_terminal_work(monkeypatch):
    now = datetime.now(timezone.utc)

    class Snapshot:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    paused_preferences = _accepted_preferences(_authority())
    terminal_preferences = {
        **paused_preferences,
        "authority_digest": "digest-b",
        "binding_id": "binding-b",
        "profile_id": "profile-b",
    }
    pending = [
        Snapshot(
            f"paused-{index}",
            {
                "status": "pending",
                "uid": "owner-paused",
                "memory_id": f"history-{index}",
                "authority_digest": "digest-a",
                "style_version": artwork.DEFAULT_STYLE_VERSION,
                "origin": artwork.HISTORICAL_BACKFILL_ORIGIN,
                "available_at": now - timedelta(minutes=10 - index),
            },
        )
        for index in range(3)
    ]
    pending.append(
        Snapshot(
            "terminal-new",
            {
                "status": "pending",
                "uid": "owner-terminal",
                "memory_id": "new-memory",
                "authority_digest": "digest-b",
                "style_version": artwork.DEFAULT_STYLE_VERSION,
                "origin": artwork.TERMINAL_ENRICHMENT_ORIGIN,
                "available_at": now - timedelta(minutes=1),
            },
        )
    )

    class Query:
        def __init__(self, values):
            self.values = values

        def where(self, *args):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, value):
            self.values = self.values[:value]
            return self

        def stream(self):
            return iter(self.values)

    class JobCollection:
        def where(self, field, operator, value):
            return Query(pending if value == "pending" else [])

    class UserDocument:
        def __init__(self, uid):
            self.id = uid

        def get(self):
            preferences = paused_preferences if self.id == "owner-paused" else terminal_preferences
            control = {
                "schema_version": "ella.memory_artwork.queue_control.v1",
                "generation_id": artwork_database.reconciliation_job_id(
                    self.id,
                    preferences["authority_digest"],
                    preferences["style_version"],
                ),
                "authority_digest": preferences["authority_digest"],
                "style_version": preferences["style_version"],
                "state": "paused" if self.id == "owner-paused" else "running",
            }
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    artwork_database.PREFERENCES_FIELD: preferences,
                    artwork_database.BACKFILL_CONTROL_FIELD: control,
                },
            )

    class UserCollection:
        def document(self, uid):
            return UserDocument(uid)

    class Database:
        def collection(self, name):
            return JobCollection() if name == artwork_database.JOB_COLLECTION else UserCollection()

    monkeypatch.setattr(artwork_database, "db", Database())

    jobs = artwork_database.list_pending_jobs(limit=1, now=now)

    assert [job["job_id"] for job in jobs] == ["terminal-new"]


def test_pending_job_selection_prioritizes_terminal_then_preview_then_history(monkeypatch):
    now = datetime.now(timezone.utc)

    class Snapshot:
        def __init__(self, identifier, payload):
            self.id = identifier
            self._payload = payload

        def to_dict(self):
            return copy.deepcopy(self._payload)

    def job(identifier, origin, available_at):
        return Snapshot(
            identifier,
            {
                "status": "pending",
                "uid": f"owner-{identifier}",
                "memory_id": f"memory-{identifier}",
                "authority_digest": "digest-a",
                "style_version": artwork.DEFAULT_STYLE_VERSION,
                "origin": origin,
                "available_at": available_at,
            },
        )

    due_by_origin = {
        artwork_database.TERMINAL_ENRICHMENT_ORIGIN: [
            job("terminal", artwork_database.TERMINAL_ENRICHMENT_ORIGIN, now - timedelta(minutes=1))
        ],
        artwork_database.PREVIEW_BACKFILL_ORIGIN: [
            job("preview", artwork_database.PREVIEW_BACKFILL_ORIGIN, now - timedelta(minutes=5))
        ],
        artwork_database.HISTORICAL_BACKFILL_ORIGIN: [
            job("history", artwork_database.HISTORICAL_BACKFILL_ORIGIN, now - timedelta(minutes=20))
        ],
    }

    def bounded(_collection, *, status, origin=None, **_kwargs):
        return due_by_origin.get(origin, []) if status == "pending" else []

    class UserDocument:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {artwork_database.PREFERENCES_FIELD: _accepted_preferences(_authority())},
            )

    class Users:
        def document(self, _uid):
            return UserDocument()

    class Database:
        def collection(self, name):
            assert name == "users"
            return Users()

    monkeypatch.setattr(artwork_database, "_bounded_due_snapshots", bounded)
    monkeypatch.setattr(artwork_database, "db", Database())

    jobs = artwork_database.list_pending_jobs(limit=3, now=now)

    assert [job["origin"] for job in jobs] == [
        artwork_database.TERMINAL_ENRICHMENT_ORIGIN,
        artwork_database.PREVIEW_BACKFILL_ORIGIN,
        artwork_database.HISTORICAL_BACKFILL_ORIGIN,
    ]


def test_legacy_job_migration_commits_firestore_writes_in_safe_chunks(monkeypatch):
    now = datetime.now(timezone.utc)
    generation_key = "a" * 64

    class Reference:
        pass

    class Snapshot:
        def __init__(self, index):
            self.id = f"job-{index}"
            self.reference = Reference()
            self._payload = {
                "uid": "owner-a",
                "memory_id": f"memory-{index}",
                "generation_key": generation_key,
                "status": "pending",
                "available_at": now,
            }

        def to_dict(self):
            return copy.deepcopy(self._payload)

    snapshots = [Snapshot(index) for index in range(401)]

    class Query:
        def where(self, *args):
            return self

        def stream(self):
            return iter(snapshots)

    class Batch:
        def __init__(self, database):
            self.database = database
            self.count = 0

        def set(self, reference, payload, merge=False):
            assert merge is True
            self.count += 1

        def commit(self):
            self.database.commits.append(self.count)

    class Database:
        def __init__(self):
            self.commits = []

        def collection(self, name):
            assert name == artwork_database.JOB_COLLECTION
            return Query()

        def batch(self):
            return Batch(self)

    class ConversationReference:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    artwork_database.ARTWORK_FIELD: {
                        "generation_key": generation_key,
                        "authority_digest": "digest-a",
                        "style_version": artwork.DEFAULT_STYLE_VERSION,
                    }
                },
            )

    database = Database()
    monkeypatch.setattr(artwork_database, "db", database)
    monkeypatch.setattr(artwork_database, "_conversation_ref", lambda uid, memory_id: ConversationReference())

    jobs = artwork_database.list_jobs_for_uid("owner-a")

    assert len(jobs) == 401
    assert database.commits == [artwork_database.FIRESTORE_MIGRATION_BATCH_SIZE, 1]


def test_queue_job_read_does_not_fan_out_into_legacy_migration(monkeypatch):
    class Reference:
        pass

    class Snapshot:
        id = "legacy-job"
        reference = Reference()

        @staticmethod
        def to_dict():
            return {
                "uid": "owner-a",
                "memory_id": "memory-a",
                "generation_key": "a" * 64,
                "status": "pending",
            }

    class Query:
        def where(self, *args):
            return self

        @staticmethod
        def stream():
            return iter([Snapshot()])

    class Database:
        def collection(self, name):
            assert name == artwork_database.JOB_COLLECTION
            return Query()

        @staticmethod
        def batch():
            raise AssertionError("read-only queue status must not allocate a migration batch")

    monkeypatch.setattr(artwork_database, "db", Database())
    monkeypatch.setattr(
        artwork_database,
        "_conversation_ref",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queue status must not read legacy conversations")),
    )

    jobs = artwork_database.list_jobs_for_uid("owner-a", migrate_legacy_jobs=False)

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "legacy-job"


def test_pending_reconciliation_query_is_bounded_to_due_work_and_indexed(monkeypatch):
    now = datetime.now(timezone.utc)
    authority_digest = "digest-a"
    style_version = artwork.DEFAULT_STYLE_VERSION

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
                "job-pending",
                {
                    "uid": "owner-a",
                    "authority_digest": authority_digest,
                    "style_version": style_version,
                    "status": "pending",
                    "available_at": now - timedelta(minutes=2),
                },
            )
        ]
    )
    processing_query = Query(
        [
            Snapshot(
                "job-processing",
                {
                    "uid": "owner-b",
                    "authority_digest": authority_digest,
                    "style_version": style_version,
                    "status": "processing",
                    "lease_expires_at": now - timedelta(minutes=1),
                },
            )
        ]
    )

    class Collection:
        def where(self, field, operator, value):
            query = pending_query if value == "pending" else processing_query
            return query.where(field, operator, value)

    class Database:
        def collection(self, name):
            assert name == artwork_database.RECONCILIATION_COLLECTION
            return Collection()

    class UserReference:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    artwork_database.PREFERENCES_FIELD: {
                        "authority_digest": authority_digest,
                        "style_version": style_version,
                    }
                },
            )

    monkeypatch.setattr(artwork_database, "db", Database())
    monkeypatch.setattr(artwork_database, "_user_ref", lambda uid: UserReference())

    jobs = artwork_database.list_pending_reconciliation_jobs(limit=2, now=now)

    assert [job["job_id"] for job in jobs] == ["job-pending", "job-processing"]
    assert pending_query.operations == [
        ("where", "status", "==", "pending"),
        ("where", "available_at", "<=", now),
        ("order_by", "available_at", artwork_database.firestore.Query.ASCENDING),
        ("limit", 8),
    ]
    assert processing_query.operations == [
        ("where", "status", "==", "processing"),
        ("where", "lease_expires_at", "<=", now),
        ("order_by", "lease_expires_at", artwork_database.firestore.Query.ASCENDING),
        ("limit", 8),
    ]
    indexes = json.loads((BACKEND_ROOT.parent / "firestore.indexes.json").read_text())["indexes"]
    reconciliation_indexes = {
        tuple((field["fieldPath"], field["order"]) for field in index["fields"])
        for index in indexes
        if index.get("collectionGroup") == artwork_database.RECONCILIATION_COLLECTION
    }
    assert (("status", "ASCENDING"), ("available_at", "ASCENDING")) in reconciliation_indexes
    assert (("status", "ASCENDING"), ("lease_expires_at", "ASCENDING")) in reconciliation_indexes


def test_pending_reconciliation_filters_paused_users_before_worker_limit(monkeypatch):
    now = datetime.now(timezone.utc)
    authority_digest = "digest-a"
    style_version = artwork.DEFAULT_STYLE_VERSION

    class Snapshot:
        def __init__(self, identifier, uid, available_at):
            self.id = identifier
            self.uid = uid
            self.available_at = available_at

        def to_dict(self):
            return {
                "uid": self.uid,
                "authority_digest": authority_digest,
                "style_version": style_version,
                "status": "pending",
                "available_at": self.available_at,
            }

    snapshots = [Snapshot(f"paused-{index}", "owner-paused", now - timedelta(minutes=20 - index)) for index in range(5)]
    snapshots.append(Snapshot("eligible", "owner-running", now - timedelta(minutes=1)))

    class Query:
        def __init__(self):
            self._status = None
            self._cursor = None
            self._limit = None

        def where(self, field, operator, value):
            self._status = value if field == "status" else self._status
            return self

        def order_by(self, field, direction):
            return self

        def start_after(self, cursor):
            self._cursor = cursor
            return self

        def limit(self, value):
            self._limit = value
            return self

        def stream(self):
            values = snapshots if self._status == "pending" else []
            if self._cursor is not None:
                cursor_index = next(index for index, snapshot in enumerate(values) if snapshot.id == self._cursor.id)
                values = values[cursor_index + 1 :]
            return iter(values[: self._limit])

    class Database:
        def collection(self, name):
            assert name == artwork_database.RECONCILIATION_COLLECTION
            return Query()

    class UserReference:
        def __init__(self, uid):
            self.uid = uid

        def get(self):
            generation_id = artwork_database.reconciliation_job_id(self.uid, authority_digest, style_version)
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    artwork_database.PREFERENCES_FIELD: {
                        "authority_digest": authority_digest,
                        "style_version": style_version,
                    },
                    artwork_database.BACKFILL_CONTROL_FIELD: {
                        "schema_version": "ella.memory_artwork.queue_control.v1",
                        "generation_id": generation_id,
                        "authority_digest": authority_digest,
                        "style_version": style_version,
                        "state": "running" if self.uid == "owner-running" else "paused",
                    },
                },
            )

    monkeypatch.setattr(artwork_database, "db", Database())
    monkeypatch.setattr(artwork_database, "_user_ref", UserReference)
    artwork_database._due_scan_cursors.clear()

    first_page = artwork_database.list_pending_reconciliation_jobs(limit=1, now=now)
    jobs = artwork_database.list_pending_reconciliation_jobs(limit=1, now=now)

    assert first_page == []
    assert [job["job_id"] for job in jobs] == ["eligible"]


def test_reconciliation_claim_deletes_job_when_owner_is_missing():
    class Snapshot:
        def __init__(self, *, exists, payload=None):
            self.exists = exists
            self.payload = payload or {}

        def to_dict(self):
            return copy.deepcopy(self.payload)

    class Reference:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def get(self, transaction=None):
            return self.snapshot

    class Transaction:
        def __init__(self):
            self.deleted = []

        def delete(self, reference):
            self.deleted.append(reference)

    transaction = Transaction()
    user_ref = Reference(Snapshot(exists=False))
    job_ref = Reference(Snapshot(exists=True, payload={"status": "pending"}))

    claimed = artwork_database._claim_reconciliation_job_transaction(
        transaction,
        user_ref,
        job_ref,
        lease_token="lease-a",
        now=datetime.now(timezone.utc),
        lease_seconds=30,
    )

    assert claimed is None
    assert transaction.deleted == [job_ref]


def test_delete_jobs_for_uid_removes_generation_and_reconciliation_jobs(monkeypatch):
    class Reference:
        def __init__(self, collection_name, document_id):
            self.collection_name = collection_name
            self.document_id = document_id

    class Snapshot:
        def __init__(self, reference):
            self.reference = reference

    class Query:
        def __init__(self, database, collection_name):
            self.database = database
            self.collection_name = collection_name
            self.query_uid = None
            self.query_limit = None

        def where(self, field, operator, value):
            assert (field, operator) == ("uid", "==")
            self.query_uid = value
            return self

        def limit(self, value):
            self.query_limit = value
            return self

        def stream(self):
            matches = [
                Snapshot(Reference(self.collection_name, document_id))
                for document_id, uid in self.database.documents[self.collection_name].items()
                if uid == self.query_uid
            ]
            return iter(matches[: self.query_limit])

    class Batch:
        def __init__(self, database):
            self.database = database
            self.references = []

        def delete(self, reference):
            self.references.append(reference)

        def commit(self):
            for reference in self.references:
                self.database.documents[reference.collection_name].pop(reference.document_id)

    class Database:
        def __init__(self):
            self.documents = {
                artwork_database.JOB_COLLECTION: {"generation-a": "owner-a", "generation-b": "owner-b"},
                artwork_database.RECONCILIATION_COLLECTION: {
                    "reconciliation-a": "owner-a",
                    "reconciliation-b": "owner-b",
                },
            }

        def collection(self, collection_name):
            return Query(self, collection_name)

        def batch(self):
            return Batch(self)

    database = Database()
    monkeypatch.setattr(artwork_database, "db", database)

    deleted = artwork_database.delete_jobs_for_uid("owner-a", batch_size=1)

    assert deleted == 2
    assert database.documents == {
        artwork_database.JOB_COLLECTION: {"generation-b": "owner-b"},
        artwork_database.RECONCILIATION_COLLECTION: {"reconciliation-b": "owner-b"},
    }


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


def test_automatic_generation_is_durably_one_shot_but_manual_retry_remains_available():
    repository = FakeRepository()
    repository.conversations[("owner-a", "memory-1")] = _terminal_memory("memory-1")
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())

    def service():
        return artwork.MemoryArtworkService(
            repository=repository,
            authority_resolver=_resolver,
            provider_factory=FakeProvider,
            store_factory=FakeStore,
            config=_enabled_config(),
        )

    first = asyncio.run(service().enqueue("owner-a", "memory-1", request_mode="automatic"))
    assert first["outcome"] == "reserved"
    generation_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"]
    repository.conversations[("owner-a", "memory-1")]["artwork"].update(
        {"status": "unavailable", "failure_code": "memory_artwork_provider_failed"}
    )
    repository.jobs[("owner-a", "memory-1", generation_key)].update({"status": "failed", "attempt_count": 5})

    after_restart = asyncio.run(service().enqueue("owner-a", "memory-1", request_mode="automatic"))
    assert after_restart == {
        "outcome": "automatic_attempt_already_used",
        "status": "unavailable",
        "failure_code": "memory_artwork_automatic_attempt_exhausted",
    }
    assert repository.reserve_writes == 1
    assert repository.jobs[("owner-a", "memory-1", generation_key)]["attempt_count"] == 5

    manual = asyncio.run(service().enqueue("owner-a", "memory-1", request_mode="manual"))
    assert manual["outcome"] == "reserved"
    assert repository.reserve_writes == 2


@pytest.mark.parametrize("terminal_job_status", ("failed", "completed"))
def test_automatic_generation_does_not_reopen_terminal_job_while_artwork_is_generating(terminal_job_status):
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

    first = asyncio.run(service.enqueue("owner-a", "memory-1", request_mode="automatic"))
    assert first["outcome"] == "reserved"
    generation_key = repository.conversations[("owner-a", "memory-1")]["artwork"]["generation_key"]
    job = repository.jobs[("owner-a", "memory-1", generation_key)]
    job.update({"status": terminal_job_status, "attempt_count": 5})

    repeated = asyncio.run(service.enqueue("owner-a", "memory-1", request_mode="automatic"))

    assert repeated == {
        "outcome": "automatic_attempt_already_used",
        "status": "unavailable",
        "failure_code": "memory_artwork_automatic_attempt_exhausted",
    }
    assert job["status"] == terminal_job_status
    assert job["attempt_count"] == 5
    assert repository.reserve_writes == 1


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
    assert repository.jobs[("owner-a", "memory-1", generation_key)]["status"] == "pending"


def test_signed_url_returns_durable_generating_state_without_runtime_resolution():
    repository = FakeRepository()
    memory = _terminal_memory("memory-1")
    _, prompt_sha256 = artwork._prompt_for(memory, artwork.DEFAULT_STYLE_VERSION)
    memory["artwork"] = {
        "status": "generating",
        "generation_key": "a" * 64,
        "style_version": artwork.DEFAULT_STYLE_VERSION,
        "enrichment_revision": "summary-memory-1",
        "prompt_sha256": prompt_sha256,
        "authority_digest": "digest-a",
        "binding_id": "binding-owner-a",
        "profile_id": "profile-owner-a",
    }
    repository.conversations[("owner-a", "memory-1")] = memory
    repository.preferences_by_uid["owner-a"] = _accepted_preferences(_authority())
    store = FakeStore()

    async def never_resolve(uid):
        raise AssertionError("signed artwork reads must not resolve the runtime")

    service = artwork.MemoryArtworkService(
        repository=repository,
        authority_resolver=never_resolve,
        store_factory=lambda: store,
        config=_enabled_config(),
    )

    result = asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert result["status"] == "generating"
    assert store.signed == []


def test_missing_ready_blob_is_demoted_and_can_be_rereserved():
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

    class MissingObjectStore(FakeStore):
        def signed_get_url(self, **kwargs):
            raise artwork.MemoryArtworkStorageError("memory_artwork_object_missing")

    service.store_factory = MissingObjectStore
    conversation = repository.conversations[("owner-a", "memory-1")]
    generation_key = conversation["artwork"]["generation_key"]

    with pytest.raises(artwork.MemoryArtworkError) as missing:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert missing.value.code == "memory_artwork_object_missing"
    assert conversation["artwork"]["status"] == "unavailable"
    assert asyncio.run(service.enqueue("owner-a", "memory-1")) == {"outcome": "reserved", "status": "generating"}
    assert repository.jobs[("owner-a", "memory-1", generation_key)]["status"] == "pending"


def test_transient_ready_blob_lookup_failure_is_retryable_without_demoting_artwork():
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

    class UnavailableStore(FakeStore):
        def signed_get_url(self, **kwargs):
            raise artwork.MemoryArtworkStorageError("memory_artwork_storage_unavailable")

    service.store_factory = UnavailableStore
    conversation = repository.conversations[("owner-a", "memory-1")]
    ready_artwork = copy.deepcopy(conversation["artwork"])

    with pytest.raises(artwork.MemoryArtworkError) as unavailable:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert unavailable.value.code == "memory_artwork_storage_unavailable"
    assert unavailable.value.retryable is True
    assert conversation["artwork"] == ready_artwork


@pytest.mark.parametrize(
    "storage_error",
    [
        "memory_artwork_object_profile_mismatch",
        "memory_artwork_object_generation_mismatch",
    ],
)
def test_ready_blob_authority_mismatch_is_non_retryable_conflict_without_demoting_artwork(storage_error):
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

    class MismatchedStore(FakeStore):
        def signed_get_url(self, **kwargs):
            raise artwork.MemoryArtworkStorageError(storage_error)

    service.store_factory = MismatchedStore
    conversation = repository.conversations[("owner-a", "memory-1")]
    ready_artwork = copy.deepcopy(conversation["artwork"])

    with pytest.raises(artwork.MemoryArtworkError) as mismatch:
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    assert mismatch.value.code == storage_error
    assert mismatch.value.retryable is False
    assert conversation["artwork"] == ready_artwork
    router_module = _load_memory_artwork_router_module(f"ella_memory_artwork_{storage_error}_router_test_module")
    response = router_module._http_error(mismatch.value)
    assert response.status_code == 409
    assert response.detail == {"code": storage_error, "retryable": False}


def test_stale_missing_blob_read_cannot_demote_a_new_same_key_generation_claim():
    class InterleavingRepository(FakeRepository):
        def mark_generation_unavailable(self, uid, memory_id, *, expected_artwork, **kwargs):
            current = self.conversations[(uid, memory_id)]["artwork"]
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
                expected_artwork=expected_artwork,
                **kwargs,
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
    with pytest.raises(artwork.MemoryArtworkError):
        asyncio.run(service.signed_url("owner-a", "memory-1"))

    current = repository.conversations[("owner-a", "memory-1")]["artwork"]
    assert current["status"] == "generating"
    assert current["lease_token"] == "new-generation-lease"
    assert "failure_code" not in current


def test_gcs_signed_url_rejects_missing_or_wrong_authority_before_signing():
    class MissingBlob:
        def exists(self):
            return False

        def generate_signed_url(self, **kwargs):
            raise AssertionError("missing artwork must not receive a signed URL")

    missing_blob = MissingBlob()

    class Bucket:
        def blob(self, object_key):
            return missing_blob

    class Client:
        def bucket(self, bucket_name):
            return Bucket()

    object_key = memory_artwork_storage.object_key_for(
        uid="owner-a",
        profile_binding_id="binding-owner-a",
        memory_id="memory-1",
        generation_key="a" * 64,
        content_type="image/png",
    )
    store = memory_artwork_storage.GCSMemoryArtworkStore(bucket_name="private-artwork", client=Client())

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as mismatch:
        store.signed_get_url(
            uid="owner-a",
            profile_binding_id="binding-owner-b",
            memory_id="memory-1",
            generation_key="a" * 64,
            object_key=object_key,
        )
    assert str(mismatch.value) == "memory_artwork_object_profile_mismatch"

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as missing:
        store.signed_get_url(
            uid="owner-a",
            profile_binding_id="binding-owner-a",
            memory_id="memory-1",
            generation_key="a" * 64,
            object_key=object_key,
        )
    assert str(missing.value) == "memory_artwork_object_missing"


def test_content_addressed_artwork_keys_are_stable_immutable_and_owner_scoped():
    common = {
        "uid": "owner-a",
        "profile_binding_id": "binding-owner-a",
        "memory_id": "memory-1",
        "generation_key": "a" * 64,
        "content_type": "image/webp",
        "rendition": "w384",
    }
    first = memory_artwork_storage.content_addressed_object_key_for(
        **common,
        image_bytes=b"first immutable rendition",
    )
    repeated = memory_artwork_storage.content_addressed_object_key_for(
        **common,
        image_bytes=b"first immutable rendition",
    )
    changed = memory_artwork_storage.content_addressed_object_key_for(
        **common,
        image_bytes=b"different immutable rendition",
    )

    assert first == repeated
    assert changed != first
    assert "owner-a" not in first
    assert memory_artwork_storage.CONTENT_ADDRESSED_ARTWORK_OBJECT_RE.fullmatch(first)
    memory_artwork_storage._validated_artwork_key(
        "owner-a",
        "binding-owner-a",
        "memory-1",
        "a" * 64,
        first,
    )


def test_memory_erasure_deletes_every_historical_binding_version():
    class Blob:
        def __init__(self, name):
            self.name = name
            self.deleted = False

        def delete(self):
            self.deleted = True

    blobs = [
        Blob(
            memory_artwork_storage.object_key_for(
                uid="owner-a",
                profile_binding_id=f"binding-{index}",
                memory_id="memory-1",
                generation_key=f"{index + 1:064x}",
                content_type="image/jpeg",
            )
        )
        for index in range(3)
    ]
    other_memory = Blob(
        memory_artwork_storage.object_key_for(
            uid="owner-a",
            profile_binding_id="binding-3",
            memory_id="memory-2",
            generation_key="4" * 64,
            content_type="image/jpeg",
        )
    )
    other_owner = Blob(
        memory_artwork_storage.object_key_for(
            uid="owner-b",
            profile_binding_id="binding-4",
            memory_id="memory-1",
            generation_key="5" * 64,
            content_type="image/jpeg",
        )
    )
    blobs.extend((other_memory, other_owner))

    class Bucket:
        def list_blobs(self, *, prefix):
            return [blob for blob in blobs if blob.name.startswith(prefix)]

    class Client:
        def bucket(self, bucket_name):
            assert bucket_name == "private-artwork"
            return Bucket()

    store = memory_artwork_storage.GCSMemoryArtworkStore(bucket_name="private-artwork", client=Client())

    assert store.delete_memory_all_bindings(uid="owner-a", memory_id="memory-1") == 3
    assert all(blob.deleted for blob in blobs[:3])
    assert other_memory.deleted is False
    assert other_owner.deleted is False


def test_gcs_signed_url_wraps_transient_existence_failure_without_signing():
    class UnavailableBlob:
        def exists(self):
            raise RuntimeError("provider detail must not escape")

        def generate_signed_url(self, **kwargs):
            raise AssertionError("unverified artwork must not receive a signed URL")

    class Bucket:
        def blob(self, object_key):
            return UnavailableBlob()

    class Client:
        def bucket(self, bucket_name):
            return Bucket()

    object_key = memory_artwork_storage.object_key_for(
        uid="owner-a",
        profile_binding_id="binding-owner-a",
        memory_id="memory-1",
        generation_key="a" * 64,
        content_type="image/png",
    )
    store = memory_artwork_storage.GCSMemoryArtworkStore(bucket_name="private-artwork", client=Client())

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as unavailable:
        store.signed_get_url(
            uid="owner-a",
            profile_binding_id="binding-owner-a",
            memory_id="memory-1",
            generation_key="a" * 64,
            object_key=object_key,
        )

    assert str(unavailable.value) == "memory_artwork_storage_unavailable"


def test_storage_owner_validation_and_production_deletion_hooks(monkeypatch):
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

    all_binding_deletes = []
    real_store_class = memory_artwork_storage.GCSMemoryArtworkStore

    class RecordingStore:
        def delete_memory_all_bindings(self, **kwargs):
            all_binding_deletes.append(kwargs)
            return 3

    monkeypatch.setattr(memory_artwork_storage, "GCSMemoryArtworkStore", RecordingStore)
    memory_artwork_storage.delete_conversation_artwork_if_present(
        "owner-a",
        "memory-1",
        {
            "artwork": {"object_key": object_key, "binding_id": "binding-owner-a"},
            "published_artwork": {
                "object_key": memory_artwork_storage.object_key_for(
                    uid="owner-a",
                    profile_binding_id="binding-owner-a-v0",
                    memory_id="memory-1",
                    generation_key="b" * 64,
                    content_type="image/png",
                ),
                "binding_id": "binding-owner-a-v0",
            },
        },
    )
    assert all_binding_deletes == [{"uid": "owner-a", "memory_id": "memory-1"}]

    memory_artwork_storage.delete_conversation_artwork_if_present(
        "owner-a",
        "memory-2",
        {"artwork": {"status": "generating", "binding_id": "binding-owner-a"}},
    )
    assert all_binding_deletes[-1] == {"uid": "owner-a", "memory_id": "memory-2"}

    real_store = object.__new__(real_store_class)
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as unbound:
        real_store.delete_memory_prefix(uid="owner-a", memory_id="memory-3")
    assert str(unbound.value) == "memory_artwork_binding_required"

    conversations_source = (BACKEND_ROOT / "database" / "conversations.py").read_text(encoding="utf-8")
    account_route_source = (BACKEND_ROOT / "routers" / "users.py").read_text(encoding="utf-8")
    delete_start = conversations_source.index("def delete_conversation(uid, conversation_id")
    assert conversations_source.index(
        "require_memory_artwork_publication_lock", delete_start
    ) < conversations_source.index("claim_deletion", delete_start)
    assert conversations_source.index("has_processing_jobs_for_memory", delete_start) < conversations_source.index(
        "delete_conversation_artwork_if_present", delete_start
    )
    assert conversations_source.index("delete_conversation_artwork_if_present") < conversations_source.index(
        "delete_jobs_for_memory", delete_start
    )
    assert conversations_source.index("delete_jobs_for_memory", delete_start) < conversations_source.index(
        "conversation_ref.delete()", delete_start
    )
    account_delete_start = account_route_source.index("async def delete_account")
    assert account_route_source.index(
        "acquire_memory_artwork_publication_lock", account_delete_start
    ) < account_route_source.index("prepare_account_artwork_deletion", account_delete_start)
    assert account_route_source.index(
        "prepare_account_artwork_deletion", account_delete_start
    ) < account_route_source.index("unlink_self_owner_account_on_deletion(uid=uid)", account_delete_start)
    user_database_source = (BACKEND_ROOT / "database" / "users.py").read_text(encoding="utf-8")
    user_delete_start = user_database_source.index("def delete_user_data(uid: str")
    assert user_database_source.index(
        "prepare_account_artwork_deletion(uid, lock_proof=artwork_lock_proof)", user_delete_start
    ) < user_database_source.index("subcollections_to_delete", user_delete_start)


def test_account_artwork_cleanup_fails_closed_only_when_storage_was_touched(monkeypatch):
    monkeypatch.delenv("ELLA_MEMORY_ARTWORK_BUCKET", raising=False)
    assert memory_artwork_storage.delete_all_user_artwork("owner-a", cleanup_required=False) == 0
    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.delete_all_user_artwork("owner-a", cleanup_required=True)
    assert str(failure.value) == "memory_artwork_storage_cleanup_unavailable"


def test_distributed_publication_lock_holds_postgres_session_until_release(monkeypatch):
    calls = []

    class Connection:
        closed = False

        async def fetchval(self, query, lock_name):
            calls.append(("query", "try" if "try" in query else "unlock", lock_name))
            return True

        async def close(self):
            self.closed = True

        def is_closed(self):
            return self.closed

    connection = Connection()

    async def open_connection():
        calls.append(("open",))
        return connection

    monkeypatch.setattr(memory_artwork_storage, "open_ella_postgres_connection", open_connection)

    async def scenario():
        async with memory_artwork_storage.acquire_memory_artwork_publication_lock("owner-a") as proof:
            memory_artwork_storage.require_memory_artwork_publication_lock("owner-a", proof)
            with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError):
                memory_artwork_storage.require_memory_artwork_publication_lock("owner-b", proof)
        with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError):
            memory_artwork_storage.require_memory_artwork_publication_lock("owner-a", proof)

    asyncio.run(scenario())

    assert calls[0] == ("open",)
    assert calls[1][0:2] == ("query", "try")
    assert calls[2][0:2] == ("query", "unlock")
    assert calls[1][2] == calls[2][2]
    assert connection.closed is True


def test_distributed_publication_lock_fails_closed_when_owner_is_busy(monkeypatch):
    class Connection:
        closed = False

        async def fetchval(self, query, lock_name):
            assert "pg_try_advisory_lock" in query
            assert lock_name.startswith("ella-memory-artwork-publication-v1:")
            return False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    connection = Connection()

    async def open_connection():
        return connection

    monkeypatch.setattr(memory_artwork_storage, "open_ella_postgres_connection", open_connection)

    async def scenario():
        with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
            async with memory_artwork_storage.acquire_memory_artwork_publication_lock("owner-a"):
                raise AssertionError("busy owner lock must not enter the publication section")
        assert str(failure.value) == "memory_artwork_publication_lock_busy"

    asyncio.run(scenario())
    assert connection.closed is True


def test_account_deletion_requires_distributed_publication_lock():
    class Repository:
        @staticmethod
        def begin_account_deletion(uid):
            raise AssertionError("deletion must not start without the distributed lock")

    with pytest.raises(memory_artwork_storage.MemoryArtworkStorageError) as failure:
        memory_artwork_storage.prepare_account_artwork_deletion("owner-a", repository=Repository)
    assert str(failure.value) == "memory_artwork_publication_lock_required"


def test_account_deletion_returns_before_cleanup_while_claimed_worker_is_active(monkeypatch):
    monkeypatch.setattr(memory_artwork_storage, "require_memory_artwork_publication_lock", lambda uid, proof: None)
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
    monkeypatch.setattr(memory_artwork_storage, "require_memory_artwork_publication_lock", lambda uid, proof: None)

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


def _load_memory_artwork_router_module(router_name: str):
    service_module_name = "ella.services.memory_artwork"
    saved_service = sys.modules.get(service_module_name)
    recovery_module_name = "ella.services.memory_artwork_recovery"
    saved_recovery = sys.modules.get(recovery_module_name)
    summary_module_name = "ella.services.summary_recovery"
    saved_summary = sys.modules.get(summary_module_name)
    auth_module_name = "utils.ella.exact_firebase_auth"
    saved_auth = sys.modules.get(auth_module_name)
    parent_auth = sys.modules.get("utils.ella")
    saved_parent_auth = getattr(parent_auth, "exact_firebase_auth", None) if parent_auth is not None else None

    class TestAuthority:
        @staticmethod
        def require_uid(uid, *, feature):
            return uid

    def reject_unauthenticated():
        raise HTTPException(status_code=401, detail="unauthorized")

    auth_stub = types.ModuleType(auth_module_name)
    auth_stub.ELLA_SUBJECT_UID_HEADER = "X-Ella-Subject-Uid"
    auth_stub.EllaRequestAuthority = TestAuthority
    auth_stub.get_exact_firebase_uid = reject_unauthenticated
    auth_stub.get_exact_service_authority = lambda **kwargs: TestAuthority()

    recovery_stub = types.ModuleType(recovery_module_name)
    recovery_stub.claim_memory_artwork_enrichment_recovery = lambda uid, memory_id: None
    summary_stub = types.ModuleType(summary_module_name)
    summary_stub.recover_failed_conversation_summary = lambda **kwargs: None

    sys.modules[service_module_name] = artwork
    sys.modules[recovery_module_name] = recovery_stub
    sys.modules[summary_module_name] = summary_stub
    sys.modules[auth_module_name] = auth_stub
    if parent_auth is not None:
        setattr(parent_auth, "exact_firebase_auth", auth_stub)
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
        if saved_recovery is None:
            sys.modules.pop(recovery_module_name, None)
        else:
            sys.modules[recovery_module_name] = saved_recovery
        if saved_summary is None:
            sys.modules.pop(summary_module_name, None)
        else:
            sys.modules[summary_module_name] = saved_summary
        if saved_auth is None:
            sys.modules.pop(auth_module_name, None)
        else:
            sys.modules[auth_module_name] = saved_auth
        if parent_auth is not None:
            if saved_parent_auth is None:
                if getattr(parent_auth, "exact_firebase_auth", None) is auth_stub:
                    delattr(parent_auth, "exact_firebase_auth")
            else:
                setattr(parent_auth, "exact_firebase_auth", saved_parent_auth)
    return router_module


def test_mounted_route_rejects_unauthenticated_request_before_service_work(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_router_test_module")

    class NeverCalled:
        calls = 0

        async def signed_url(self, uid, memory_id):
            self.calls += 1
            return {"status": "ready"}

        async def recover_recent(self, uid):
            self.calls += 1
            return {"status": "pending"}

        async def recover_permanent_artwork(self, uid, **kwargs):
            self.calls += 1
            return {"status": "pending"}

        async def day_artwork(self, uid, day, **kwargs):
            self.calls += 1
            return {"items": []}

    fake = NeverCalled()
    monkeypatch.setattr(router_module, "MemoryArtworkService", lambda: fake)
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.get("/v1/ella/memories/memory-1/artwork")
    recovery_response = client.post("/v1/ella/memory-artwork/recovery/recent")
    permanent_response = client.post("/v1/ella/memory-artwork/recovery/permanent")
    day_response = client.get("/v1/ella/memory-artwork/day/2026-09-25")
    assert response.status_code == 401
    assert recovery_response.status_code == 401
    assert permanent_response.status_code == 401
    assert day_response.status_code == 401
    assert fake.calls == 0


def test_permanent_recovery_and_day_routes_bind_authenticated_owner(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_permanent_router_test_module")
    calls = []

    class Service:
        async def recover_permanent_artwork(self, uid, *, cursor_memory_id=None, limit=50):
            calls.append(("recover", uid, cursor_memory_id, limit))
            return {"provider_calls": 0, "restored": 2, "variant_backfilled": 1}

        async def day_artwork(self, uid, day, *, utc_offset_minutes):
            calls.append(("day", uid, day, utc_offset_minutes))
            return {"day": day, "items": []}

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"
    client = TestClient(app)

    recovery = client.post(
        "/v1/ella/memory-artwork/recovery/permanent",
        json={"cursor": "memory-cursor", "limit": 25},
    )
    day = client.get("/v1/ella/memory-artwork/day/2026-09-25?utc_offset_minutes=-420")

    assert recovery.status_code == 202
    assert recovery.json()["provider_calls"] == 0
    assert day.status_code == 200
    assert calls == [
        ("recover", "owner-a", "memory-cursor", 25),
        ("day", "owner-a", "2026-09-25", -420),
    ]


def test_libraries_route_uses_authenticated_owner_and_actual_inventory_service(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_libraries_router_test_module")
    calls = []

    class Service:
        async def libraries(self, uid):
            calls.append(uid)
            return {
                "schema_version": artwork.ARTWORK_LIBRARIES_SCHEMA_VERSION,
                "selected_style_version": artwork.DEFAULT_STYLE_VERSION,
                "default_preview_days": 3,
                "historical_batch_size": 10,
                "libraries": [],
            }

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).get("/v1/ella/memory-artwork/libraries")

    assert response.status_code == 200
    assert response.json()["schema_version"] == artwork.ARTWORK_LIBRARIES_SCHEMA_VERSION
    assert calls == ["owner-a"]


def test_retry_route_queues_missing_terminal_enrichment_once(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_recovery_router_test_module")
    recovery_calls = []

    class Service:
        async def enqueue(self, uid, memory_id, **kwargs):
            raise artwork.MemoryArtworkError("memory_artwork_enrichment_not_terminal", retryable=True)

    async def claim(uid, memory_id):
        return {
            "outcome": "claimed",
            "request_id": "84eb13fa-31d9-40ba-a742-c4de4757dc10",
            "attempt_count": 1,
        }

    async def recover(**kwargs):
        recovery_calls.append(kwargs)

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    monkeypatch.setattr(router_module, "claim_memory_artwork_enrichment_recovery", claim)
    monkeypatch.setattr(router_module, "recover_failed_conversation_summary", recover)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"
    client = TestClient(app)

    response = client.post("/v1/ella/memories/memory-1/artwork")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "ella.memory_artwork.v1",
        "outcome": "enrichment_queued",
        "status": "generating",
    }
    assert recovery_calls == [
        {
            "uid": "owner-a",
            "conversation_id": "memory-1",
            "request_id": "84eb13fa-31d9-40ba-a742-c4de4757dc10",
            "attempt_count": 1,
        }
    ]


def test_retry_route_observes_existing_enrichment_without_duplicate_work(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_existing_recovery_router_test_module")

    class Service:
        async def enqueue(self, uid, memory_id, **kwargs):
            raise artwork.MemoryArtworkError("memory_artwork_enrichment_not_terminal", retryable=True)

    async def claim(uid, memory_id):
        return {"outcome": "processing", "request_id": "existing", "attempt_count": 1}

    async def never_recover(**kwargs):
        raise AssertionError("an active recovery must not be scheduled twice")

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    monkeypatch.setattr(router_module, "claim_memory_artwork_enrichment_recovery", claim)
    monkeypatch.setattr(router_module, "recover_failed_conversation_summary", never_recover)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"
    client = TestClient(app)

    response = client.post("/v1/ella/memories/memory-1/artwork")

    assert response.status_code == 200
    assert response.json()["status"] == "generating"
    assert response.json()["outcome"] == "enrichment_in_progress"


def test_backfill_route_defaults_to_preview_and_schedules_bounded_recovery(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_backfill_router_test_module")
    recoveries = []

    class Service:
        async def backfill(self, uid, *, cursor_memory_id=None, origin):
            assert uid == "owner-a"
            assert cursor_memory_id == "memory-cursor"
            assert origin == "preview_backfill"
            return {
                "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
                "queued": 2,
                "existing": 7,
                "skipped": 1,
                "next_cursor": "memory-next",
                "has_more": True,
                "_recovery_memory_ids": ["memory-recovery"],
            }

        async def enqueue(self, uid, memory_id):
            raise AssertionError("claimed recovery must finish asynchronously")

    async def claim(uid, memory_id):
        assert (uid, memory_id) == ("owner-a", "memory-recovery")
        return {"outcome": "claimed", "request_id": "recovery-request", "attempt_count": 2}

    async def recover(**kwargs):
        recoveries.append(kwargs)

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    monkeypatch.setattr(router_module, "claim_memory_artwork_enrichment_recovery", claim)
    monkeypatch.setattr(router_module, "recover_failed_conversation_summary", recover)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).post("/v1/ella/memory-artwork/backfill", json={"cursor": "memory-cursor"})

    assert response.status_code == 200
    assert "_recovery_memory_ids" not in response.json()
    assert response.json()["mode"] == "preview"
    assert response.json()["enrichment_recovery_queued"] == 1
    assert recoveries == [
        {
            "uid": "owner-a",
            "conversation_id": "memory-recovery",
            "request_id": "recovery-request",
            "attempt_count": 2,
        }
    ]


def test_backfill_route_empty_body_starts_only_a_bounded_preview(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_preview_backfill_router_test_module")

    class Service:
        async def backfill(self, uid, *, cursor_memory_id=None, origin):
            assert uid == "owner-a"
            assert cursor_memory_id is None
            assert origin == "preview_backfill"
            return {
                "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
                "queued": 10,
                "existing": 0,
                "skipped": 0,
                "next_cursor": "memory-older-10",
                "has_more": True,
                "_recovery_memory_ids": [],
            }

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).post("/v1/ella/memory-artwork/backfill")

    assert response.status_code == 200
    assert response.json()["has_more"] is True
    assert response.json()["next_cursor"] == "memory-older-10"
    assert response.json()["mode"] == "preview"


def test_backfill_route_only_starts_full_reconciliation_after_explicit_all_request(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_full_backfill_router_test_module")

    class Service:
        async def start_reconciliation(self, uid):
            assert uid == "owner-a"
            return {
                "schema_version": artwork.ARTWORK_RECONCILIATION_SCHEMA_VERSION,
                "job_id": "a" * 64,
                "status": "pending",
                "pages_processed": 0,
                "scanned": 0,
                "queued": 0,
                "existing": 0,
                "skipped": 0,
            }

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).post("/v1/ella/memory-artwork/backfill", json={"mode": "all"})

    assert response.status_code == 200
    assert response.json()["has_more"] is True
    assert response.json()["next_cursor"] == f"reconciliation:{'a' * 64}"
    assert response.json()["reconciliation_status"] == "pending"
    assert response.json()["mode"] == "all"


def test_backfill_route_preserves_hexadecimal_legacy_memory_cursor(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_hex_legacy_cursor_router_test_module")
    legacy_cursor = "b" * 64
    calls = []

    class Service:
        async def reconciliation_status(self, uid):
            raise AssertionError("an unprefixed memory cursor must not select reconciliation status")

        async def backfill(self, uid, *, cursor_memory_id, origin):
            calls.append((uid, cursor_memory_id))
            assert origin == "preview_backfill"
            return {
                "schema_version": artwork.ARTWORK_SCHEMA_VERSION,
                "queued": 0,
                "existing": 0,
                "skipped": 0,
                "next_cursor": None,
                "has_more": False,
                "_recovery_memory_ids": [],
            }

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).post("/v1/ella/memory-artwork/backfill", json={"cursor": legacy_cursor})

    assert response.status_code == 200
    assert calls == [("owner-a", legacy_cursor)]


def test_queue_routes_bind_status_and_control_to_authenticated_owner(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_queue_router_test_module")
    calls = []

    class Service:
        async def queue_status(self, uid):
            calls.append(("status", uid))
            return {"schema_version": artwork.ARTWORK_QUEUE_SCHEMA_VERSION, "generation_id": "a" * 64}

        async def set_queue_control(self, uid, *, action, generation_id, auto_continue=False):
            calls.append(("control", uid, action, generation_id, auto_continue))
            return {
                "schema_version": artwork.ARTWORK_QUEUE_SCHEMA_VERSION,
                "generation_id": generation_id,
                "control_state": "paused",
            }

        async def recover_recent(self, uid):
            calls.append(("recovery", uid))
            return {"schema_version": artwork.ARTWORK_RECENT_RECOVERY_SCHEMA_VERSION, "items": []}

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"
    client = TestClient(app)

    status_response = client.get("/v1/ella/memory-artwork/queue")
    control_response = client.post(
        "/v1/ella/memory-artwork/queue/control",
        json={"action": "pause", "generation_id": "a" * 64},
    )
    recovery_response = client.post("/v1/ella/memory-artwork/recovery/recent")

    assert status_response.status_code == 200
    assert control_response.status_code == 200
    assert recovery_response.status_code == 202
    assert calls == [
        ("status", "owner-a"),
        ("control", "owner-a", "pause", "a" * 64, False),
        ("recovery", "owner-a"),
    ]


def test_queue_control_route_returns_conflict_for_stale_generation(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_queue_stale_router_test_module")

    class Service:
        async def set_queue_control(self, uid, *, action, generation_id, auto_continue=False):
            raise artwork.MemoryArtworkError("memory_artwork_queue_generation_stale")

    monkeypatch.setattr(router_module, "MemoryArtworkService", Service)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_exact_firebase_uid] = lambda: "owner-a"

    response = TestClient(app).post(
        "/v1/ella/memory-artwork/queue/control",
        json={"action": "resume", "generation_id": "a" * 64},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "memory_artwork_queue_generation_stale"


def test_mounted_internal_process_route_uses_durable_worker_job_claim(monkeypatch):
    router_module = _load_memory_artwork_router_module("ella_memory_artwork_internal_router_test_module")

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
        async def enqueue(self, uid, memory_id, *, request_mode):
            calls.append(("enqueue", uid, memory_id, request_mode))
            return {"outcome": "reserved", "status": "generating"}

    monkeypatch.setattr(artwork, "MemoryArtworkService", Service)

    assert asyncio.run(artwork.enqueue_after_terminal_enrichment("owner-a", "memory-1")) is None
    assert calls == [("enqueue", "owner-a", "memory-1", "automatic")]


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


def test_first_party_provider_sends_bounded_owner_scoped_designer_brief(monkeypatch, tmp_path):
    token_file = tmp_path / "artwork-service-token"
    token_file.write_text("test-service-token-value-0000000000000000")
    token_file.chmod(0o600)
    monkeypatch.setenv(artwork.PROVIDER_URL_ENV, "https://artwork.internal/v1/ella/internal/artwork/render")
    monkeypatch.setenv(artwork.PROVIDER_ALLOWED_HOST_ENV, "artwork.internal")
    monkeypatch.setenv(artwork.PROVIDER_TOKEN_FILE_ENV, str(token_file))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            content=b"private-image",
            headers={
                "Content-Type": "image/png",
                "X-Ella-Image-Width": "1536",
                "X-Ella-Image-Height": "1024",
            },
        )

    context = artwork.ArtworkProviderContext(
        owner_uid="owner-a",
        profile_binding="profile-owner-a",
        authority_generation=7,
        source_revision="summary-v3",
        consent_version="ai-data-processors-v10-test",
        title="A winter walk",
        summary="Two friends paused beside a frozen lake in blue evening light.",
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.FirstPartyHTTPArtworkProvider(client=client)
            return await provider.generate(
                prompt="This provider must not forward an opaque prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="job-123",
                context=context,
            )

    generated = asyncio.run(scenario())

    assert generated.image_bytes == b"private-image"
    assert len(calls) == 1
    request = calls[0]
    payload = json.loads(request.content)
    assert request.headers["X-Ella-Contract"] == artwork.ARTWORK_PROVIDER_CONTRACT_VERSION
    assert request.headers["Idempotency-Key"] == "job-123"
    assert payload == {
        "schemaVersion": "ella.artwork.brief.v1",
        "jobId": "job-123",
        "ownerUid": "owner-a",
        "profileBinding": "profile-owner-a",
        "authorityGeneration": 7,
        "sourceRevision": "summary-v3",
        "consentVersion": "ai-data-processors-v10-test",
        "synthetic": False,
        "style": "gouache",
        "title": "A winter walk",
        "summary": "Two friends paused beside a frozen lake in blue evening light.",
    }
    assert "opaque prompt" not in request.content.decode()


def test_first_party_provider_enforces_wall_clock_deadline(monkeypatch, tmp_path):
    token_file = tmp_path / "artwork-service-token"
    token_file.write_text("test-service-token-value-0000000000000000")
    token_file.chmod(0o600)
    monkeypatch.setenv(artwork.PROVIDER_URL_ENV, "https://artwork.internal/v1/ella/internal/artwork/render")
    monkeypatch.setenv(artwork.PROVIDER_ALLOWED_HOST_ENV, "artwork.internal")
    monkeypatch.setenv(artwork.PROVIDER_TOKEN_FILE_ENV, str(token_file))

    async def never_finishes(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("wall-clock timeout did not cancel provider request")

    monkeypatch.setattr(artwork, "_bounded_provider_post", never_finishes)
    context = artwork.ArtworkProviderContext(
        owner_uid="owner-a",
        profile_binding="profile-owner-a",
        authority_generation=7,
        source_revision="summary-v3",
        consent_version="ai-data-processors-v10-test",
        title="A winter walk",
        summary="Two friends paused beside a frozen lake in blue evening light.",
    )
    provider = artwork.FirstPartyHTTPArtworkProvider(client=object())
    provider.timeout_seconds = 0.01

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(
            provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="job-123",
                context=context,
            )
        )

    assert failure.value.code == "memory_artwork_provider_unavailable"
    assert failure.value.retryable is True


def test_first_party_provider_rejects_missing_context_before_egress(monkeypatch, tmp_path):
    token_file = tmp_path / "artwork-service-token"
    token_file.write_text("test-service-token-value-0000000000000000")
    token_file.chmod(0o600)
    monkeypatch.setenv(artwork.PROVIDER_URL_ENV, "https://artwork.internal/v1/ella/internal/artwork/render")
    monkeypatch.setenv(artwork.PROVIDER_ALLOWED_HOST_ENV, "artwork.internal")
    monkeypatch.setenv(artwork.PROVIDER_TOKEN_FILE_ENV, str(token_file))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = artwork.FirstPartyHTTPArtworkProvider(client=client)
            return await provider.generate(
                prompt="Synthetic test prompt.",
                style_version=artwork.DEFAULT_STYLE_VERSION,
                idempotency_key="job-123",
            )

    with pytest.raises(artwork.MemoryArtworkError) as failure:
        asyncio.run(scenario())

    assert failure.value.code == "memory_artwork_provider_context_missing"
    assert calls == []
