import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ella.services import hermes_cloud_runtime
from ella.routers.canonical_events import InMemoryCanonicalEventStore
from ella.services.hermes_cloud import HermesCloudTurn
from ella.services.hermes_cloud_runtime import (
    HermesCloudRuntimeService,
    HermesCloudTurnRequest,
    conservative_client_input_token_upper_bound,
)
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime


@pytest.fixture(autouse=True)
def cloud_synthetic_identity(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")


def _runtime(**updates) -> IsolatedRuntime:
    values = dict(
        uid="synthetic-user",
        binding_id="00000000-0000-0000-0000-000000000001",
        provider="hermes_cloud",
        status="internal_canary",
        profile_name="synthetic-profile",
        agent_id="hermes-cloud",
        runtime_instance_id="instance-a",
        gateway_url="https://cloud.example.test",
        gateway_token="server-secret",
        workspace_root="",
        honcho_workspace="workspace-a",
        observed_peer="user-a",
        observer_peer="companion-a",
        prompt_pack_version="prompt-v1",
        expected_model="model-a",
        model_context_window_tokens=16384,
        allowed_tools=(),
        required_capabilities=("responses_api", "session_key_header"),
        model_policy_version="models-v1",
        voice_policy_version="voice-v1",
        revision=2,
    )
    values.update(updates)
    return IsolatedRuntime(**values)


def _request(text: str = "Synthetic hello") -> HermesCloudTurnRequest:
    return HermesCloudTurnRequest(
        uid="synthetic-user",
        client_interaction_id="client-turn-1",
        correlation_id="trace-1",
        channel="synthetic_shadow",
        user_input=text,
        instructions="Synthetic test only.",
        started_at=datetime.now(timezone.utc),
        client_metadata={"synthetic": True},
    )


class FakeRepository:
    def __init__(self, *, previous_response_id=None, previous_response_usage=None):
        self.interaction = None
        self.receipts = {}
        self.failures = []
        self.previous_response_id = previous_response_id
        self.previous_response_usage = previous_response_usage or {}

    async def get_or_create_runtime_scope(self, **kwargs):
        assert kwargs["allow_shadow"] is False
        return {"id": "00000000-0000-0000-0000-000000000002", "session_key": "scope-a"}

    async def get_or_create_runtime_interaction(self, **kwargs):
        if self.interaction:
            if self.interaction["request_hash"] != kwargs["request_hash"]:
                from database.ella_provisioning import RuntimePoolClaimError

                raise RuntimePoolClaimError("runtime_interaction_payload_conflict")
            return dict(self.interaction)
        self.interaction = {
            "id": "00000000-0000-0000-0000-000000000003",
            "status": "pending",
            "request_hash": kwargs["request_hash"],
            "hermes_session_id": "interaction-a",
            "idempotency_key": "request-a",
            "previous_response_id": self.previous_response_id,
            "previous_response_usage": self.previous_response_usage,
            "provider_response_id": None,
            "usage": {},
        }
        return dict(self.interaction)

    async def claim_runtime_interaction(self, interaction_id):
        if self.interaction["status"] not in {"pending", "failed"}:
            return None
        self.interaction["status"] = "running"
        return dict(self.interaction)

    async def complete_runtime_interaction(self, **kwargs):
        self.interaction.update(
            status="completed",
            provider_response_id=kwargs["provider_response_id"],
            usage=kwargs["usage"],
        )
        return dict(self.interaction)

    async def fail_runtime_interaction(self, **kwargs):
        self.interaction["status"] = "failed"
        self.failures.append(kwargs["error_code"])

    async def record_runtime_provider_receipt(self, **kwargs):
        self.interaction.update(
            provider_response_id=kwargs["provider_response_id"],
            usage=kwargs["usage"],
        )

    async def claim_runtime_ingestion(self, **kwargs):
        key = (
            kwargs["binding_id"],
            kwargs["canonical_event_id"],
            kwargs["source_identity"],
            kwargs["event_revision"],
        )
        receipt = self.receipts.setdefault(
            key,
            {
                "id": f"receipt-{len(self.receipts) + 1}",
                "status": "claimed",
                "inserted": True,
            },
        )
        return dict(receipt)

    async def complete_runtime_ingestion(self, **kwargs):
        for receipt in self.receipts.values():
            if receipt["id"] == kwargs["receipt_id"]:
                receipt.update(status=kwargs["status"], provider_ref=kwargs.get("provider_ref"))
                return dict(receipt)
        raise AssertionError("unknown receipt")


class FakePolicy:
    def __init__(self, *, admission=True, reservation=True):
        self.admission = admission
        self.reservation = reservation
        self.accepted = []
        self.reserved = []
        self.settled = []
        self.released = []
        self.updated = []
        self.completed = []

    async def get_entitlement(self, uid):
        return {"revision": 3}

    async def accept_session(self, **kwargs):
        self.accepted.append(kwargs)
        return SimpleNamespace(allowed=self.admission, code="ok" if self.admission else "global_kill_switch")

    async def update_session(self, **kwargs):
        self.updated.append(kwargs)
        return SimpleNamespace(allowed=True, code="ok")

    async def complete_session(self, **kwargs):
        self.completed.append(kwargs)
        return "usage-event"

    async def reserve_session_cost(self, **kwargs):
        self.reserved.append(kwargs)
        return SimpleNamespace(
            allowed=self.reservation,
            code="ok" if self.reservation else "quota_daily_cost",
        )

    async def settle_session_cost(self, **kwargs):
        self.settled.append(kwargs)
        return SimpleNamespace(allowed=True, code="ok")

    async def release_session_cost(self, **kwargs):
        self.released.append(kwargs)
        return SimpleNamespace(allowed=True, code="ok")


class FakeCloudClient:
    def __init__(self, *, usage=None, tool_calls=0):
        self.calls = []
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5}
        self.tool_calls = tool_calls

    async def create_response(self, binding, **kwargs):
        self.calls.append((binding, kwargs))
        before_provider_send = kwargs.get("before_provider_send")
        if before_provider_send is not None:
            await before_provider_send()
        return HermesCloudTurn(
            response_id="response-a",
            text="Synthetic acknowledgement.",
            usage=self.usage,
            model="model-a",
            tool_calls=self.tool_calls,
        )


def test_cloud_turn_writes_once_and_same_interaction_replays():
    repository = FakeRepository()
    event_store = InMemoryCanonicalEventStore()
    policy = FakePolicy()
    cloud = FakeCloudClient()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=event_store,
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 7,
        max_cost_estimator=lambda **kwargs: 20,
    )

    first = asyncio.run(service.run_turn(_runtime(), _request()))
    replay = asyncio.run(service.run_turn(_runtime(), _request()))

    assert first.duplicate is False
    assert replay.duplicate is True
    assert replay.text == first.text
    assert len(cloud.calls) == 1
    assert len(repository.receipts) == 2
    assert all(receipt["status"] == "written" for receipt in repository.receipts.values())
    assert policy.accepted[0]["provider"] == "hermes_cloud"
    assert policy.accepted[0]["model"] == "model-a"
    assert policy.updated[0]["estimated_cost_microusd"] == 7
    assert policy.reserved[0]["reservation_microusd"] == 20
    assert policy.settled[0]["actual_cost_microusd"] == 7
    assert policy.completed[0]["termination_reason"] == "completed"


def test_photon_turn_uses_photon_entitlement_mode():
    repository = FakeRepository()
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=FakeCloudClient(),
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    request = _request()
    request = HermesCloudTurnRequest(**{**request.__dict__, "channel": "photon"})

    asyncio.run(service.run_turn(_runtime(), request))

    assert policy.accepted[0]["mode"] == "hermes-cloud-photon"


def test_enrichment_turn_uses_distinct_mode_and_disables_user_scan():
    repository = FakeRepository()
    event_store = InMemoryCanonicalEventStore()
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=event_store,
        cloud_client=FakeCloudClient(),
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    request = HermesCloudTurnRequest(
        **{
            **_request().__dict__,
            "channel": "omi_enrichment",
            "user_scan_policy": "none",
        }
    )

    result = asyncio.run(service.run_turn(_runtime(), request))
    source_identity = "hermes_cloud:omi_enrichment:interaction:" + result.canonical_user_event_id.split(":")[1]
    user_event = asyncio.run(
        event_store.get_event(
            uid=request.uid,
            event_id=result.canonical_user_event_id,
            source_identity=source_identity,
        )
    )

    assert policy.accepted[0]["mode"] == "hermes-cloud-enrichment"
    assert user_event["scan_policy"] == "none"


def test_turn_rejects_invalid_user_scan_policy_before_ingest():
    service = HermesCloudRuntimeService(
        repository=FakeRepository(),
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=FakeCloudClient(),
        voice_policy=FakePolicy(),
    )
    request = HermesCloudTurnRequest(**{**_request().__dict__, "user_scan_policy": "guardian"})

    with pytest.raises(ProvisioningError, match="hermes_cloud_scan_policy_invalid"):
        asyncio.run(service.run_turn(_runtime(), request))


def test_idempotency_hash_binds_instructions():
    repository = FakeRepository()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=FakeCloudClient(),
        voice_policy=FakePolicy(),
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    asyncio.run(service.run_turn(_runtime(), _request()))
    changed = HermesCloudTurnRequest(**{**_request().__dict__, "instructions": "Different policy."})

    with pytest.raises(ProvisioningError, match="runtime_interaction_payload_conflict"):
        asyncio.run(service.run_turn(_runtime(), changed))


def test_provider_boundary_callback_runs_after_final_checks_and_before_cloud(
    monkeypatch,
):
    events = []
    policy = FakePolicy()

    def consent_gate(_runtime):
        events.append("consent")
        return "aicr_" + ("a" * 32)

    class OrderedCloud(FakeCloudClient):
        async def create_response(self, binding, **kwargs):
            result = await super().create_response(binding, **kwargs)
            events.append("provider")
            return result

    async def mark_provider_started():
        assert policy.reserved
        assert events == ["consent", "consent"]
        events.append("provider_started")

    monkeypatch.setattr(
        hermes_cloud_runtime,
        "assert_runtime_managed_consent",
        consent_gate,
    )
    service = HermesCloudRuntimeService(
        repository=FakeRepository(),
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=OrderedCloud(),
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )

    asyncio.run(
        service.run_turn(
            _runtime(),
            _request(),
            before_provider_call=mark_provider_started,
        )
    )

    assert events == ["consent", "consent", "provider_started", "provider"]


def test_consent_revoked_after_reservation_blocks_provider_call(monkeypatch):
    repository = FakeRepository()
    policy = FakePolicy()
    cloud = FakeCloudClient()
    checks = 0

    def consent_gate(_runtime):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ProvisioningError("managed_cloud_consent_required", retryable=False)

    monkeypatch.setattr(hermes_cloud_runtime, "assert_runtime_managed_consent", consent_gate)
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )

    provider_marked = False

    async def mark_provider_started():
        nonlocal provider_marked
        provider_marked = True

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            service.run_turn(
                _runtime(),
                _request(),
                before_provider_call=mark_provider_started,
            )
        )

    assert error.value.code == "managed_cloud_consent_required"
    assert checks == 2
    assert cloud.calls == []
    assert provider_marked is False
    assert policy.reserved
    assert policy.released


def test_consent_regrant_epoch_change_after_reservation_blocks_provider_call(
    monkeypatch,
):
    repository = FakeRepository()
    policy = FakePolicy()
    cloud = FakeCloudClient()
    epochs = iter(("aicr_" + ("a" * 32), "aicr_" + ("c" * 32)))

    monkeypatch.setattr(
        hermes_cloud_runtime,
        "assert_runtime_managed_consent",
        lambda _runtime: next(epochs),
    )
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    request = HermesCloudTurnRequest(
        **{
            **_request().__dict__,
            "consent_grant_epoch": "aicr_" + ("a" * 32),
        }
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), request))

    assert error.value.code == "managed_cloud_consent_grant_changed"
    assert cloud.calls == []
    assert policy.reserved
    assert policy.released


def test_same_interaction_id_with_changed_payload_fails_closed():
    repository = FakeRepository()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=FakeCloudClient(),
        voice_policy=FakePolicy(),
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    asyncio.run(service.run_turn(_runtime(), _request()))

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request("Different payload")))

    assert error.value.code == "runtime_interaction_payload_conflict"


def test_retry_recovers_canonical_assistant_without_second_provider_call():
    repository = FakeRepository()
    event_store = InMemoryCanonicalEventStore()
    cloud = FakeCloudClient()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=event_store,
        cloud_client=cloud,
        voice_policy=FakePolicy(),
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )
    first = asyncio.run(service.run_turn(_runtime(), _request()))
    repository.interaction["status"] = "failed"

    recovered = asyncio.run(service.run_turn(_runtime(), _request()))

    assert first.duplicate is False
    assert recovered.duplicate is True
    assert recovered.response_id == "response-a"
    assert len(cloud.calls) == 1


def test_kill_switch_denial_never_calls_cloud_and_records_terminal_failure():
    repository = FakeRepository()
    cloud = FakeCloudClient()
    policy = FakePolicy(admission=False)
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request()))

    assert error.value.code == "global_kill_switch"
    assert cloud.calls == []
    assert repository.failures == ["global_kill_switch"]
    assert policy.completed == []


def test_cost_reservation_denial_never_calls_provider_and_releases_no_reservation():
    repository = FakeRepository()
    cloud = FakeCloudClient()
    policy = FakePolicy(reservation=False)
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 25,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request()))

    assert error.value.code == "quota_daily_cost"
    assert cloud.calls == []
    assert policy.reserved[0]["reservation_microusd"] == 25
    assert policy.settled == []
    assert policy.released[0]["session_id"] == "interaction-a"


def test_full_input_budget_includes_chained_context_and_is_conservative():
    bound = conservative_client_input_token_upper_bound(
        user_input="Synthetic follow-up",
        instructions="Use the selected synthetic context.",
        previous_response_id="response-previous",
        previous_response_usage={"input_tokens": 100, "output_tokens": 40},
    )

    assert bound > 140


def test_first_turn_reserves_signed_context_for_remote_prompt_and_tool_overhead(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS", "256")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_OUTPUT_TOKENS", "64")
    runtime = _runtime(
        model_context_window_tokens=1024,
        allowed_tools=("honcho_search",),
    )
    request = _request("x" * 170)
    client_bound = conservative_client_input_token_upper_bound(
        user_input=request.user_input,
        instructions=request.instructions,
        previous_response_id=None,
        previous_response_usage={},
    )
    assert 200 <= client_bound <= 256

    estimator_calls = []

    def max_cost_estimator(**kwargs):
        estimator_calls.append(kwargs)
        return 500

    cloud = FakeCloudClient(
        usage={"input_tokens": 2016, "output_tokens": 32},
        tool_calls=1,
    )
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=FakeRepository(),
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 250,
        max_cost_estimator=max_cost_estimator,
    )

    result = asyncio.run(service.run_turn(runtime, request))

    assert result.usage["input_tokens"] > client_bound
    assert estimator_calls == [{"max_input_tokens": 2048, "max_output_tokens": 128}]
    assert policy.reserved[0]["reservation_microusd"] == 500
    assert policy.settled[0]["actual_cost_microusd"] == 250
    assert cloud.calls[0][0]["model_context_window_tokens"] == 1024


def test_chained_turn_reserves_context_ceiling_for_fresh_remote_overhead(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS", "900")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_OUTPUT_TOKENS", "64")
    previous_usage = {"input_tokens": 600, "output_tokens": 100}
    request = _request("x" * 80)
    client_bound = conservative_client_input_token_upper_bound(
        user_input=request.user_input,
        instructions=request.instructions,
        previous_response_id="response-previous",
        previous_response_usage=previous_usage,
    )
    assert 700 < client_bound <= 900

    estimator_calls = []

    def max_cost_estimator(**kwargs):
        estimator_calls.append(kwargs)
        return 500

    cloud = FakeCloudClient(usage={"input_tokens": 950, "output_tokens": 32})
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=FakeRepository(
            previous_response_id="response-previous",
            previous_response_usage=previous_usage,
        ),
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 250,
        max_cost_estimator=max_cost_estimator,
    )

    result = asyncio.run(
        service.run_turn(
            _runtime(model_context_window_tokens=1024),
            request,
        )
    )

    assert result.usage["input_tokens"] > client_bound
    assert estimator_calls == [{"max_input_tokens": 1024, "max_output_tokens": 64}]
    assert policy.reserved[0]["reservation_microusd"] == 500


def test_chained_context_over_input_budget_never_reaches_provider(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS", "256")
    repository = FakeRepository(
        previous_response_id="response-previous",
        previous_response_usage={"input_tokens": 180, "output_tokens": 60},
    )
    cloud = FakeCloudClient()
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 25,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request("short follow-up")))

    assert error.value.code == "hermes_cloud_input_budget_exceeded"
    assert cloud.calls == []
    assert policy.reserved == []


def test_over_input_budget_never_reserves_cost_or_calls_provider(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS", "256")
    repository = FakeRepository()
    cloud = FakeCloudClient()
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 25,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request("x" * 300)))

    assert error.value.code == "hermes_cloud_input_budget_exceeded"
    assert cloud.calls == []
    assert policy.reserved == []
    assert policy.settled == []
    assert policy.released == []
    assert policy.completed[0]["normalized_error_code"] == "hermes_cloud_input_budget_exceeded"


def test_chained_context_without_usage_fails_before_reservation_or_provider(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_MAX_INPUT_TOKENS", "8192")
    repository = FakeRepository(previous_response_id="response-previous")
    cloud = FakeCloudClient()
    policy = FakePolicy()
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=cloud,
        voice_policy=policy,
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 25,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(_runtime(), _request()))

    assert error.value.code == "hermes_cloud_previous_usage_missing"
    assert cloud.calls == []
    assert policy.reserved == []


def test_cancellation_closes_policy_lease():
    class BlockingCloud(FakeCloudClient):
        async def create_response(self, binding, **kwargs):
            self.calls.append((binding, kwargs))
            await kwargs["before_provider_send"]()
            await asyncio.Event().wait()

    async def scenario():
        repository = FakeRepository()
        policy = FakePolicy()
        service = HermesCloudRuntimeService(
            repository=repository,
            event_store=InMemoryCanonicalEventStore(),
            cloud_client=BlockingCloud(),
            voice_policy=policy,
            cost_estimator=lambda usage: 0,
            max_cost_estimator=lambda **kwargs: 1,
        )
        task = asyncio.create_task(service.run_turn(_runtime(), _request()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repository.failures == ["client_cancelled"]
        assert policy.completed[0]["termination_reason"] == "client_disconnect"
        assert policy.completed[0]["normalized_error_code"] == "client_cancelled"
        assert policy.settled[0]["actual_cost_microusd"] == 1

    asyncio.run(scenario())


def test_shadow_binding_is_rejected_by_ordinary_runtime():
    runtime = _runtime()
    runtime = IsolatedRuntime(**{**runtime.__dict__, "status": "shadow"})
    service = HermesCloudRuntimeService(
        repository=FakeRepository(),
        event_store=InMemoryCanonicalEventStore(),
        cloud_client=FakeCloudClient(),
        voice_policy=FakePolicy(),
        cost_estimator=lambda usage: 0,
        max_cost_estimator=lambda **kwargs: 1,
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(service.run_turn(runtime, _request()))

    assert error.value.code == "hermes_cloud_shadow_not_routable"
