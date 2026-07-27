"""Fail-closed Hermes Cloud and Honcho Cloud provider contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import httpx

from ella.services.provisioning import ProvisioningError

CLOUD_SECRET_REF_RE = re.compile(
    r"^env:(?:"
    r"ELLA_HERMES_CLOUD_API_URL_[A-Z0-9_]+|"
    r"ELLA_HERMES_CLOUD_API_KEY_[A-Z0-9_]+|"
    r"ELLA_HONCHO_CLOUD_API_KEY(?:_[A-Z0-9_]+)?|"
    r"ELLA_RUNTIME_POOL_ALERT_TOKEN"
    r")$"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
HERMES_CLOUD_TIMEOUT_SECONDS = float(os.getenv("ELLA_HERMES_CLOUD_TIMEOUT_SECONDS", "60"))
HERMES_CLOUD_PREFLIGHT_TIMEOUT_SECONDS = float(
    os.getenv("ELLA_HERMES_CLOUD_PREFLIGHT_TIMEOUT_SECONDS", "15")
)
HONCHO_CLOUD_BASE_URL = os.getenv("ELLA_HONCHO_CLOUD_BASE_URL", "https://api.honcho.dev").rstrip("/")
POOL_ALERT_TIMEOUT_SECONDS = float(os.getenv("ELLA_RUNTIME_POOL_ALERT_TIMEOUT_SECONDS", "5"))


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def validate_cloud_secret_ref(reference: Optional[str]) -> str:
    reference = str(reference or "").strip()
    if not CLOUD_SECRET_REF_RE.fullmatch(reference):
        raise ProvisioningError("invalid_cloud_secret_reference", retryable=False)
    return reference[4:]


def resolve_cloud_secret(reference: Optional[str]) -> str:
    variable = validate_cloud_secret_ref(reference)
    value = os.getenv(variable, "").strip()
    if not value:
        raise ProvisioningError("cloud_secret_unavailable", retryable=True)
    return value


def validate_cloud_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProvisioningError("invalid_hermes_cloud_url", retryable=False)
    return value.rstrip("/")


def validate_honcho_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProvisioningError("invalid_honcho_cloud_url", retryable=False)
    return value.rstrip("/")


def validate_prompt_artifact_receipt(binding: dict[str, Any]) -> dict[str, Any]:
    """Require reviewed prompt artifacts to match the observed vendor export."""
    receipt = binding.get("prompt_artifact_receipt")
    if not isinstance(receipt, dict):
        raise ProvisioningError("prompt_artifact_receipt_missing", retryable=False)
    prompt_pack_version = str(binding.get("prompt_pack_version") or "").strip()
    model_policy_version = str(binding.get("model_policy_version") or "").strip()
    if (
        not prompt_pack_version
        or str(receipt.get("prompt_pack_version") or "") != prompt_pack_version
        or not model_policy_version
        or str(receipt.get("model_policy_version") or "") != model_policy_version
    ):
        raise ProvisioningError("prompt_artifact_version_mismatch", retryable=False)
    review_receipt = str(receipt.get("review_receipt") or "").strip()
    if not review_receipt.startswith("https://github.com/ellaaicare/ella-ai/"):
        raise ProvisioningError("prompt_artifact_review_missing", retryable=False)

    normalized = {
        "prompt_pack_version": prompt_pack_version,
        "model_policy_version": model_policy_version,
        "review_receipt": review_receipt,
        "content_free": True,
    }
    for artifact in ("soul", "agents", "model_policy"):
        expected = str(receipt.get(f"{artifact}_sha256") or "").lower()
        observed = str(receipt.get(f"observed_{artifact}_sha256") or "").lower()
        if not SHA256_RE.fullmatch(expected) or expected != observed:
            raise ProvisioningError("prompt_artifact_checksum_mismatch", retryable=False)
        normalized[f"{artifact}_sha256"] = expected
        normalized[f"observed_{artifact}_sha256"] = observed
    return normalized


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _response_output_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for output in body.get("output") or []:
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def _observed_tools(body: Any) -> set[str]:
    if not isinstance(body, list):
        raise ProvisioningError("invalid_hermes_toolset_receipt", retryable=True)
    tools: set[str] = set()
    for toolset in body:
        if not isinstance(toolset, dict):
            raise ProvisioningError("invalid_hermes_toolset_receipt", retryable=True)
        if toolset.get("enabled") is not True:
            continue
        concrete = toolset.get("tools")
        if not isinstance(concrete, list):
            raise ProvisioningError("invalid_hermes_toolset_receipt", retryable=True)
        tools.update(str(item) for item in concrete if str(item).strip())
    return tools


def _capability_enabled(body: dict[str, Any], name: str) -> bool:
    if name == "session_key_header":
        return body.get("session_key_header") == "X-Hermes-Session-Key"
    features = body.get("features")
    return isinstance(features, dict) and features.get(name) is True


def _health_ready(body: Any) -> bool:
    if not isinstance(body, dict) or str(body.get("status") or "").lower() not in {"ok", "healthy"}:
        return False
    readiness = body.get("readiness")
    return not isinstance(readiness, dict) or str(readiness.get("status") or "ok").lower() in {
        "ok",
        "healthy",
    }


def estimate_turn_cost_microusd(usage: dict[str, Any]) -> int:
    """Estimate provider cost from normalized token rates, failing closed."""
    try:
        input_tokens = max(0, int(usage.get("input_tokens") or 0))
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("invalid_hermes_cloud_usage", retryable=False) from exc
    if input_tokens == 0 and output_tokens == 0:
        return 0

    def rate(name: str) -> Decimal:
        raw = os.getenv(name, "").strip()
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise ProvisioningError("hermes_cloud_billing_rate_missing", retryable=False) from exc
        if not value.is_finite() or value < 0:
            raise ProvisioningError("invalid_hermes_cloud_billing_rate", retryable=False)
        return value

    input_rate = rate("ELLA_HERMES_CLOUD_INPUT_MICROUSD_PER_MILLION_TOKENS")
    output_rate = rate("ELLA_HERMES_CLOUD_OUTPUT_MICROUSD_PER_MILLION_TOKENS")
    total = (
        Decimal(input_tokens) * input_rate
        + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    return max(0, int(total.to_integral_value()))


@dataclass(frozen=True)
class HermesCloudPreflight:
    model: str
    tools: tuple[str, ...]
    capabilities: tuple[str, ...]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class HermesCloudTurn:
    response_id: str
    text: str
    usage: dict[str, Any]
    model: str


class HermesCloudClient:
    def __init__(
        self,
        *,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    ):
        self.http_client_factory = http_client_factory

    @staticmethod
    def credentials(binding: dict[str, Any]) -> tuple[str, str]:
        base_url = validate_cloud_base_url(resolve_cloud_secret(binding.get("api_base_url_ref")))
        token = resolve_cloud_secret(binding.get("api_key_ref"))
        return base_url, token

    async def preflight(self, binding: dict[str, Any]) -> HermesCloudPreflight:
        prompt_artifacts = validate_prompt_artifact_receipt(binding)
        base_url, token = self.credentials(binding)
        expected_model = str(binding.get("expected_model") or "").strip()
        allowed_tools = {
            str(value)
            for value in (binding.get("allowed_tools") or [])
            if str(value).strip()
        }
        required_capabilities = {
            str(value)
            for value in (binding.get("required_capabilities") or [])
            if str(value).strip()
        }
        if not expected_model:
            raise ProvisioningError("cloud_model_policy_missing", retryable=False)
        if "responses_api" not in required_capabilities or "session_key_header" not in required_capabilities:
            raise ProvisioningError("cloud_capability_policy_incomplete", retryable=False)

        try:
            async with self.http_client_factory(timeout=HERMES_CLOUD_PREFLIGHT_TIMEOUT_SECONDS) as client:
                deadline = asyncio.get_running_loop().time() + HERMES_CLOUD_PREFLIGHT_TIMEOUT_SECONDS
                health_response = None
                health = None
                while True:
                    health_response = await client.get(
                        f"{base_url}/health/detailed",
                        headers=_headers(token),
                    )
                    if health_response.status_code in {401, 403}:
                        raise ProvisioningError("hermes_cloud_auth_failed", retryable=False)
                    if health_response.status_code == 200:
                        try:
                            health = health_response.json()
                        except ValueError as exc:
                            raise ProvisioningError(
                                "invalid_hermes_cloud_preflight_receipt",
                                retryable=True,
                            ) from exc
                        if _health_ready(health):
                            break
                    elif health_response.status_code not in {425, 429} and health_response.status_code < 500:
                        raise ProvisioningError(
                            f"hermes_cloud_health_http_{health_response.status_code}",
                            retryable=False,
                        )
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ProvisioningError("hermes_cloud_wake_timeout", retryable=True)
                    await asyncio.sleep(0.5)

                capabilities_response, models_response, toolsets_response = await asyncio.gather(
                    client.get(f"{base_url}/v1/capabilities", headers=_headers(token)),
                    client.get(f"{base_url}/v1/models", headers=_headers(token)),
                    client.get(f"{base_url}/v1/toolsets", headers=_headers(token)),
                )
        except httpx.TimeoutException as exc:
            raise ProvisioningError("hermes_cloud_preflight_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProvisioningError("hermes_cloud_unavailable", retryable=True) from exc

        responses = {
            "capabilities": capabilities_response,
            "models": models_response,
            "toolsets": toolsets_response,
        }
        for name, response in responses.items():
            if response.status_code in {401, 403}:
                raise ProvisioningError("hermes_cloud_auth_failed", retryable=False)
            if response.status_code != 200:
                raise ProvisioningError(
                    f"hermes_cloud_{name}_http_{response.status_code}",
                    retryable=response.status_code >= 500,
                )

        try:
            capabilities = capabilities_response.json()
            models = models_response.json()
            toolsets = toolsets_response.json()
        except ValueError as exc:
            raise ProvisioningError("invalid_hermes_cloud_preflight_receipt", retryable=True) from exc
        assert isinstance(health, dict)
        if not isinstance(capabilities, dict):
            raise ProvisioningError("invalid_hermes_capability_receipt", retryable=True)
        missing_capabilities = sorted(
            name for name in required_capabilities if not _capability_enabled(capabilities, name)
        )
        if missing_capabilities:
            raise ProvisioningError(
                "hermes_cloud_capability_drift",
                retryable=False,
                detail={"missing_capabilities": missing_capabilities},
            )

        model_rows = models.get("data") if isinstance(models, dict) else None
        observed_models = {
            str(item.get("id") or "")
            for item in (model_rows or [])
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        if expected_model not in observed_models:
            raise ProvisioningError(
                "hermes_cloud_model_drift",
                retryable=False,
                detail={"expected_model": expected_model},
            )
        observed_tools = _observed_tools(toolsets)
        if observed_tools != allowed_tools:
            raise ProvisioningError(
                "hermes_cloud_tool_drift",
                retryable=False,
                detail={
                    "missing_tools": sorted(allowed_tools - observed_tools),
                    "unexpected_tools": sorted(observed_tools - allowed_tools),
                },
            )

        receipt = {
            "status": "ok",
            "model": expected_model,
            "tools": sorted(observed_tools),
            "capabilities": sorted(required_capabilities),
            "health_sha256": _content_hash(health),
            "capabilities_sha256": _content_hash(capabilities),
            "models_sha256": _content_hash(models),
            "toolsets_sha256": _content_hash(toolsets),
            "prompt_artifacts": prompt_artifacts,
            "content_free": True,
        }
        return HermesCloudPreflight(
            model=expected_model,
            tools=tuple(sorted(observed_tools)),
            capabilities=tuple(sorted(required_capabilities)),
            receipt=receipt,
        )

    async def create_response(
        self,
        binding: dict[str, Any],
        *,
        session_key: str,
        hermes_session_id: str,
        idempotency_key: str,
        user_input: str,
        instructions: str,
        previous_response_id: Optional[str] = None,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> HermesCloudTurn:
        if base_url is None or token is None:
            base_url, token = self.credentials(binding)
        else:
            base_url = validate_cloud_base_url(base_url)
            if not token:
                raise ProvisioningError("cloud_secret_unavailable", retryable=True)
        expected_model = str(binding.get("expected_model") or "").strip()
        headers = {
            **_headers(token),
            "X-Hermes-Session-Key": session_key,
            "X-Hermes-Session-Id": hermes_session_id,
            "Idempotency-Key": idempotency_key,
        }
        payload: dict[str, Any] = {
            "model": expected_model,
            "input": user_input,
            "instructions": instructions,
            "store": True,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        try:
            async with self.http_client_factory(timeout=HERMES_CLOUD_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ProvisioningError("hermes_cloud_turn_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProvisioningError("hermes_cloud_unavailable", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProvisioningError("hermes_cloud_auth_failed", retryable=False)
        if response.status_code == 409:
            raise ProvisioningError("hermes_cloud_idempotency_conflict", retryable=True)
        if response.status_code != 200:
            raise ProvisioningError(
                f"hermes_cloud_turn_http_{response.status_code}",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProvisioningError("invalid_hermes_cloud_response", retryable=True) from exc
        if not isinstance(body, dict) or body.get("status") != "completed":
            raise ProvisioningError("hermes_cloud_turn_incomplete", retryable=True)
        response_id = str(body.get("id") or "").strip()
        text = _response_output_text(body)
        if not response_id or not text:
            raise ProvisioningError("invalid_hermes_cloud_response", retryable=True)
        unexpected_calls = [
            str(item.get("name") or "")
            for item in body.get("output") or []
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        allowed_tools = set(binding.get("allowed_tools") or [])
        if any(name not in allowed_tools for name in unexpected_calls):
            raise ProvisioningError("hermes_cloud_unapproved_tool_call", retryable=False)
        return HermesCloudTurn(
            response_id=response_id,
            text=text,
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else {},
            model=str(body.get("model") or expected_model),
        )


class HonchoCloudProvisionClient:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    ):
        self.base_url = validate_honcho_base_url(base_url or HONCHO_CLOUD_BASE_URL)
        self.http_client_factory = http_client_factory

    @staticmethod
    def _resource_ids(binding_id: str) -> tuple[str, str, str]:
        opaque = hashlib.sha256(str(binding_id).encode("utf-8")).hexdigest()[:24]
        return (
            f"ella-{opaque}",
            f"user-{opaque}",
            f"companion-{opaque}",
        )

    async def ensure_profile(self, binding: dict[str, Any]) -> dict[str, str]:
        token = resolve_cloud_secret(binding.get("honcho_api_key_ref"))
        workspace, observed_peer, observer_peer = self._resource_ids(str(binding["id"]))
        calls = (
            (
                f"{self.base_url}/v3/workspaces",
                {"id": workspace, "metadata": {"source": "ella_runtime_binding"}},
                workspace,
            ),
            (
                f"{self.base_url}/v3/workspaces/{quote(workspace, safe='')}/peers",
                {"id": observed_peer, "metadata": {"role": "user"}},
                observed_peer,
            ),
            (
                f"{self.base_url}/v3/workspaces/{quote(workspace, safe='')}/peers",
                {"id": observer_peer, "metadata": {"role": "companion"}},
                observer_peer,
            ),
        )
        try:
            async with self.http_client_factory(timeout=HERMES_CLOUD_PREFLIGHT_TIMEOUT_SECONDS) as client:
                for url, payload, expected_id in calls:
                    response = await client.post(url, headers=_headers(token), json=payload)
                    if response.status_code in {401, 403}:
                        raise ProvisioningError("honcho_cloud_auth_failed", retryable=False)
                    if response.status_code not in {200, 201}:
                        raise ProvisioningError(
                            f"honcho_cloud_setup_http_{response.status_code}",
                            retryable=response.status_code >= 500 or response.status_code == 429,
                        )
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProvisioningError("invalid_honcho_cloud_receipt", retryable=True) from exc
                    if not isinstance(body, dict) or str(body.get("id") or "") != expected_id:
                        raise ProvisioningError("honcho_cloud_identity_mismatch", retryable=False)
        except httpx.TimeoutException as exc:
            raise ProvisioningError("honcho_cloud_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProvisioningError("honcho_cloud_unavailable", retryable=True) from exc
        return {
            "workspace": workspace,
            "observed_peer": observed_peer,
            "observer_peer": observer_peer,
        }


class RuntimePoolAlertPublisher:
    """Best-effort content-free notification; the database outbox is authoritative."""

    def __init__(
        self,
        *,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    ):
        self.http_client_factory = http_client_factory

    async def publish(self, pool_state: dict[str, Any]) -> bool:
        alert = pool_state.get("alert")
        url = os.getenv("ELLA_RUNTIME_POOL_ALERT_WEBHOOK_URL", "").strip()
        token_ref = os.getenv("ELLA_RUNTIME_POOL_ALERT_TOKEN_REF", "").strip()
        if not alert or not url:
            return False
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProvisioningError("invalid_runtime_pool_alert_url", retryable=False)
        headers = {"Content-Type": "application/json"}
        if token_ref:
            headers["Authorization"] = f"Bearer {resolve_cloud_secret(token_ref)}"
        payload = {
            "event": "ella_runtime_pool_low_water",
            "provider": "hermes_cloud",
            "available": int(pool_state["available"]),
            "threshold": int(alert["threshold"]),
            "alert_id": str(alert["id"]),
            "content_free": True,
        }
        try:
            async with self.http_client_factory(timeout=POOL_ALERT_TIMEOUT_SECONDS) as client:
                response = await client.post(url, headers=headers, json=payload)
            return 200 <= response.status_code < 300
        except httpx.HTTPError:
            return False


class HermesCloudPoolManager:
    """Register only vendor instances that pass the exact runtime policy."""

    def __init__(self, *, repository: Any, cloud_client: Optional[HermesCloudClient] = None):
        self.repository = repository
        self.cloud_client = cloud_client or HermesCloudClient()

    async def register(self, candidate: dict[str, Any]) -> dict[str, Any]:
        preflight = await self.cloud_client.preflight(candidate)
        binding = await self.repository.register_cloud_pool_binding(
            runtime_instance_id=str(candidate["runtime_instance_id"]),
            profile_name=str(candidate["profile_name"]),
            agent_id=str(candidate["agent_id"]),
            api_base_url_ref=str(candidate["api_base_url_ref"]),
            api_key_ref=str(candidate["api_key_ref"]),
            honcho_api_key_ref=str(candidate["honcho_api_key_ref"]),
            template_version=str(candidate["template_version"]),
            prompt_pack_version=str(candidate["prompt_pack_version"]),
            prompt_artifact_receipt=dict(preflight.receipt["prompt_artifacts"]),
            model_policy_version=str(candidate["model_policy_version"]),
            voice_policy_version=str(candidate["voice_policy_version"]),
            expected_model=preflight.model,
            allowed_tools=list(preflight.tools),
            required_capabilities=list(preflight.capabilities),
            health_receipt=preflight.receipt,
        )
        return {
            "binding_id": str(binding["id"]),
            "runtime_instance_id": str(binding["runtime_instance_id"]),
            "status": str(binding["status"]),
            "health_state": str(binding["health_state"]),
            "model": preflight.model,
            "tools": list(preflight.tools),
            "capabilities": list(preflight.capabilities),
            "content_free": True,
        }
