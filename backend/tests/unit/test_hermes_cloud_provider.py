import asyncio
import pytest

from ella.services import hermes_cloud
from ella.services import runtime_resolver
from ella.services.hermes_cloud import (
    HermesCloudClient,
    HonchoCloudProvisionClient,
    HermesCloudPoolManager,
    estimate_turn_cost_microusd,
)
from ella.services.hermes_cloud_policy import ApprovedRuntimeManifest
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_from_binding


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses[url]

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        response = self.responses[url]
        return response(kwargs) if callable(response) else response


def _binding():
    artifact_hash = "a" * 64
    return {
        "id": "binding-a",
        "api_base_url_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        "api_key_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        "honcho_api_key_ref": "env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
        "expected_model": "model-a",
        "allowed_tools": [],
        "required_capabilities": ["responses_api", "session_key_header"],
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "models-v1",
        "prompt_artifact_receipt": {
            "schema_version": "ella-hermes-cloud-approval-v1",
            "prompt_pack_version": "prompt-v1",
            "model_policy_version": "models-v1",
            "expected_model": "model-a",
            "model_context_window_tokens": 16384,
            "policy_commit_sha": "b" * 40,
            "lane_s_review_url": "https://github.com/ellaaicare/ella-ai/issues/1124",
            "approval_manifest_sha256": "c" * 64,
            "soul_sha256": artifact_hash,
            "observed_soul_sha256": artifact_hash,
            "agents_sha256": artifact_hash,
            "observed_agents_sha256": artifact_hash,
            "model_policy_sha256": artifact_hash,
            "observed_model_policy_sha256": artifact_hash,
        },
    }


def _preflight_responses(toolsets=None):
    base = "https://cloud.example.test"
    return {
        f"{base}/health/detailed": Response(200, {"status": "ok", "readiness": {"status": "ok"}}),
        f"{base}/v1/capabilities": Response(
            200,
            {
                "session_key_header": "X-Hermes-Session-Key",
                "features": {"responses_api": True},
            },
        ),
        f"{base}/v1/models": Response(200, {"data": [{"id": "model-a"}]}),
        f"{base}/v1/toolsets": Response(200, toolsets if toolsets is not None else []),
    }


@pytest.fixture
def cloud_env(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_API_URL_SYNTHETIC", "https://cloud.example.test")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC", "cloud-secret")
    monkeypatch.setenv("ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC", "honcho-secret")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")


def test_preflight_requires_exact_model_capabilities_and_tools(cloud_env):
    fake = FakeClient(_preflight_responses())
    receipt = asyncio.run(HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding()))

    assert receipt.model == "model-a"
    assert receipt.tools == ()
    assert receipt.capabilities == ("responses_api", "session_key_header")
    assert receipt.receipt["content_free"] is True
    assert all(call[2]["headers"]["Authorization"] == "Bearer cloud-secret" for call in fake.calls)


def test_preflight_rejects_unexpected_enabled_tool(cloud_env):
    fake = FakeClient(_preflight_responses([{"enabled": True, "tools": ["shell"]}]))
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding()))
    assert error.value.code == "hermes_cloud_tool_drift"


def test_preflight_rejects_builtin_memory_for_empty_canary_tool_surface(
    cloud_env,
):
    fake = FakeClient(
        _preflight_responses(
            [{"enabled": True, "tools": ["memory"]}],
        )
    )

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding()))

    assert error.value.code == "hermes_cloud_tool_drift"


def test_preflight_rejects_missing_or_mismatched_prompt_artifacts(cloud_env):
    missing = _binding()
    missing.pop("prompt_artifact_receipt")
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())).preflight(
                missing
            )
        )
    assert error.value.code == "prompt_artifact_receipt_missing"

    mismatch = _binding()
    mismatch["prompt_artifact_receipt"]["observed_soul_sha256"] = "b" * 64
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())).preflight(
                mismatch
            )
        )
    assert error.value.code == "prompt_artifact_checksum_mismatch"

    missing_context = _binding()
    missing_context["prompt_artifact_receipt"].pop("model_context_window_tokens")
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())).preflight(
                missing_context
            )
        )
    assert error.value.code == "prompt_artifact_model_context_invalid"

    mismatched_context = _binding()
    mismatched_context["model_context_window_tokens"] = 8192
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())).preflight(
                mismatched_context
            )
        )
    assert error.value.code == "prompt_artifact_model_context_mismatch"


def test_preflight_wakes_sleeping_instance_within_bounded_poll(cloud_env, monkeypatch):
    responses = _preflight_responses()
    health_url = "https://cloud.example.test/health/detailed"
    attempts = [Response(503, {}), responses[health_url]]

    class WakingClient(FakeClient):
        async def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if url == health_url:
                return attempts.pop(0)
            return self.responses[url]

    async def no_sleep(_seconds):
        return None

    fake = WakingClient(responses)
    monkeypatch.setattr(hermes_cloud.asyncio, "sleep", no_sleep)

    receipt = asyncio.run(HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding()))

    assert receipt.model == "model-a"
    assert sum(1 for call in fake.calls if call[1] == health_url) == 2


def test_responses_api_uses_distinct_session_headers_and_idempotency(cloud_env):
    url = "https://cloud.example.test/v1/responses"
    fake = FakeClient(
        {
            url: Response(
                200,
                {
                    "id": "response-a",
                    "status": "completed",
                    "model": "model-a",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Hello"}],
                        }
                    ],
                },
            )
        }
    )
    turn = asyncio.run(
        HermesCloudClient(http_client_factory=lambda **kwargs: fake).create_response(
            _binding(),
            session_key="stable-memory-key",
            hermes_session_id="single-interaction-id",
            idempotency_key="request-id",
            user_input="Synthetic hello",
            instructions="Synthetic only",
            previous_response_id="response-previous",
            max_output_tokens=128,
            max_tool_calls=0,
        )
    )

    assert turn.text == "Hello"
    headers = fake.calls[0][2]["headers"]
    assert headers["X-Hermes-Session-Key"] == "stable-memory-key"
    assert headers["X-Hermes-Session-Id"] == "single-interaction-id"
    assert headers["Idempotency-Key"] == "request-id"
    assert fake.calls[0][2]["json"]["previous_response_id"] == "response-previous"
    assert fake.calls[0][2]["json"]["max_output_tokens"] == 128
    assert fake.calls[0][2]["json"]["max_tool_calls"] == 0


def test_responses_api_validation_failure_does_not_cross_provider_boundary(cloud_env):
    boundary = []

    async def mark_boundary():
        boundary.append("provider_send")

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **_kwargs: pytest.fail("client must not be created for invalid local budget")
            ).create_response(
                _binding(),
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=0,
                max_tool_calls=0,
                before_provider_send=mark_boundary,
            )
        )

    assert error.value.code == "hermes_cloud_turn_budget_invalid"
    assert boundary == []


def test_responses_api_client_entry_failure_does_not_cross_provider_boundary(cloud_env):
    boundary = []

    class EntryFailureClient:
        async def __aenter__(self):
            raise hermes_cloud.httpx.ConnectError("synthetic client entry failure")

        async def __aexit__(self, *_args):
            return None

    async def mark_boundary():
        boundary.append("provider_send")

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **_kwargs: EntryFailureClient()).create_response(
                _binding(),
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=128,
                max_tool_calls=0,
                before_provider_send=mark_boundary,
            )
        )

    assert error.value.code == "hermes_cloud_unavailable"
    assert boundary == []


def test_responses_api_post_failure_crosses_provider_boundary_first(cloud_env):
    boundary = []

    class PostFailureClient(FakeClient):
        async def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            raise hermes_cloud.httpx.ConnectError("synthetic post failure")

    fake = PostFailureClient({})

    async def mark_boundary():
        boundary.append("provider_send")

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **_kwargs: fake).create_response(
                _binding(),
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=128,
                max_tool_calls=0,
                before_provider_send=mark_boundary,
            )
        )

    assert error.value.code == "hermes_cloud_unavailable"
    assert boundary == ["provider_send"]
    assert [call[0] for call in fake.calls] == ["POST"]


def test_honcho_claim_creates_opaque_workspace_and_peers_without_uid(cloud_env):
    responses = {}
    effects = []

    def create_response(kwargs):
        return Response(201, {"id": kwargs["json"]["id"]})

    fake = FakeClient(responses)
    workspace, observed, observer = HonchoCloudProvisionClient._resource_ids("binding-a")
    responses.update(
        {
            "https://honcho.example.test/v3/workspaces": create_response,
            f"https://honcho.example.test/v3/workspaces/{workspace}/peers": create_response,
        }
    )
    result = asyncio.run(
        HonchoCloudProvisionClient(
            base_url="https://honcho.example.test",
            http_client_factory=lambda **kwargs: fake,
        ).ensure_profile(
            _binding(),
            on_side_effect=lambda effect: asyncio.sleep(0, result=effects.append(effect)),
        )
    )

    assert result == {
        "workspace": workspace,
        "observed_peer": observed,
        "observer_peer": observer,
    }
    payloads = [call[2]["json"] for call in fake.calls]
    assert "synthetic" not in str(payloads).lower()
    assert all(call[2]["headers"]["Authorization"] == "Bearer honcho-secret" for call in fake.calls)
    assert [effect["state"] for effect in effects] == [
        "planned",
        "confirmed",
        "planned",
        "confirmed",
        "planned",
        "confirmed",
    ]


def test_cost_estimator_requires_normalized_rates(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_CLOUD_INPUT_MICROUSD_PER_MILLION_TOKENS", raising=False)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_OUTPUT_MICROUSD_PER_MILLION_TOKENS", raising=False)
    with pytest.raises(ProvisioningError) as error:
        estimate_turn_cost_microusd({"input_tokens": 1, "output_tokens": 1})
    assert error.value.code == "hermes_cloud_billing_rate_missing"

    monkeypatch.setenv("ELLA_HERMES_CLOUD_INPUT_MICROUSD_PER_MILLION_TOKENS", "1000000")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_OUTPUT_MICROUSD_PER_MILLION_TOKENS", "2000000")
    assert estimate_turn_cost_microusd({"input_tokens": 3, "output_tokens": 2}) == 7


def test_pool_registration_persists_server_approved_prompt_receipt():
    class Repository:
        def __init__(self):
            self.kwargs = None

        async def register_cloud_pool_binding(self, **kwargs):
            self.kwargs = kwargs
            return {
                "id": "binding-a",
                "runtime_instance_id": "instance-a",
                "status": "pool_available",
                "health_state": "healthy",
            }

    class Cloud:
        async def preflight(self, candidate):
            return hermes_cloud.HermesCloudPreflight(
                model="model-a",
                tools=(),
                capabilities=("responses_api", "session_key_header"),
                receipt={
                    "prompt_artifacts": candidate["prompt_artifact_receipt"],
                    "content_free": True,
                },
            )

    artifact_hash = "a" * 64
    manifest = ApprovedRuntimeManifest(
        policy_commit_sha="b" * 40,
        lane_s_review_url="https://github.com/ellaaicare/ella-ai/pull/1127",
        prompt_pack_version="prompt-v1",
        model_policy_version="models-v1",
        expected_model="model-a",
        model_context_window_tokens=16384,
        allowed_tools=(),
        required_capabilities=("responses_api", "session_key_header"),
        artifact_sha256={
            "soul": artifact_hash,
            "agents": artifact_hash,
            "model_policy": artifact_hash,
        },
        manifest_sha256="c" * 64,
    )

    class ManifestStore:
        def load(self):
            return manifest

    repository = Repository()
    candidate = {
        "api_base_url_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        "api_key_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        "honcho_api_key_ref": "env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
        "observed_prompt_artifacts": {
            "soul_sha256": artifact_hash,
            "agents_sha256": artifact_hash,
            "model_policy_sha256": artifact_hash,
        },
        "runtime_instance_id": "instance-a",
        "profile_name": "pool-instance-a",
        "agent_id": "hermes-cloud",
        "template_version": "hermes-cloud-user-v1",
        "voice_policy_version": "voice-v1",
    }
    result = asyncio.run(
        HermesCloudPoolManager(
            repository=repository,
            cloud_client=Cloud(),
            manifest_store=ManifestStore(),
        ).register(candidate)
    )

    assert result["status"] == "pool_available"
    assert repository.kwargs["prompt_artifact_receipt"]["policy_commit_sha"] == "b" * 40
    assert repository.kwargs["prompt_artifact_receipt"]["approval_manifest_sha256"] == "c" * 64
    assert repository.kwargs["prompt_artifact_receipt"]["model_context_window_tokens"] == 16384
    assert "expected_model" not in candidate
    assert "model_context_window_tokens" not in candidate


def test_pool_registration_rejects_missing_server_approval_manifest():
    class Repository:
        async def register_cloud_pool_binding(self, **kwargs):
            raise AssertionError("unapproved candidate must not be registered")

    class MissingManifest:
        def load(self):
            raise ProvisioningError("hermes_cloud_approval_manifest_missing", retryable=False)

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudPoolManager(
                repository=Repository(),
                manifest_store=MissingManifest(),
            ).register({"observed_prompt_artifacts": {}})
        )
    assert error.value.code == "hermes_cloud_approval_manifest_missing"


def test_pool_registration_rejects_candidate_policy_self_attestation():
    class Repository:
        async def register_cloud_pool_binding(self, **kwargs):
            raise AssertionError("candidate policy must not be registered")

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudPoolManager(repository=Repository()).register(
                {
                    "expected_model": "candidate-controlled",
                    "model_context_window_tokens": 1,
                    "observed_prompt_artifacts": {},
                }
            )
        )
    assert error.value.code == "hermes_cloud_candidate_policy_forbidden"


def test_cloud_runtime_resolver_is_fail_closed_and_contains_no_local_route(cloud_env):
    binding = {
        **_binding(),
        "id": "binding-a",
        "omi_uid": "synthetic-user",
        "provider": "hermes_cloud",
        "status": "internal_canary",
        "profile_class": "synthetic",
        "active": True,
        "health_state": "healthy",
        "profile_name": "synthetic-profile",
        "agent_id": "hermes-cloud",
        "runtime_instance_id": "instance-a",
        "honcho_workspace": "workspace-a",
        "observed_peer": "user-a",
        "observer_peer": "companion-a",
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "models-v1",
        "voice_policy_version": "voice-v1",
        "revision": 2,
        "workspace_root": None,
        "internal_gateway_url": None,
        "gateway_port": None,
        "service_label": None,
        "credential_ref": None,
    }

    runtime = runtime_from_binding(binding, "synthetic-user")

    assert runtime.provider == "hermes_cloud"
    assert runtime.gateway_url == "https://cloud.example.test"
    assert runtime.workspace_root == ""
    assert runtime.model_context_window_tokens == 16384

    binding["workspace_root"] = "/Users/ellaai/.hermes/profiles/legacy"
    with pytest.raises(ProvisioningError) as error:
        runtime_from_binding(binding, "synthetic-user")
    assert error.value.code == "cloud_binding_contains_local_runtime"

    binding["workspace_root"] = None
    binding["profile_class"] = "real"
    with pytest.raises(ProvisioningError) as profile:
        runtime_from_binding(binding, "synthetic-user")
    assert profile.value.code == "hermes_cloud_synthetic_profile_required"


def test_cloud_runtime_resolver_checks_consent_before_loading_credentials(cloud_env, monkeypatch):
    binding = {
        **_binding(),
        "id": "binding-a",
        "omi_uid": "user-a",
        "provider": "hermes_cloud",
        "status": "active",
        "active": True,
        "health_state": "healthy",
        "profile_name": "cloud-user-a",
        "agent_id": "hermes-cloud",
        "runtime_instance_id": "instance-a",
        "honcho_workspace": "workspace-a",
        "observed_peer": "user-a",
        "observer_peer": "companion-a",
        "revision": 2,
        "workspace_root": None,
        "internal_gateway_url": None,
        "gateway_port": None,
        "service_label": None,
        "credential_ref": None,
    }

    def denied(*_args, **_kwargs):
        raise ProvisioningError("managed_cloud_consent_required", retryable=False)

    def credentials_forbidden(_binding):
        raise AssertionError("credentials must not load before consent")

    monkeypatch.setattr(runtime_resolver, "assert_cloud_identity_gate", denied)
    monkeypatch.setattr(runtime_resolver.HermesCloudClient, "credentials", credentials_forbidden)

    with pytest.raises(ProvisioningError) as error:
        runtime_from_binding(binding, "user-a")

    assert error.value.code == "managed_cloud_consent_required"


@pytest.mark.parametrize(
    ("body_update", "expected_code"),
    [
        ({"model": "other-model"}, "hermes_cloud_returned_model_mismatch"),
        ({"usage": {"input_tokens": 3}}, "invalid_hermes_cloud_usage"),
    ],
)
def test_responses_api_fails_closed_on_model_or_usage_drift(
    cloud_env,
    body_update,
    expected_code,
):
    url = "https://cloud.example.test/v1/responses"
    body = {
        "id": "response-a",
        "status": "completed",
        "model": "model-a",
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            }
        ],
    }
    body.update(body_update)
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient({url: Response(200, body)})
            ).create_response(
                _binding(),
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=128,
                max_tool_calls=0,
            )
        )
    assert error.value.code == expected_code


def test_responses_api_rejects_usage_beyond_signed_model_context(cloud_env):
    url = "https://cloud.example.test/v1/responses"
    body = {
        "id": "response-a",
        "status": "completed",
        "model": "model-a",
        "usage": {"input_tokens": 120, "output_tokens": 9},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            }
        ],
    }
    binding = _binding()
    binding["prompt_artifact_receipt"]["model_context_window_tokens"] = 128

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient({url: Response(200, body)})
            ).create_response(
                binding,
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=16,
                max_tool_calls=0,
            )
        )

    assert error.value.code == "hermes_cloud_provider_context_exceeded"


def test_responses_api_allows_signed_context_for_each_tool_enabled_round(cloud_env):
    url = "https://cloud.example.test/v1/responses"
    body = {
        "id": "response-a",
        "status": "completed",
        "model": "model-a",
        "usage": {"input_tokens": 230, "output_tokens": 26},
        "output": [
            {"type": "function_call", "name": "honcho_recall"},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
        ],
    }
    binding = _binding()
    binding["allowed_tools"] = ["honcho_recall"]
    binding["prompt_artifact_receipt"]["model_context_window_tokens"] = 128

    turn = asyncio.run(
        HermesCloudClient(http_client_factory=lambda **kwargs: FakeClient({url: Response(200, body)})).create_response(
            binding,
            session_key="scope",
            hermes_session_id="interaction",
            idempotency_key="request",
            user_input="Synthetic",
            instructions="Synthetic",
            max_output_tokens=16,
            max_tool_calls=1,
        )
    )

    assert turn.usage == {"input_tokens": 230, "output_tokens": 26}
    assert turn.tool_calls == 1


def test_responses_api_enforces_tool_allowlist_and_hard_count(cloud_env):
    url = "https://cloud.example.test/v1/responses"
    body = {
        "id": "response-a",
        "status": "completed",
        "model": "model-a",
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "output": [
            {"type": "function_call", "name": "honcho_recall"},
            {"type": "function_call", "name": "honcho_recall"},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
        ],
    }
    binding = _binding()
    binding["allowed_tools"] = ["honcho_recall"]

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient({url: Response(200, body)})
            ).create_response(
                binding,
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=128,
                max_tool_calls=1,
            )
        )
    assert error.value.code == "hermes_cloud_tool_budget_exceeded"


def test_responses_api_rejects_memory_call_when_canary_allows_no_tools(
    cloud_env,
):
    url = "https://cloud.example.test/v1/responses"
    body = {
        "id": "response-a",
        "status": "completed",
        "model": "model-a",
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "output": [
            {"type": "function_call", "name": "memory"},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
        ],
    }

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient({url: Response(200, body)})
            ).create_response(
                _binding(),
                session_key="scope",
                hermes_session_id="interaction",
                idempotency_key="request",
                user_input="Synthetic",
                instructions="Synthetic",
                max_output_tokens=128,
                max_tool_calls=0,
            )
        )

    assert error.value.code == "hermes_cloud_unapproved_tool_call"


def test_cloud_runtime_resolver_does_not_fall_back_when_lookup_fails(monkeypatch):
    class Repository:
        async def resolve_active_runtime(self, uid):
            assert uid == "synthetic-user"
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(runtime_resolver, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(runtime_resolver, "cloud_provisioning_enabled", lambda uid=None: True)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(resolve_isolated_runtime("synthetic-user", Repository()))


def test_cloud_runtime_resolver_requires_binding_for_enabled_user(monkeypatch):
    class Repository:
        async def resolve_active_runtime(self, uid):
            return None

        async def resolve_cloud_binding_state(self, uid):
            return None

    monkeypatch.setattr(runtime_resolver, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(runtime_resolver, "cloud_provisioning_enabled", lambda uid=None: True)

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(resolve_isolated_runtime("synthetic-user", Repository()))
    assert error.value.code == "hermes_cloud_not_provisioned"
    assert error.value.retryable is True
