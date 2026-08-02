"""Fail-closed configuration for the retained and isolated provision APIs."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

LEGACY_PROVISION_URL_ENV = "ELLA_PROVISION_API_URL"
LEGACY_PROVISION_TOKEN_ENV = "ELLA_PROVISION_API_TOKEN"
HERMES_PROVISION_URL_ENV = "ELLA_HERMES_PROVISION_API_URL"
HERMES_PROVISION_TOKEN_ENV = "ELLA_HERMES_PROVISION_API_TOKEN"
HERMES_PROVISION_ALLOWLIST_ENV = "ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST"
HERMES_PROVISION_BINDING_REF_ENV = "ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF"

DEFAULT_LEGACY_PROVISION_URL = "http://100.76.138.56:8200"
APPROVED_HERMES_PROVISION_URL = "http://100.76.138.56:8210"

_AUTHORITY_BINDING_DOMAIN = b"ella-hermes-provision-authority-v1"
_BINDING_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_REF_RE = re.compile(r"^env:(ELLA_[A-Z0-9_]{3,120})$")


class ProvisionAuthorityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProvisionAuthority:
    base_url: str
    token: str = field(repr=False)
    token_reference: str
    binding_reference: str = ""


def _canonical_base_url(value: str, *, error_code: str) -> str:
    raw = str(value or "").strip()
    if not raw or any(character.isspace() for character in raw) or "\\" in raw or "?" in raw or "#" in raw:
        raise ProvisionAuthorityError(error_code)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ProvisionAuthorityError(error_code) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionAuthorityError(error_code)

    host = parsed.hostname.rstrip(".").lower()
    if not host or "%" in host:
        raise ProvisionAuthorityError(error_code)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ProvisionAuthorityError(error_code) from exc
        host_text = host
    else:
        host_text = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed

    scheme = parsed.scheme.lower()
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{host_text}{port_suffix}"


def _authority_binding_value(base_url: str, token: str) -> str:
    digest = hashlib.sha256()
    digest.update(_AUTHORITY_BINDING_DOMAIN)
    digest.update(b"\0")
    digest.update(base_url.encode("utf-8"))
    digest.update(b"\0")
    digest.update(token.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _resolve_binding_reference(reference: str) -> tuple[str, str]:
    match = _SECRET_REF_RE.fullmatch(str(reference or "").strip())
    if match is None:
        raise ProvisionAuthorityError("hermes_provision_authority_binding_invalid")
    variable = match.group(1)
    binding = os.getenv(variable, "").strip().lower()
    if _BINDING_RE.fullmatch(binding) is None:
        raise ProvisionAuthorityError("hermes_provision_authority_binding_invalid")
    return variable, binding


def _legacy_coordinates() -> tuple[str, str]:
    base_url = _canonical_base_url(
        os.getenv(LEGACY_PROVISION_URL_ENV, DEFAULT_LEGACY_PROVISION_URL),
        error_code="legacy_provision_authority_incomplete",
    )
    return base_url, os.getenv(LEGACY_PROVISION_TOKEN_ENV, "").strip()


def legacy_provision_authority() -> ProvisionAuthority:
    """Return the retained OpenClaw/Plato authority; never borrow Hermes credentials."""
    base_url, token = _legacy_coordinates()
    if not token:
        raise ProvisionAuthorityError("legacy_provision_authority_incomplete")

    hermes_url = os.getenv(HERMES_PROVISION_URL_ENV, "").strip()
    if hermes_url:
        hermes_base_url = _canonical_base_url(
            hermes_url,
            error_code="provision_authority_pair_conflict",
        )
        if hmac.compare_digest(base_url, hermes_base_url):
            raise ProvisionAuthorityError("provision_authority_pair_conflict")
    hermes_token = os.getenv(HERMES_PROVISION_TOKEN_ENV, "").strip()
    if hermes_token and hmac.compare_digest(token, hermes_token):
        raise ProvisionAuthorityError("provision_authority_pair_conflict")

    return ProvisionAuthority(base_url=base_url, token=token, token_reference=LEGACY_PROVISION_TOKEN_ENV)


def hermes_provision_authority() -> ProvisionAuthority:
    """Return the invitation-owned Hermes authority after all fail-closed checks."""
    raw_url = os.getenv(HERMES_PROVISION_URL_ENV, "").strip()
    token = os.getenv(HERMES_PROVISION_TOKEN_ENV, "").strip()
    binding_reference = os.getenv(HERMES_PROVISION_BINDING_REF_ENV, "").strip()
    if not raw_url or not token or not binding_reference:
        raise ProvisionAuthorityError("hermes_provision_authority_incomplete")

    base_url = _canonical_base_url(raw_url, error_code="hermes_provision_authority_destination_rejected")
    approved_destinations = {
        _canonical_base_url(
            APPROVED_HERMES_PROVISION_URL,
            error_code="hermes_provision_authority_destination_rejected",
        )
    }
    for reviewed in os.getenv(HERMES_PROVISION_ALLOWLIST_ENV, "").split(","):
        if reviewed.strip():
            approved_destinations.add(
                _canonical_base_url(
                    reviewed,
                    error_code="hermes_provision_authority_destination_rejected",
                )
            )
    if base_url not in approved_destinations:
        raise ProvisionAuthorityError("hermes_provision_authority_destination_rejected")

    legacy_base_url, legacy_token = _legacy_coordinates()
    if hmac.compare_digest(base_url, legacy_base_url) or (legacy_token and hmac.compare_digest(token, legacy_token)):
        raise ProvisionAuthorityError("provision_authority_pair_conflict")

    binding_variable, expected_binding = _resolve_binding_reference(binding_reference)
    observed_binding = _authority_binding_value(base_url, token)
    if not hmac.compare_digest(observed_binding, expected_binding):
        raise ProvisionAuthorityError("hermes_provision_authority_binding_invalid")

    return ProvisionAuthority(
        base_url=base_url,
        token=token,
        token_reference=HERMES_PROVISION_TOKEN_ENV,
        binding_reference=f"env:{binding_variable}",
    )
