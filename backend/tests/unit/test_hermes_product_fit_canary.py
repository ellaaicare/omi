import asyncio
import base64
import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

from ella.routers.hermes_cloud_enrichment import (
    create_hermes_cloud_enrichment_router,
)

from scripts import hermes_product_fit_canary as canary

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "hermes_product_fit_callback_v1.json"


def _config_mapping():
    return {
        "schema_version": canary.CONFIG_SCHEMA,
        "run_id": "synthetic-fit-20260730",
        "selectors": {
            "uid": "staging-synthetic-product-fit",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "profile_id": "22222222-2222-4222-8222-222222222222",
            "binding_id": "33333333-3333-4333-8333-333333333333",
            "consent_epoch": "44444444-4444-4444-8444-444444444444",
            "expected_model": "gpt-5.6-terra",
            "chat_channel": "ios_chat",
            "primary_session_key": "ella:canary:primary",
            "isolated_session_key": "ella:canary:isolated",
        },
        "backend": {
            "base_url": "https://omi.example.test",
            "auth_token_ref": "env:ELLA_CANARY_FIREBASE_ID_TOKEN",
            "enrichment_token_ref": "env:ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN",
        },
        "broker": {
            "base_url": "http://127.0.0.1:18097",
            "allowed_host": "127.0.0.1",
            "service_token_ref": "env:ELLA_HERMES_BROKER_SERVICE_TOKEN",
            "poll_interval_seconds": 0.1,
            "poll_timeout_seconds": 5.0,
            "deadline_seconds": 30,
        },
        "voice_memory": {
            "conversation_id": "synthetic-memory-conversation",
            "active_summary_version_id": "synthetic-summary-version",
            "pack_sha256": "a" * 64,
        },
        "enrichment": {
            "conversation_id": "synthetic-enrichment-conversation",
            "transcript_sha256": "b" * 64,
        },
        "max_latency_ms": 120_000,
    }


def _config():
    return canary.CanaryConfig.from_mapping(_config_mapping())


def _fixture_config():
    raw = _config_mapping()
    raw["selectors"]["profile_id"] = raw["selectors"]["account_id"]
    raw["enrichment"]["conversation_id"] = raw["voice_memory"]["conversation_id"]
    return canary.CanaryConfig.from_mapping(raw)


def _token_for(uid):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": uid}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature-padding-for-content-safe-test"


def _off_receipt(config):
    return {
        "schema_version": "ella-hermes-canary-off-receipt-v1",
        "uid_sha256": canary._sha256(config.uid),
        "flags_off": True,
        "selectors_empty": True,
        "workflows_off": True,
        "content_free": True,
    }


def _fixture_environment(monkeypatch, config):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(config.uid))
    for name in canary.FIXTURE_GLOBAL_FLAGS_REQUIRED_FALSE:
        monkeypatch.delenv(name, raising=False)
    for name in canary.FIXTURE_SELECTORS_REQUIRED_EMPTY:
        monkeypatch.delenv(name, raising=False)


class FakeFixtureBackend:
    def __init__(self, config):
        self.config = config
        self.conversation = None
        self.put_calls = 0
        self.summary_calls = 0
        self.vector_ids = {config.voice_conversation_id}
        self.deleted_conversations = []
        self.deleted_vectors = []
        self.runtime_calls = 0
        self.authority_value = {
            "user": {
                "id": config.account_id,
                "omi_uid": config.uid,
                "status": "ACTIVE",
                "profile_class": "synthetic",
            },
            "binding": {
                "id": config.binding_id,
                "user_id": config.account_id,
                "account_user_id": config.account_id,
                "profile_user_id": config.profile_id,
                "omi_uid": config.uid,
                "provider": "hermes_cloud",
                "profile_class": "synthetic",
                "active": True,
                "status": "internal_canary",
                "health_state": "healthy",
            },
            "entitlement": {
                "uid": config.uid,
                "consent_authority_epoch": config.consent_epoch,
                "consent_policy_version": "ai-data-processors-v8",
                "status": "invited",
            },
            "consent": {
                "subject_uid": config.uid,
                "authorized": True,
                "consent": {
                    "decision": "granted",
                    "policy_version": "ai-data-processors-v8",
                    "receipt_id": "aicr_content_free_fixture",
                    "profile_binding_id": canary.ai_consent.derive_profile_binding_id(
                        account_uid=config.uid,
                        profile_uid=config.uid,
                    ),
                },
            },
        }

    async def authority(self, config):
        assert config == self.config
        return copy.deepcopy(self.authority_value)

    def get_conversation(self, uid, conversation_id):
        assert uid == self.config.uid
        assert conversation_id == self.config.voice_conversation_id
        return copy.deepcopy(self.conversation)

    def put_conversation(self, uid, conversation):
        assert uid == self.config.uid
        self.put_calls += 1
        self.conversation = conversation.model_dump()

    def ensure_summary_version(self, uid, conversation_id):
        assert uid == self.config.uid
        assert conversation_id == self.config.voice_conversation_id
        self.summary_calls += 1
        if not self.conversation.get("summary_versions"):
            summary_version = {
                "id": "synthetic-legacy-summary-version",
                "created_at": self.conversation["created_at"],
                "source": "legacy",
                "kind": "legacy_current",
                "title": self.conversation["structured"]["title"],
                "overview": self.conversation["structured"]["overview"],
                "emoji": self.conversation["structured"]["emoji"],
                "category": self.conversation["structured"]["category"],
                "correction_id": None,
                "based_on_version_id": None,
                "is_active": True,
            }
            self.conversation.update(
                {
                    "summary_versions": [summary_version],
                    "active_summary_version_id": summary_version["id"],
                }
            )
        return {
            "status": "ready",
            "active_summary_version_id": self.conversation["active_summary_version_id"],
            "conversation": copy.deepcopy(self.conversation),
        }

    def delete_conversation(self, uid, conversation_id):
        assert uid == self.config.uid
        self.deleted_conversations.append(conversation_id)
        self.conversation = None

    def delete_vector(self, uid, conversation_id):
        assert uid == self.config.uid
        self.deleted_vectors.append(conversation_id)
        self.vector_ids.discard(conversation_id)

    def vector_exists(self, uid, conversation_id):
        assert uid == self.config.uid
        return conversation_id in self.vector_ids


class FakeAdapter:
    def __init__(self, config):
        self.config = config
        self.nonce = ""
        self.responses = {}
        self.cleaned = False

    async def chat(self, *, session_key, source_event_id, prompt):
        if "Remember this turn-local synthetic nonce:" in prompt:
            self.nonce = prompt.split("nonce: ", 1)[1].split(".", 1)[0]
            text = "ACK"
        elif "previous turn" in prompt or "retained in this" in prompt:
            text = self.nonce
        elif "If a turn-local synthetic nonce exists" in prompt:
            text = "NO-NONCE"
        else:
            text = prompt.rsplit(": ", 1)[1]
        response_id = f"response:{canary._sha256(source_event_id)[:24]}"
        self.responses[source_event_id] = (text, response_id)
        return canary.ChatObservation(
            text=text,
            response_id=response_id,
            request_id=f"request:{canary._sha256(source_event_id)[:24]}",
            correlation_id=f"correlation:{canary._sha256(source_event_id)[:24]}",
            duplicate=False,
            statuses=("pending", "dispatching", "awaiting_callback", "writeback_completed"),
            terminal_frames=1,
            admission_posts=1,
            callback_outcomes=1,
            generation=1,
            latency_ms=25,
        )

    async def memory_pack(self):
        return canary.MemoryObservation(
            scoped_pack_sha256=self.config.memory_pack_sha256,
            conversation_id_matches=True,
            summary_version_matches=True,
            unscoped_memory_absent=True,
            latency_ms=20,
        )

    async def enrichment(self):
        return canary.EnrichmentObservation(
            status="applied",
            correlation_matches=True,
            transcript_hash_matches=True,
            duplicate_replay=True,
            chat_identity_absent=True,
            latency_ms=30,
        )

    async def replay(self, original, *, source_event_id, prompt):
        text, response_id = self.responses[source_event_id]
        return canary.ReplayObservation(
            duplicate_ingress=True,
            same_response_hash=canary._sha256(text) == canary._sha256(original.text),
            same_response_id=response_id == original.response_id,
            missing_outcome_refused=True,
            wrong_correlation_refused=True,
            wrong_session_refused=True,
            timeout_refused=True,
            partial_after_terminal_refused=True,
            latency_ms=10,
        )

    async def cleanup(self, *, off_receipt):
        canary._validate_off_receipt(off_receipt, self.config)
        self.cleaned = True
        return {
            "status": "cleaned",
            "uid_sha256": canary._sha256(self.config.uid),
            "content_free": True,
        }


def test_scenarios_a_to_f_pass_with_content_free_stage_tables():
    config = _config()
    report = asyncio.run(canary.run_harness(config, FakeAdapter(config)))
    rendered = report.render()

    assert set(report.verdicts.values()) == {"PASS"}
    assert [f"Scenario {letter}" for letter in "ABCDEF"] == [
        line for line in rendered.splitlines() if line.startswith("Scenario ")
    ]
    assert "data: " not in rendered
    assert "done: " not in rendered
    for label in ("full-response", "same-session-nonce"):
        assert canary._marker(config, label) not in rendered
    assert "response_sha256=" in rendered
    assert "event_sequence=data>done" in rendered


def test_scenario_a_requires_one_terminal_callback_and_lossless_hash():
    config = _config()

    class DuplicateTerminalAdapter(FakeAdapter):
        async def chat(self, **kwargs):
            observed = await super().chat(**kwargs)
            if "full-response" in kwargs["prompt"].lower():
                return canary.ChatObservation(
                    **{
                        **observed.__dict__,
                        "statuses": ("pending", "writeback_completed", "writeback_completed"),
                        "terminal_frames": 2,
                        "callback_outcomes": 2,
                    }
                )
            return observed

    report = asyncio.run(canary.run_harness(config, DuplicateTerminalAdapter(config)))

    transport = next(stage for stage in report.stages if stage.name == "broker_transport")
    assert transport.status == "FAIL"
    assert report.verdicts["chat transport"] == "FAIL"


def test_session_continuity_and_isolation_use_distinct_stable_projections():
    config = _config()

    assert canary._session_id(config, config.primary_session_key) == canary._session_id(
        config,
        config.primary_session_key,
    )
    assert canary._session_id(config, config.primary_session_key) != canary._session_id(
        config,
        config.isolated_session_key,
    )

    report = asyncio.run(canary.run_harness(config, FakeAdapter(config)))
    continuity = next(stage for stage in report.stages if stage.scenario == "B")
    isolation = next(stage for stage in report.stages if stage.scenario == "C")
    assert continuity.status == "PASS"
    assert isolation.status == "PASS"


def test_voice_memory_pack_is_hash_only_and_unscoped_session_isolated():
    config = _config()

    class LeakingMemoryAdapter(FakeAdapter):
        async def memory_pack(self):
            observed = await super().memory_pack()
            return canary.MemoryObservation(
                **{
                    **observed.__dict__,
                    "unscoped_memory_absent": False,
                }
            )

    report = asyncio.run(canary.run_harness(config, LeakingMemoryAdapter(config)))
    memory = next(stage for stage in report.stages if stage.scenario == "D")
    assert memory.status == "FAIL"
    assert "synthetic-memory-conversation" not in report.render()
    assert report.verdicts["profile memory pack"] == "FAIL"


def test_enrichment_requires_one_correlated_duplicate_safe_result_without_chat_identity(monkeypatch):
    config = _config()
    token = "e" * 32
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", token)
    expected_interaction_id, _ = canary.production_enrichment_interaction_identity(
        config.uid,
        config.enrichment_conversation_id,
        config.enrichment_transcript_sha256,
    )
    old_run_id_digest = canary._sha256(f"{config.run_id}|{config.uid}|{config.enrichment_conversation_id}|enrichment")
    assert expected_interaction_id != f"omi-enrichment:{old_run_id_digest}"

    class ContractService:
        def __init__(self):
            self.calls = []

        async def enrich(self, **kwargs):
            assert kwargs["expected_client_interaction_id"] == expected_interaction_id
            assert kwargs["expected_transcript_sha256"] == config.enrichment_transcript_sha256
            self.calls.append(kwargs)
            return SimpleNamespace(
                conversation_id=kwargs["conversation_id"],
                runtime_binding_id=config.binding_id,
                runtime_interaction_id="55555555-5555-4555-8555-555555555555",
                active_summary_version_id=config.voice_summary_version_id,
                canonical_user_event_id="canonical-user-event",
                canonical_assistant_event_id="canonical-assistant-event",
                transcript_sha256=config.enrichment_transcript_sha256,
                summary_sha256="c" * 64,
                provider_response_present=True,
                duplicate=len(self.calls) > 1,
                client_interaction_id=kwargs["expected_client_interaction_id"],
            )

    service = ContractService()

    async def service_factory():
        return service

    app = FastAPI()
    app.include_router(create_hermes_cloud_enrichment_router(service_factory))
    original_async_client = canary.httpx.AsyncClient

    def asgi_client(*_args, **kwargs):
        return original_async_client(
            transport=canary.httpx.ASGITransport(app=app),
            base_url=config.backend_base_url,
            timeout=kwargs.get("timeout"),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr(canary.httpx, "AsyncClient", asgi_client)
    observed = asyncio.run(canary.LiveCanaryAdapter(config).enrichment())

    assert observed.status == "applied"
    assert observed.correlation_matches is True
    assert observed.transcript_hash_matches is True
    assert observed.duplicate_replay is True
    assert observed.chat_identity_absent is True
    assert [call["expected_client_interaction_id"] for call in service.calls] == [
        expected_interaction_id,
        expected_interaction_id,
    ]
    assert [call["expected_transcript_sha256"] for call in service.calls] == [
        config.enrichment_transcript_sha256,
        config.enrichment_transcript_sha256,
    ]
    canonical_result = {
        "canonical_user_event_id": "canonical-user-event",
        "canonical_assistant_event_id": "canonical-assistant-event",
    }
    assert canary._enrichment_chat_identity_absent(canonical_result)
    assert not canary._enrichment_chat_identity_absent(
        {
            **canonical_result,
            "session_id": "ella:broker-session:v1:leaked",
        }
    )


def test_replay_errors_fail_closed_for_outcome_correlation_session_timeout_and_ordering():
    config = _config()
    checks = asyncio.run(
        canary._contract_failure_checks(
            config,
            canary._source_event_id(config, "contract"),
        )
    )

    assert checks == {
        "missing_outcome": True,
        "wrong_session": True,
        "wrong_correlation": True,
        "timeout": True,
    }
    assert canary._status_sequence_valid(("pending", "dispatching", "writeback_completed"))
    assert not canary._status_sequence_valid(("pending", "writeback_completed", "writeback_pending"))
    assert not canary._status_sequence_valid(("pending", "writeback_completed", "writeback_completed"))


def test_callback_fixture_matches_ella_callback_v1_and_requires_outcome():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = _config()

    assert fixture["schema_version"] == canary.CALLBACK_SCHEMA
    assert fixture["outcome"] == "success"
    assert set(fixture) == {
        "schema_version",
        "correlation_id",
        "delivery_id",
        "outcome",
        "response",
        "sent_at",
    }
    assert fixture["response"]["session_key"] == "ella:canary:primary"
    assert fixture["response"]["canonical_user_event_id"].startswith("canary-event:")
    assert fixture["response"]["session_id"] == canary._session_id(config, config.primary_session_key)

    mapped = canary.HermesBrokerClient(config.broker)._map_chat_result(
        {
            "outcome": fixture["outcome"],
            "result": fixture["response"],
            "diagnostic": {
                "stage": "broker_writeback",
                "reason": "writeback_completed",
                "generation": 1,
            },
        },
        request_id="canary-request",
        correlation_id=fixture["correlation_id"],
        expected_model=config.expected_model,
        session_key=config.primary_session_key,
        session_id=fixture["response"]["session_id"],
        source_event_id=fixture["response"]["canonical_user_event_id"],
        admission_duplicate=False,
    )
    assert mapped.text == "CANARY-CONTENT-SAFE"
    assert mapped.diagnostic == {
        "stage": "broker_writeback",
        "reason": "writeback_completed",
        "generation": 1,
    }


@pytest.mark.parametrize(
    ("mutator", "code"),
    (
        (
            lambda raw: raw["selectors"].update(uid="realcryptoplato"),
            "synthetic_uid_required",
        ),
        (
            lambda raw: raw["selectors"].update(
                isolated_session_key=raw["selectors"]["primary_session_key"],
            ),
            "distinct_sessions_required",
        ),
        (
            lambda raw: raw["backend"].update(base_url="http://127.0.0.1:8000"),
            "backend_base_url_not_allowlisted",
        ),
        (
            lambda raw: raw["broker"].update(base_url="http://127.0.0.1:18098"),
            "hermes_broker_prototype_url_not_allowlisted",
        ),
        (
            lambda raw: raw["broker"].update(service_token_ref="literal-secret"),
            "broker_service_token_ref_invalid",
        ),
    ),
)
def test_config_rejects_real_identity_session_drift_urls_and_literal_secrets(mutator, code):
    raw = _config_mapping()
    mutator(raw)

    with pytest.raises((canary.HarnessRefusal, canary.ProvisioningError), match=code):
        canary.CanaryConfig.from_mapping(raw)


def test_protected_config_and_off_receipt_require_exact_owner_modes(tmp_path):
    protected_root = tmp_path / "ella"
    protected_root.mkdir(mode=0o700)
    protected = protected_root / "config.json"
    protected.write_text("{}\n", encoding="utf-8")
    protected.chmod(0o400)

    assert (
        canary._assert_protected_file(
            protected,
            approved_roots=(tmp_path,),
            expected_owner_uid=os.geteuid(),
        )
        == protected.resolve()
    )
    protected.chmod(0o644)
    with pytest.raises(canary.HarnessRefusal, match="protected_file_metadata_refused"):
        canary._assert_protected_file(
            protected,
            approved_roots=(tmp_path,),
            expected_owner_uid=os.geteuid(),
        )

    def token_for(uid):
        payload = base64.urlsafe_b64encode(json.dumps({"sub": uid}).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    canary._require_firebase_subject(
        token_for("staging-synthetic-product-fit"),
        "staging-synthetic-product-fit",
    )
    with pytest.raises(canary.HarnessRefusal, match="firebase_token_subject_mismatch"):
        canary._require_firebase_subject(
            token_for("real-user"),
            "staging-synthetic-product-fit",
        )


def test_cleanup_requires_content_free_all_off_receipt_for_exact_synthetic_uid():
    config = _config()
    adapter = FakeAdapter(config)
    receipt = {
        "schema_version": "ella-hermes-canary-off-receipt-v1",
        "uid_sha256": canary._sha256(config.uid),
        "flags_off": True,
        "selectors_empty": True,
        "workflows_off": True,
        "content_free": True,
    }

    result = asyncio.run(adapter.cleanup(off_receipt=receipt))
    assert adapter.cleaned is True
    assert result["content_free"] is True
    with pytest.raises(canary.HarnessRefusal, match="cleanup_off_receipt_invalid"):
        canary._validate_off_receipt({**receipt, "workflows_off": False}, config)


def test_missing_optional_live_surface_reports_not_tested_not_pass():
    config = _config()

    class MissingSurfaceAdapter(FakeAdapter):
        async def memory_pack(self):
            raise canary.ScenarioNotTested("voice_context_unavailable")

    report = asyncio.run(canary.run_harness(config, MissingSurfaceAdapter(config)))

    memory = next(stage for stage in report.stages if stage.scenario == "D")
    assert memory.status == "NOT TESTED"
    assert report.verdicts["profile memory pack"] == "NOT TESTED"
    assert canary._exit_code(report) == 2


def test_fixture_prepare_creates_one_synthetic_conversation_and_production_identities(monkeypatch, tmp_path):
    config = _fixture_config()
    _fixture_environment(monkeypatch, config)
    backend = FakeFixtureBackend(config)
    protected_root = tmp_path / "fixture"
    protected_root.mkdir(mode=0o700)
    receipt_path = protected_root / "fixture-receipt.json"

    result = asyncio.run(
        canary.fixture_prepare(
            config,
            off_receipt=_off_receipt(config),
            receipt_path=receipt_path,
            backend=backend,
            approved_roots=(tmp_path,),
            expected_owner_uid=os.geteuid(),
        )
    )

    assert backend.put_calls == 1
    assert backend.summary_calls == 1
    conversation = backend.conversation
    assert conversation["discarded"] is False
    assert conversation["visibility"] == canary.ConversationVisibility.private
    assert conversation["status"] == canary.ConversationStatus.completed
    assert conversation["source"] == canary.ConversationSource.external_integration
    assert conversation["structured"]["title"]
    assert conversation["structured"]["overview"]
    assert len(conversation["summary_versions"]) == 1
    assert conversation["summary_versions"][0]["source"] == "legacy"
    assert conversation["summary_versions"][0]["kind"] == "legacy_current"
    assert conversation["summary_versions"][0]["is_active"] is True
    assert "enrichment_state" not in conversation
    assert "canonical_events" not in conversation
    assert "import_jobs" not in conversation
    identity = canary.build_enrichment_identity(
        uid=config.uid,
        conversation_id=config.voice_conversation_id,
        conversation=conversation,
    )
    assert result["client_interaction_id"] == identity.client_interaction_id
    assert result["job_id"] == identity.job_id
    assert result["transcript_sha256"] == identity.transcript_sha256
    assert result["content_free"] is True
    assert result["status"] == "prepared"
    assert receipt_path.stat().st_mode & 0o777 == 0o400
    stored = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert stored["fixture_shape_sha256"] == result["fixture_shape_sha256"]
    assert not any("Synthetic profile-memory marker" in str(value) for value in stored.values())


def test_fixture_prepare_refuses_real_stale_or_mismatched_owner_without_writes(monkeypatch, tmp_path):
    config = _fixture_config()
    protected_root = tmp_path / "fixture"
    protected_root.mkdir(mode=0o700)

    real_config = replace(config, uid="real-user")
    real_backend = FakeFixtureBackend(real_config)
    _fixture_environment(monkeypatch, real_config)
    with pytest.raises(canary.HarnessRefusal, match="firebase_token_subject_mismatch"):
        asyncio.run(
            canary.fixture_prepare(
                real_config,
                off_receipt=_off_receipt(real_config),
                receipt_path=protected_root / "real.json",
                backend=real_backend,
                approved_roots=(tmp_path,),
                expected_owner_uid=os.geteuid(),
            )
        )
    assert real_backend.put_calls == 0

    _fixture_environment(monkeypatch, config)
    stale_backend = FakeFixtureBackend(config)
    stale_backend.authority_value["consent"]["consent"]["policy_version"] = "ai-data-processors-v7"
    with pytest.raises(canary.HarnessRefusal, match="fixture_consent_mismatch"):
        asyncio.run(
            canary.fixture_prepare(
                config,
                off_receipt=_off_receipt(config),
                receipt_path=protected_root / "stale.json",
                backend=stale_backend,
                approved_roots=(tmp_path,),
                expected_owner_uid=os.geteuid(),
            )
        )
    assert stale_backend.put_calls == 0

    owner_backend = FakeFixtureBackend(config)
    owner_backend.authority_value["user"]["profile_class"] = "real"
    with pytest.raises(canary.HarnessRefusal, match="fixture_owner_mismatch"):
        asyncio.run(
            canary.fixture_prepare(
                config,
                off_receipt=_off_receipt(config),
                receipt_path=protected_root / "owner.json",
                backend=owner_backend,
                approved_roots=(tmp_path,),
                expected_owner_uid=os.geteuid(),
            )
        )
    assert owner_backend.put_calls == 0

    selector_backend = FakeFixtureBackend(config)
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", config.uid)
    with pytest.raises(canary.HarnessRefusal, match="fixture_selector_active"):
        asyncio.run(
            canary.fixture_prepare(
                config,
                off_receipt=_off_receipt(config),
                receipt_path=protected_root / "selector.json",
                backend=selector_backend,
                approved_roots=(tmp_path,),
                expected_owner_uid=os.geteuid(),
            )
        )
    assert selector_backend.put_calls == 0

    _fixture_environment(monkeypatch, config)
    flag_backend = FakeFixtureBackend(config)
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED", "true")
    with pytest.raises(canary.HarnessRefusal, match="fixture_global_flag_active"):
        asyncio.run(
            canary.fixture_prepare(
                config,
                off_receipt=_off_receipt(config),
                receipt_path=protected_root / "flag.json",
                backend=flag_backend,
                approved_roots=(tmp_path,),
                expected_owner_uid=os.geteuid(),
            )
        )
    assert flag_backend.put_calls == 0


def test_fixture_prepare_retry_is_idempotent_and_never_invokes_runtime(monkeypatch, tmp_path):
    config = _fixture_config()
    _fixture_environment(monkeypatch, config)
    backend = FakeFixtureBackend(config)

    def runtime_forbidden(*_args, **_kwargs):
        backend.runtime_calls += 1
        raise AssertionError("fixture preparation invoked runtime")

    monkeypatch.setattr(canary, "LiveCanaryAdapter", runtime_forbidden)
    protected_root = tmp_path / "fixture"
    protected_root.mkdir(mode=0o700)
    receipt_path = protected_root / "fixture-receipt.json"
    kwargs = {
        "off_receipt": _off_receipt(config),
        "receipt_path": receipt_path,
        "backend": backend,
        "approved_roots": (tmp_path,),
        "expected_owner_uid": os.geteuid(),
    }

    first = asyncio.run(canary.fixture_prepare(config, **kwargs))
    second = asyncio.run(canary.fixture_prepare(config, **kwargs))

    assert first["receipt_id"] == second["receipt_id"]
    assert second["idempotent"] is True
    assert backend.put_calls == 1
    assert backend.runtime_calls == 0
    assert len(backend.conversation["summary_versions"]) == 1
    assert list(protected_root.iterdir()) == [receipt_path]


def test_fixture_show_and_cleanup_are_content_free_and_exact_id_only(monkeypatch, tmp_path, capsys):
    config = _fixture_config()
    _fixture_environment(monkeypatch, config)
    backend = FakeFixtureBackend(config)
    protected_root = tmp_path / "fixture"
    protected_root.mkdir(mode=0o700)
    receipt_path = protected_root / "fixture-receipt.json"
    common = {
        "off_receipt": _off_receipt(config),
        "receipt_path": receipt_path,
        "backend": backend,
        "approved_roots": (tmp_path,),
        "expected_owner_uid": os.geteuid(),
    }
    asyncio.run(canary.fixture_prepare(config, **common))

    shown = asyncio.run(canary.fixture_show(config, **common))
    assert shown["status"] == "ready"
    assert shown["content_free"] is True
    assert "transcript_segments" not in shown
    assert "structured" not in shown
    enriched_version = {
        **backend.conversation["summary_versions"][0],
        "id": "synthetic-hermes-summary-version",
        "source": "hermes_cloud",
        "kind": "hermes_enriched",
    }
    backend.conversation["summary_versions"][0]["is_active"] = False
    backend.conversation["summary_versions"].append(enriched_version)
    backend.conversation["active_summary_version_id"] = enriched_version["id"]
    backend.conversation["enrichment_state"] = {"status": "writeback_applied"}
    used = asyncio.run(canary.fixture_show(config, **common))
    assert used["status"] == "used"
    assert used["receipt_id"] == shown["receipt_id"]
    with pytest.raises(canary.HarnessRefusal, match="fixture_cleanup_confirmation_mismatch"):
        asyncio.run(
            canary.fixture_cleanup(
                config,
                confirm_conversation_id="wrong-conversation",
                **common,
            )
        )
    assert backend.conversation is not None
    assert backend.deleted_vectors == []

    cleaned = asyncio.run(
        canary.fixture_cleanup(
            config,
            confirm_conversation_id=config.voice_conversation_id,
            **common,
        )
    )

    assert cleaned == {
        "schema_version": canary.FIXTURE_RECEIPT_SCHEMA,
        "receipt_id": shown["receipt_id"],
        "status": "cleaned",
        "content_free": True,
        "conversation_id": config.voice_conversation_id,
        "conversation_absent": True,
        "vector_absent": True,
        "receipt_absent": True,
    }
    assert backend.deleted_vectors == [config.voice_conversation_id]
    assert backend.deleted_conversations == [config.voice_conversation_id]
    assert backend.conversation is None
    assert not receipt_path.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
