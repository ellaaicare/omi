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
            "prompt_pack_version": "prompt-v1",
            "model_policy_version": "models-v1",
            "review_receipt": "https://github.com/ellaaicare/ella-ai/issues/1124",
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


def test_preflight_requires_exact_model_capabilities_and_tools(cloud_env):
    fake = FakeClient(_preflight_responses())
    receipt = asyncio.run(
        HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding())
    )

    assert receipt.model == "model-a"
    assert receipt.tools == ()
    assert receipt.capabilities == ("responses_api", "session_key_header")
    assert receipt.receipt["content_free"] is True
    assert all(call[2]["headers"]["Authorization"] == "Bearer cloud-secret" for call in fake.calls)


def test_preflight_rejects_unexpected_enabled_tool(cloud_env):
    fake = FakeClient(
        _preflight_responses([{"enabled": True, "tools": ["shell"]}])
    )
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding())
        )
    assert error.value.code == "hermes_cloud_tool_drift"


def test_preflight_rejects_missing_or_mismatched_prompt_artifacts(cloud_env):
    missing = _binding()
    missing.pop("prompt_artifact_receipt")
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())
            ).preflight(missing)
        )
    assert error.value.code == "prompt_artifact_receipt_missing"

    mismatch = _binding()
    mismatch["prompt_artifact_receipt"]["observed_soul_sha256"] = "b" * 64
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(
            HermesCloudClient(
                http_client_factory=lambda **kwargs: FakeClient(_preflight_responses())
            ).preflight(mismatch)
        )
    assert error.value.code == "prompt_artifact_checksum_mismatch"


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

    receipt = asyncio.run(
        HermesCloudClient(http_client_factory=lambda **kwargs: fake).preflight(_binding())
    )

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
        )
    )

    assert turn.text == "Hello"
    headers = fake.calls[0][2]["headers"]
    assert headers["X-Hermes-Session-Key"] == "stable-memory-key"
    assert headers["X-Hermes-Session-Id"] == "single-interaction-id"
    assert headers["Idempotency-Key"] == "request-id"
    assert fake.calls[0][2]["json"]["previous_response_id"] == "response-previous"


def test_honcho_claim_creates_opaque_workspace_and_peers_without_uid(cloud_env):
    responses = {}

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
        ).ensure_profile(_binding())
    )

    assert result == {
        "workspace": workspace,
        "observed_peer": observed,
        "observer_peer": observer,
    }
    payloads = [call[2]["json"] for call in fake.calls]
    assert "synthetic" not in str(payloads).lower()
    assert all(call[2]["headers"]["Authorization"] == "Bearer honcho-secret" for call in fake.calls)


def test_cost_estimator_requires_normalized_rates(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_CLOUD_INPUT_MICROUSD_PER_MILLION_TOKENS", raising=False)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_OUTPUT_MICROUSD_PER_MILLION_TOKENS", raising=False)
    with pytest.raises(ProvisioningError) as error:
        estimate_turn_cost_microusd({"input_tokens": 1, "output_tokens": 1})
    assert error.value.code == "hermes_cloud_billing_rate_missing"

    monkeypatch.setenv("ELLA_HERMES_CLOUD_INPUT_MICROUSD_PER_MILLION_TOKENS", "1000000")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_OUTPUT_MICROUSD_PER_MILLION_TOKENS", "2000000")
    assert estimate_turn_cost_microusd({"input_tokens": 3, "output_tokens": 2}) == 7


def test_pool_registration_persists_reviewed_prompt_receipt():
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

    repository = Repository()
    candidate = {
        **_binding(),
        "runtime_instance_id": "instance-a",
        "profile_name": "pool-instance-a",
        "agent_id": "hermes-cloud",
        "template_version": "hermes-cloud-user-v1",
        "voice_policy_version": "voice-v1",
    }
    result = asyncio.run(
        HermesCloudPoolManager(repository=repository, cloud_client=Cloud()).register(candidate)
    )

    assert result["status"] == "pool_available"
    assert repository.kwargs["prompt_artifact_receipt"] == candidate["prompt_artifact_receipt"]


def test_cloud_runtime_resolver_is_fail_closed_and_contains_no_local_route(cloud_env):
    binding = {
        **_binding(),
        "id": "binding-a",
        "omi_uid": "synthetic-user",
        "provider": "hermes_cloud",
        "status": "shadow",
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

    binding["workspace_root"] = "/Users/ellaai/.hermes/profiles/legacy"
    with pytest.raises(ProvisioningError) as error:
        runtime_from_binding(binding, "synthetic-user")
    assert error.value.code == "cloud_binding_contains_local_runtime"


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
