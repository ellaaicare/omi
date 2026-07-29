"""Fail-closed allowlist for the ordinary Hermes webhook-broker prototype.

Default OFF. Only an exact synthetic account/profile (and optional binding id)
may leave the direct Hermes Cloud `/v1/responses` path. All other users keep
the existing transport byte-for-byte.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime

# Exact lowercase only — no permissive truthy aliases.
_TRUE = "true"

# Fixed private broker path prefixes on the allowlisted host.
ADMIT_PATH = "/v1/ella/internal/hermes-webhook-broker/admit"
# Companion contract (not yet on merged ella-ai main): bounded result wait.
# Documented in backend/ella/docs/HERMES_BROKER_PROTOTYPE_RUNBOOK.md.
RESULT_PATH_PREFIX = "/v1/ella/internal/hermes-webhook-broker/requests/"

CHAT_LANE = "chat_turn"
TRANSCRIPT_LANE = "transcript_summary_enrichment"
USER_SUMMARY_PASS = "user_summary"


@dataclass(frozen=True)
class HermesBrokerPrototypeConfig:
    """Secret-free runtime configuration (values resolved only via refs)."""

    enabled: bool
    account_id: str
    profile_id: str
    binding_id: str
    base_url: str
    allowed_host: str
    service_token_ref: str
    poll_interval_seconds: float
    poll_timeout_seconds: float
    deadline_seconds: int


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def prototype_enabled_raw() -> bool:
    # Exact lowercase "true" only — no case-insensitive truthy aliases.
    return _env("ELLA_HERMES_BROKER_PROTOTYPE_ENABLED") == _TRUE


def load_prototype_config() -> Optional[HermesBrokerPrototypeConfig]:
    """Return config only when the global prototype switch is exactly enabled."""
    if not prototype_enabled_raw():
        return None
    account_id = _env("ELLA_HERMES_BROKER_PROTOTYPE_ACCOUNT_ID")
    profile_id = _env("ELLA_HERMES_BROKER_PROTOTYPE_PROFILE_ID") or account_id
    binding_id = _env("ELLA_HERMES_BROKER_PROTOTYPE_BINDING_ID")
    base_url = _env("ELLA_HERMES_BROKER_BASE_URL").rstrip("/")
    allowed_host = _env("ELLA_HERMES_BROKER_ALLOWED_HOST")
    service_token_ref = _env(
        "ELLA_HERMES_BROKER_SERVICE_TOKEN_REF",
        "env:ELLA_HERMES_BROKER_SERVICE_TOKEN",
    )
    if not account_id or not profile_id or not base_url or not allowed_host:
        raise ProvisioningError(
            "hermes_broker_prototype_config_invalid",
            retryable=False,
        )
    if not service_token_ref.startswith("env:") or len(service_token_ref) < 8:
        raise ProvisioningError(
            "hermes_broker_prototype_token_ref_invalid",
            retryable=False,
        )
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.port not in (None, 443))
        or parsed.hostname != allowed_host
    ):
        raise ProvisioningError(
            "hermes_broker_prototype_url_not_allowlisted",
            retryable=False,
        )
    try:
        poll_interval = float(_env("ELLA_HERMES_BROKER_POLL_INTERVAL_SECONDS", "0.5"))
        poll_timeout = float(_env("ELLA_HERMES_BROKER_POLL_TIMEOUT_SECONDS", "45"))
        deadline_seconds = int(_env("ELLA_HERMES_BROKER_CALLBACK_DEADLINE_SECONDS", "90"))
    except ValueError as exc:
        raise ProvisioningError(
            "hermes_broker_prototype_config_invalid",
            retryable=False,
        ) from exc
    if not (0.1 <= poll_interval <= 5.0 and 1.0 <= poll_timeout <= 120.0):
        raise ProvisioningError(
            "hermes_broker_prototype_config_invalid",
            retryable=False,
        )
    if not (10 <= deadline_seconds <= 600):
        raise ProvisioningError(
            "hermes_broker_prototype_config_invalid",
            retryable=False,
        )
    return HermesBrokerPrototypeConfig(
        enabled=True,
        account_id=account_id,
        profile_id=profile_id,
        binding_id=binding_id,
        base_url=base_url,
        allowed_host=allowed_host,
        service_token_ref=service_token_ref,
        poll_interval_seconds=poll_interval,
        poll_timeout_seconds=poll_timeout,
        deadline_seconds=deadline_seconds,
    )


def runtime_uses_broker_prototype(
    runtime: IsolatedRuntime,
    *,
    config: Optional[HermesBrokerPrototypeConfig] = None,
) -> bool:
    """True only for the exact allowlisted synthetic cloud binding."""
    cfg = config if config is not None else load_prototype_config()
    if cfg is None:
        return False
    if runtime.provider != "hermes_cloud":
        return False
    if str(runtime.profile_class or "").lower() != "synthetic":
        return False
    if not hmac.compare_digest(str(runtime.uid), cfg.account_id):
        return False
    # Prototype pins one exact profile id (typically equal to the synthetic uid).
    if not hmac.compare_digest(str(runtime.uid), cfg.profile_id):
        return False
    if cfg.binding_id and not hmac.compare_digest(str(runtime.binding_id), cfg.binding_id):
        return False
    if runtime.runtime_target_mode and runtime.runtime_target_mode not in {
        "hermes-cloud-chat",
        "hermes-cloud-transcript",
        "hermes-cloud-photon",
    }:
        return False
    return True


def resolve_service_token(token_ref: str) -> str:
    """Resolve env:NAME only. Never logs the value."""
    if not token_ref.startswith("env:"):
        raise ProvisioningError(
            "hermes_broker_prototype_token_ref_invalid",
            retryable=False,
        )
    name = token_ref[4:]
    value = os.getenv(name)
    if not value:
        raise ProvisioningError(
            "hermes_broker_prototype_token_unavailable",
            retryable=True,
        )
    return value
