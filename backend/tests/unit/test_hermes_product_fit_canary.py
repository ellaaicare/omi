import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

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


def test_enrichment_requires_one_correlated_duplicate_safe_result_without_chat_identity():
    config = _config()

    class ContaminatedEnrichmentAdapter(FakeAdapter):
        async def enrichment(self):
            observed = await super().enrichment()
            return canary.EnrichmentObservation(
                **{
                    **observed.__dict__,
                    "chat_identity_absent": False,
                }
            )

    report = asyncio.run(canary.run_harness(config, ContaminatedEnrichmentAdapter(config)))
    enrichment = next(stage for stage in report.stages if stage.scenario == "E")
    assert enrichment.status == "FAIL"
    assert report.verdicts["enrichment"] == "FAIL"


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
