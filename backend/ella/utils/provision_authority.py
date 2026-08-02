"""Fail-closed configuration for the retained and isolated provision APIs."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from urllib.parse import urlparse


LEGACY_PROVISION_URL_ENV = "ELLA_PROVISION_API_URL"
LEGACY_PROVISION_TOKEN_ENV = "ELLA_PROVISION_API_TOKEN"
HERMES_PROVISION_URL_ENV = "ELLA_HERMES_PROVISION_API_URL"
HERMES_PROVISION_TOKEN_ENV = "ELLA_HERMES_PROVISION_API_TOKEN"

DEFAULT_LEGACY_PROVISION_URL = "http://100.76.138.56:8200"
DEFAULT_HERMES_PROVISION_URL = "http://100.76.138.56:8210"


class ProvisionAuthorityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProvisionAuthority:
    base_url: str
    token: str
    token_reference: str


def _normalized_base_url(value: str, *, incomplete_code: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionAuthorityError(incomplete_code)
    return raw


def _authority_pair(
    *,
    url_env: str,
    token_env: str,
    default_url: str,
    other_url_env: str,
    other_token_env: str,
    other_default_url: str,
    incomplete_code: str,
) -> ProvisionAuthority:
    base_url = _normalized_base_url(os.getenv(url_env, default_url), incomplete_code=incomplete_code)
    token = os.getenv(token_env, "").strip()
    if not token:
        raise ProvisionAuthorityError(incomplete_code)

    other_base_url = _normalized_base_url(
        os.getenv(other_url_env, other_default_url),
        incomplete_code="provision_authority_pair_conflict",
    )
    other_token = os.getenv(other_token_env, "").strip()
    if base_url == other_base_url or (other_token and hmac.compare_digest(token, other_token)):
        raise ProvisionAuthorityError("provision_authority_pair_conflict")

    return ProvisionAuthority(base_url=base_url, token=token, token_reference=token_env)


def legacy_provision_authority() -> ProvisionAuthority:
    """Return the retained OpenClaw/Plato authority; never borrow Hermes credentials."""
    return _authority_pair(
        url_env=LEGACY_PROVISION_URL_ENV,
        token_env=LEGACY_PROVISION_TOKEN_ENV,
        default_url=DEFAULT_LEGACY_PROVISION_URL,
        other_url_env=HERMES_PROVISION_URL_ENV,
        other_token_env=HERMES_PROVISION_TOKEN_ENV,
        other_default_url=DEFAULT_HERMES_PROVISION_URL,
        incomplete_code="legacy_provision_authority_incomplete",
    )


def hermes_provision_authority() -> ProvisionAuthority:
    """Return the invitation-owned Hermes authority; never borrow legacy credentials."""
    return _authority_pair(
        url_env=HERMES_PROVISION_URL_ENV,
        token_env=HERMES_PROVISION_TOKEN_ENV,
        default_url=DEFAULT_HERMES_PROVISION_URL,
        other_url_env=LEGACY_PROVISION_URL_ENV,
        other_token_env=LEGACY_PROVISION_TOKEN_ENV,
        other_default_url=DEFAULT_LEGACY_PROVISION_URL,
        incomplete_code="hermes_provision_authority_incomplete",
    )
