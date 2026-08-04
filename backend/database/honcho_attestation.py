"""Authenticated Honcho isolation-attestation contract.

The first-party backend creates a fresh, context-bound challenge before provisioning.
The provisioner must read the profile-local ``honcho.json`` and return the
exact schema below, signed with the separately scoped attestation key.  A set
of mutually agreeing response objects is not authority without this MAC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Mapping

ATTESTATION_VERSION = "honcho-isolation-v2"
ATTESTATION_ISSUER = "hermes-provisioner"
ATTESTATION_TTL_SECONDS = 360
ATTESTATION_KEY_ENV = "ELLA_HERMES_PROVISION_ATTESTATION_KEY"

# This contract is intentionally name-based instead of a second hand-maintained
# list of currently known callers.  Every server-owned secret-like environment
# credential is a distinct authority from the Honcho attestation MAC.  Dynamic
# runtime credentials and configured secret references are covered without
# trusting receipt/provider metadata to choose the comparison set.
AUTHORITY_CREDENTIAL_ENV_SUFFIXES = ("_KEY", "_KEYS", "_PASSWORD", "_SECRET", "_TOKEN", "_TOKENS")
AUTHORITY_CREDENTIAL_ENV_PREFIXES = (
    "ELLA_HERMES_GATEWAY_KEY_",
    "ELLA_HERMES_CLOUD_API_KEY_",
    "ELLA_HONCHO_CLOUD_API_KEY_",
)
AUTHORITY_CREDENTIAL_REFERENCE_ENV_NAMES = (
    "ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF",
    "ELLA_HERMES_BROKER_SERVICE_TOKEN_REF",
    "ELLA_RUNTIME_POOL_ALERT_TOKEN_REF",
)
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,120}$")
_OBSERVED_AUTHORITY_VALUES: set[bytes] = set()
_OBSERVED_AUTHORITY_VALUES_LOCK = threading.Lock()

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CHALLENGE_FIELDS = {
    "version",
    "nonce",
    "issued_at",
    "expires_at",
    "firebase_uid",
    "account_owner_id",
    "runtime_target_id",
    "binding_id",
    "job_id",
}
_READBACK_FIELDS = {
    "issuer",
    "provider",
    "profile_name",
    "config_path_sha256",
    "workspace_root_sha256",
    "honcho_workspace",
    "observed_peer_id",
    "observer_peer_id",
    "gateway_port",
    "gateway_target_sha256",
    "credential_ref_sha256",
    "agent_id",
    "service_label",
}
ATTESTATION_FIELDS = _CHALLENGE_FIELDS | _READBACK_FIELDS | {"signature"}


class HonchoAttestationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_authority_credential_environment_name(name: str) -> bool:
    """Classify server credential names without consulting caller/provider data."""
    return bool(
        name != ATTESTATION_KEY_ENV
        and _ENVIRONMENT_NAME_RE.fullmatch(name)
        and (
            name.endswith(AUTHORITY_CREDENTIAL_ENV_SUFFIXES)
            or any(name.startswith(prefix) for prefix in AUTHORITY_CREDENTIAL_ENV_PREFIXES)
        )
    )


def authority_credential(*environment_names: str, strip: bool = True) -> str:
    """Resolve one runtime authority through the same contract used for separation."""
    for name in environment_names:
        if not is_authority_credential_environment_name(name):
            raise ValueError("invalid_authority_credential_reference")
        value = os.getenv(name, "")
        effective = value.strip() if strip else value
        if effective:
            with _OBSERVED_AUTHORITY_VALUES_LOCK:
                _OBSERVED_AUTHORITY_VALUES.add(effective.encode("utf-8"))
            return effective
    return ""


def _configured_authority_reference_names() -> set[str]:
    names: set[str] = set()
    for reference_environment in AUTHORITY_CREDENTIAL_REFERENCE_ENV_NAMES:
        reference = os.getenv(reference_environment, "")
        if not reference.startswith("env:"):
            continue
        referenced_name = reference[4:]
        if _ENVIRONMENT_NAME_RE.fullmatch(referenced_name):
            names.add(referenced_name)
    return names


def _cross_authority_environment_names() -> tuple[str, ...]:
    names = {name for name in os.environ if is_authority_credential_environment_name(name)}
    names.update(_configured_authority_reference_names())
    return tuple(sorted(names))


def _effective_authority_values(name: str) -> tuple[str, ...]:
    candidate = os.getenv(name, "")
    if not candidate:
        return ()
    values = {candidate, candidate.strip()}
    if name.endswith(("_KEYS", "_TOKENS")):
        values.update(item.strip() for item in candidate.split(","))
    return tuple(value for value in values if value)


def _attestation_key() -> bytes:
    raw = os.getenv(ATTESTATION_KEY_ENV, "")
    if raw != raw.strip() or any(character.isspace() for character in raw) or len(raw) < 32:
        raise HonchoAttestationError("honcho_attestation_key_unavailable")
    raw_bytes = raw.encode("utf-8")
    with _OBSERVED_AUTHORITY_VALUES_LOCK:
        observed_values = tuple(_OBSERVED_AUTHORITY_VALUES)
    conflict = False
    for value in observed_values:
        conflict |= hmac.compare_digest(raw_bytes, value)
    for name in _cross_authority_environment_names():
        for value in _effective_authority_values(name):
            conflict |= hmac.compare_digest(raw_bytes, value.encode("utf-8"))
    if conflict:
        raise HonchoAttestationError("honcho_attestation_key_conflict")
    return raw_bytes


def _canonical_payload(attestation: Mapping[str, Any]) -> bytes:
    payload = {key: attestation[key] for key in sorted(ATTESTATION_FIELDS - {"signature"})}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def calculate_signature(attestation: Mapping[str, Any]) -> str:
    """Return the contract MAC; used by the provisioner and deterministic tests."""
    return hmac.new(_attestation_key(), _canonical_payload(attestation), hashlib.sha256).hexdigest()


def create_challenge(
    *,
    firebase_uid: str,
    account_owner_id: str,
    runtime_target_id: str,
    binding_id: str,
    job_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    _attestation_key()
    issued_at = int(time.time() if now is None else now)
    values = {
        "version": ATTESTATION_VERSION,
        "nonce": secrets.token_urlsafe(32),
        "issued_at": issued_at,
        "expires_at": issued_at + ATTESTATION_TTL_SECONDS,
        "firebase_uid": str(firebase_uid),
        "account_owner_id": str(account_owner_id),
        "runtime_target_id": str(runtime_target_id),
        "binding_id": str(binding_id),
        "job_id": str(job_id),
    }
    if not all(str(values[field]).strip() for field in _CHALLENGE_FIELDS - {"issued_at", "expires_at"}):
        raise HonchoAttestationError("honcho_attestation_context_incomplete")
    return values


def observed_runtime_fields(
    *,
    profile_name: str,
    config_path: str,
    workspace_root: str,
    honcho_workspace: str,
    observed_peer_id: str,
    observer_peer_id: str,
    gateway_port: int,
    gateway_target: str,
    credential_ref: str,
    agent_id: str,
    service_label: str,
) -> dict[str, Any]:
    return {
        "issuer": ATTESTATION_ISSUER,
        "provider": "hermes",
        "profile_name": str(profile_name),
        "config_path_sha256": content_hash(str(config_path)),
        "workspace_root_sha256": content_hash(str(workspace_root)),
        "honcho_workspace": str(honcho_workspace),
        "observed_peer_id": str(observed_peer_id),
        "observer_peer_id": str(observer_peer_id),
        "gateway_port": int(gateway_port),
        "gateway_target_sha256": content_hash(str(gateway_target)),
        "credential_ref_sha256": content_hash(str(credential_ref)),
        "agent_id": str(agent_id),
        "service_label": str(service_label),
    }


def verify_attestation(
    attestation: Any,
    *,
    expected_challenge: Mapping[str, Any],
    observed: Mapping[str, Any],
    now: int | None = None,
    require_fresh: bool,
) -> dict[str, Any]:
    if not isinstance(attestation, dict) or set(attestation) != ATTESTATION_FIELDS:
        raise HonchoAttestationError("honcho_attestation_malformed")
    if set(expected_challenge) != _CHALLENGE_FIELDS or set(observed) != _READBACK_FIELDS:
        raise HonchoAttestationError("honcho_attestation_expected_context_incomplete")
    if any(attestation.get(field) != expected_challenge.get(field) for field in _CHALLENGE_FIELDS):
        raise HonchoAttestationError("honcho_attestation_context_mismatch")
    if any(attestation.get(field) != observed.get(field) for field in _READBACK_FIELDS):
        raise HonchoAttestationError("honcho_attestation_readback_mismatch")

    nonce = str(attestation["nonce"])
    issued_at = attestation["issued_at"]
    expires_at = attestation["expires_at"]
    if (
        not _NONCE_RE.fullmatch(nonce)
        or type(issued_at) is not int
        or type(expires_at) is not int
        or expires_at - issued_at != ATTESTATION_TTL_SECONDS
    ):
        raise HonchoAttestationError("honcho_attestation_freshness_invalid")
    checked_at = int(time.time() if now is None else now)
    if require_fresh and not issued_at <= checked_at <= expires_at:
        raise HonchoAttestationError("honcho_attestation_stale")

    signature = attestation["signature"]
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        raise HonchoAttestationError("honcho_attestation_integrity_invalid")
    expected_signature = calculate_signature(attestation)
    if not hmac.compare_digest(signature, expected_signature):
        raise HonchoAttestationError("honcho_attestation_integrity_invalid")
    return {key: attestation[key] for key in sorted(ATTESTATION_FIELDS)}


def verify_persisted_attestation(
    evidence: Any,
    *,
    expected_challenge: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(evidence, dict) or set(evidence) != {"attestation", "verified_at"}:
        raise HonchoAttestationError("honcho_attestation_evidence_malformed")
    verified_at = evidence.get("verified_at")
    if type(verified_at) is not int:
        raise HonchoAttestationError("honcho_attestation_evidence_malformed")
    attestation = verify_attestation(
        evidence.get("attestation"),
        expected_challenge=expected_challenge,
        observed=observed,
        now=verified_at,
        require_fresh=True,
    )
    return {"attestation": attestation, "verified_at": verified_at}
