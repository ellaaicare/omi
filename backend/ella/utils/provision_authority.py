"""Fail-closed configuration for the retained and isolated provision APIs."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from database.honcho_attestation import authority_credential

LEGACY_PROVISION_URL_ENV = "ELLA_PROVISION_API_URL"
LEGACY_PROVISION_TOKEN_ENV = "ELLA_PROVISION_API_TOKEN"
HERMES_PROVISION_URL_ENV = "ELLA_HERMES_PROVISION_API_URL"
HERMES_PROVISION_TOKEN_ENV = "ELLA_HERMES_PROVISION_API_TOKEN"
HERMES_PROVISION_ALLOWLIST_ENV = "ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST"
HERMES_PROVISION_BINDING_REF_ENV = "ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF"

DEFAULT_LEGACY_PROVISION_URL = "http://100.76.138.56:8200"
APPROVED_HERMES_PROVISION_URL = "http://100.76.138.56:8210"

_AUTHORITY_BINDING_DOMAIN = b"ella-hermes-provision-authority-v1"
_AUTHORITY_IDENTITY_DOMAIN = b"ella-hermes-provision-authority-identity-v1"
_AUTHORITY_IDENTITY_KEY = os.urandom(32)
_BINDING_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_REF_RE = re.compile(r"^env:(ELLA_[A-Z0-9_]{3,120})$")


class ProvisionAuthorityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ProvisionAuthoritySnapshot:
    """Opaque comparison-only identity; never renders authority material."""

    __slots__ = ("_digest",)

    def __init__(self, digest: bytes):
        self._digest = bytes(digest)

    def matches(self, other: "ProvisionAuthoritySnapshot") -> bool:
        return isinstance(other, ProvisionAuthoritySnapshot) and hmac.compare_digest(self._digest, other._digest)

    def __repr__(self) -> str:
        return "ProvisionAuthoritySnapshot(<opaque>)"

    __str__ = __repr__


@dataclass(frozen=True)
class ProvisionAuthority:
    base_url: str
    token: str = field(repr=False)
    token_reference: str
    binding_reference: str = field(default="", repr=False)
    _snapshot: ProvisionAuthoritySnapshot | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> ProvisionAuthoritySnapshot:
        if self._snapshot is None:
            raise ProvisionAuthorityError("hermes_provision_authority_incomplete")
        return self._snapshot


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


def _canonical_hermes_base_url(value: str) -> str:
    """Require the one canonical numeric Mini/Tailscale coordinate."""
    configured = str(value or "")
    raw = configured.strip()
    canonical = _canonical_base_url(raw, error_code="hermes_provision_authority_destination_rejected")
    try:
        parsed = urlsplit(canonical)
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ProvisionAuthorityError("hermes_provision_authority_destination_rejected") from exc
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or configured != raw
        or raw != canonical
        or canonical != APPROVED_HERMES_PROVISION_URL
    ):
        raise ProvisionAuthorityError("hermes_provision_authority_destination_rejected")
    return canonical


def _canonical_legacy_base_url(value: str) -> str:
    """Retain the legacy pair only as a numeric, non-DNS authority."""
    canonical = _canonical_base_url(value, error_code="legacy_provision_authority_incomplete")
    try:
        ipaddress.ip_address(urlsplit(canonical).hostname or "")
    except ValueError as exc:
        raise ProvisionAuthorityError("legacy_provision_authority_incomplete") from exc
    return canonical


def _normalized_socket_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.compressed
    return address.compressed


def _socket_destinations(base_url: str, *, error_code: str) -> tuple[str, ...]:
    """Return the numeric socket coordinate; authority hostnames are unsupported."""
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    port = parsed.port or (80 if parsed.scheme == "http" else 443)
    try:
        address = _normalized_socket_address(host)
        return (f"{address}:{port}",)
    except ValueError as exc:
        raise ProvisionAuthorityError(error_code) from exc


def _identity_snapshot(*parts: str) -> ProvisionAuthoritySnapshot:
    digest = hmac.new(_AUTHORITY_IDENTITY_KEY, digestmod=hashlib.sha256)
    digest.update(_AUTHORITY_IDENTITY_DOMAIN)
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode("utf-8"))
    return ProvisionAuthoritySnapshot(digest.digest())


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
    base_url = _canonical_legacy_base_url(os.getenv(LEGACY_PROVISION_URL_ENV, DEFAULT_LEGACY_PROVISION_URL))
    return base_url, authority_credential(LEGACY_PROVISION_TOKEN_ENV)


def legacy_provision_authority() -> ProvisionAuthority:
    """Return the retained OpenClaw/Plato authority; never borrow Hermes credentials."""
    base_url, token = _legacy_coordinates()
    if not token:
        raise ProvisionAuthorityError("legacy_provision_authority_incomplete")

    hermes_url = os.getenv(HERMES_PROVISION_URL_ENV, "").strip()
    if hermes_url:
        try:
            hermes_base_url = _canonical_hermes_base_url(hermes_url)
        except ProvisionAuthorityError as exc:
            raise ProvisionAuthorityError("provision_authority_pair_conflict") from exc
        if hmac.compare_digest(base_url, hermes_base_url) or set(
            _socket_destinations(base_url, error_code="provision_authority_pair_conflict")
        ).intersection(_socket_destinations(hermes_base_url, error_code="provision_authority_pair_conflict")):
            raise ProvisionAuthorityError("provision_authority_pair_conflict")
    hermes_token = authority_credential(HERMES_PROVISION_TOKEN_ENV)
    if hermes_token and hmac.compare_digest(token, hermes_token):
        raise ProvisionAuthorityError("provision_authority_pair_conflict")

    return ProvisionAuthority(base_url=base_url, token=token, token_reference=LEGACY_PROVISION_TOKEN_ENV)


def hermes_provision_authority(
    expected_snapshot: ProvisionAuthoritySnapshot | None = None,
) -> ProvisionAuthority:
    """Return the invitation-owned Hermes authority after all fail-closed checks."""
    raw_url = os.getenv(HERMES_PROVISION_URL_ENV, "")
    token = authority_credential(HERMES_PROVISION_TOKEN_ENV)
    binding_reference = os.getenv(HERMES_PROVISION_BINDING_REF_ENV, "").strip()
    if not raw_url or not token or not binding_reference:
        raise ProvisionAuthorityError("hermes_provision_authority_incomplete")

    base_url = _canonical_hermes_base_url(raw_url)
    reviewed_policy = tuple(
        item.strip() for item in os.getenv(HERMES_PROVISION_ALLOWLIST_ENV, "").split(",") if item.strip()
    )
    if any(reviewed != APPROVED_HERMES_PROVISION_URL for reviewed in reviewed_policy):
        raise ProvisionAuthorityError("hermes_provision_authority_destination_rejected")

    legacy_base_url, legacy_token = _legacy_coordinates()
    legacy_destinations = _socket_destinations(
        legacy_base_url,
        error_code="provision_authority_pair_conflict",
    )
    hermes_destinations = _socket_destinations(
        base_url,
        error_code="hermes_provision_authority_destination_rejected",
    )
    if (
        hmac.compare_digest(base_url, legacy_base_url)
        or set(hermes_destinations).intersection(legacy_destinations)
        or (legacy_token and hmac.compare_digest(token, legacy_token))
    ):
        raise ProvisionAuthorityError("provision_authority_pair_conflict")

    binding_variable, expected_binding = _resolve_binding_reference(binding_reference)
    observed_binding = _authority_binding_value(base_url, token)
    if not hmac.compare_digest(observed_binding, expected_binding):
        raise ProvisionAuthorityError("hermes_provision_authority_binding_invalid")

    snapshot = _identity_snapshot(
        base_url,
        token,
        HERMES_PROVISION_TOKEN_ENV,
        f"env:{binding_variable}",
        expected_binding,
        "\n".join(reviewed_policy),
        legacy_base_url,
        legacy_token,
        "\n".join(legacy_destinations),
        "\n".join(hermes_destinations),
    )
    if expected_snapshot is not None and not expected_snapshot.matches(snapshot):
        raise ProvisionAuthorityError("hermes_provision_authority_drift")

    return ProvisionAuthority(
        base_url=base_url,
        token=token,
        token_reference=HERMES_PROVISION_TOKEN_ENV,
        binding_reference=f"env:{binding_variable}",
        _snapshot=snapshot,
    )
