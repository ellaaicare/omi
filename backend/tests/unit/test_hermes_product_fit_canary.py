import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

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
    return canary.CanaryConfig.from_mapping(raw)


def _token_for(uid):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": uid}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature-padding-that-keeps-the-token-long"


def _fixture_authority(config):
    return {
        "user_id": config.account_id,
        "omi_uid": config.uid,
        "user_status": "ACTIVE",
        "profile_class": "synthetic",
        "binding_id": config.binding_id,
        "binding_user_id": config.account_id,
        "account_user_id": config.account_id,
        "profile_user_id": config.profile_id,
        "provider": "hermes_cloud",
        "binding_status": "internal_canary",
        "health_state": "healthy",
        "active": True,
        "expected_model": config.expected_model,
        "decision": "granted",
        "consent_profile_binding_id": canary.ai_consent.derive_profile_binding_id(
            account_uid=config.uid,
            profile_uid=config.uid,
        ),
        "policy_version": canary.ai_consent.CURRENT_POLICY_VERSION,
        "processor_set_hash": canary.ai_consent.CURRENT_PROCESSOR_SET_HASH,
        "scope_version": canary.ai_consent.CURRENT_SCOPE_VERSION,
        "scope_hash": canary.ai_consent.CURRENT_SCOPE_HASH,
        "authority_epoch": config.consent_epoch,
        "entitlement_status": "active",
        "entitlement_authority_epoch": config.consent_epoch,
        "transcript_target_ready": True,
    }


class FakeFixtureStore:
    def __init__(self, *, authority=None, authority_error=None):
        self.authority = authority
        self.authority_error = authority_error
        self.conversations = {}
        self.vectors = set()
        self.authority_checks = 0
        self.create_calls = 0
        self.summary_calls = 0
        self.delete_calls = 0
        self.vector_delete_calls = 0

    async def assert_prepare_authority(self, config):
        self.authority_checks += 1
        if self.authority_error:
            raise canary.HarnessRefusal(self.authority_error)
        if self.authority is not None:
            canary._validate_fixture_authority(config, self.authority)

    async def get_conversation(self, uid, conversation_id):
        value = self.conversations.get((uid, conversation_id))
        return json.loads(json.dumps(value, default=str)) if value is not None else None

    async def create_conversation(self, uid, conversation):
        self.create_calls += 1
        self.conversations[(uid, conversation.id)] = conversation.model_dump()

    async def ensure_summary_version(self, uid, conversation_id):
        self.summary_calls += 1
        conversation = self.conversations[(uid, conversation_id)]
        if not conversation.get("active_summary_version_id"):
            version_id = "fixture-summary-version"
            conversation.update(
                {
                    "summary_versions": [
                        {
                            "id": version_id,
                            "created_at": conversation["created_at"],
                            "source": "legacy",
                            "kind": "legacy_current",
                            "title": conversation["structured"]["title"],
                            "overview": conversation["structured"]["overview"],
                            "emoji": conversation["structured"]["emoji"],
                            "category": conversation["structured"]["category"],
                            "correction_id": None,
                            "based_on_version_id": None,
                            "is_active": True,
                        }
                    ],
                    "active_summary_version_id": version_id,
                }
            )
        return {
            "status": "ready",
            "active_summary_version_id": conversation["active_summary_version_id"],
            "conversation": conversation,
        }

    async def delete_conversation(self, uid, conversation_id):
        self.delete_calls += 1
        self.conversations.pop((uid, conversation_id), None)

    async def delete_vector(self, uid, conversation_id):
        self.vector_delete_calls += 1
        self.vectors.discard((uid, conversation_id))

    async def vector_exists(self, uid, conversation_id):
        return (uid, conversation_id) in self.vectors


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


def test_config_rejects_real_identity_session_drift_urls_and_literal_secrets():
    cases = (
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
    )

    for mutator, code in cases:
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


def test_fixture_prepare_creates_one_synthetic_conversation_and_production_identities(monkeypatch):
    config = _fixture_config()
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(config.uid))
    store = FakeFixtureStore()

    receipt = asyncio.run(canary.prepare_fixture(config, store))
    conversation = store.conversations[(config.uid, receipt["conversation_id"])]
    production_identity = canary.build_enrichment_identity(
        uid=config.uid,
        conversation_id=receipt["conversation_id"],
        conversation=conversation,
    )

    assert store.authority_checks == 1
    assert store.create_calls == 1
    assert len(store.conversations) == 1
    assert conversation["source"].value == "external_integration"
    assert conversation["visibility"].value == "private"
    assert conversation["status"].value == "completed"
    assert conversation["discarded"] is False
    assert conversation.get("enrichment_state") is None
    assert receipt["active_summary_version_id"] == conversation["active_summary_version_id"]
    assert receipt["transcript_sha256"] == production_identity.transcript_sha256
    assert receipt["enrichment_client_interaction_id"] == production_identity.client_interaction_id
    assert receipt["enrichment_job_id"] == production_identity.job_id
    assert receipt["provider_calls"] == 0
    assert receipt["enrichment_success_preseeded"] is False


def test_fixture_prepare_refuses_real_stale_or_mismatched_owner_without_writes(monkeypatch):
    config = _fixture_config()
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(config.uid))

    mutations = (
        lambda value: value.update(profile_class="real"),
        lambda value: value.update(policy_version="ai-data-processors-stale"),
        lambda value: value.update(account_user_id="99999999-9999-4999-8999-999999999999"),
    )
    for mutate in mutations:
        authority = _fixture_authority(config)
        mutate(authority)
        store = FakeFixtureStore(authority=authority)
        with pytest.raises(canary.HarnessRefusal, match="fixture_authority_mismatch"):
            asyncio.run(canary.prepare_fixture(config, store))
        assert store.create_calls == 0
        assert store.summary_calls == 0
        assert store.conversations == {}

    real_config = canary.CanaryConfig(**{**config.__dict__, "uid": "real-user"})
    real_store = FakeFixtureStore()
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(real_config.uid))
    with pytest.raises(canary.HarnessRefusal, match="firebase_token_subject_mismatch"):
        asyncio.run(canary.prepare_fixture(real_config, real_store))
    assert real_store.authority_checks == 0
    assert real_store.create_calls == 0


def test_fixture_prepare_retry_is_idempotent_and_never_invokes_runtime(monkeypatch):
    config = _fixture_config()
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(config.uid))
    store = FakeFixtureStore()

    class RefuseRuntimeConstruction:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("runtime must not be constructed")

    monkeypatch.setattr(canary, "LiveCanaryAdapter", RefuseRuntimeConstruction)
    first = asyncio.run(canary.prepare_fixture(config, store))
    second = asyncio.run(canary.prepare_fixture(config, store))

    assert first == second
    assert store.create_calls == 1
    assert store.summary_calls == 2
    assert len(store.conversations) == 1


def test_fixture_show_and_cleanup_are_content_free_and_exact_id_only(monkeypatch):
    config = _fixture_config()
    monkeypatch.setenv("ELLA_CANARY_FIREBASE_ID_TOKEN", _token_for(config.uid))
    store = FakeFixtureStore()
    receipt = asyncio.run(canary.prepare_fixture(config, store))
    key = (config.uid, receipt["conversation_id"])
    store.vectors.add(key)

    shown = asyncio.run(canary.show_fixture(config, receipt, store))
    assert shown == receipt
    rendered = json.dumps(shown, sort_keys=True)
    assert "Synthetic product-fit fixture turn" not in rendered
    assert "Synthetic data for" not in rendered

    with pytest.raises(canary.HarnessRefusal, match="fixture_cleanup_confirmation_mismatch"):
        asyncio.run(
            canary.cleanup_fixture(
                config,
                receipt,
                store,
                confirm_conversation_id="wrong-conversation",
            )
        )
    assert store.delete_calls == 0
    assert store.vector_delete_calls == 0

    cleaned = asyncio.run(
        canary.cleanup_fixture(
            config,
            receipt,
            store,
            confirm_conversation_id=receipt["conversation_id"],
        )
    )
    assert cleaned == {
        "schema_version": canary.FIXTURE_SCHEMA,
        "status": "cleaned",
        "uid_sha256": canary._sha256(config.uid),
        "conversation_id_sha256": canary._sha256(receipt["conversation_id"]),
        "conversation_absent": True,
        "vector_absent": True,
        "content_free": True,
    }
    assert key not in store.conversations
    assert key not in store.vectors
    assert store.delete_calls == 1
    assert store.vector_delete_calls == 1
