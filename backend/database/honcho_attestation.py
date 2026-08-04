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
import time
from typing import Any, Mapping

ATTESTATION_VERSION = "honcho-isolation-v2"
ATTESTATION_ISSUER = "hermes-provisioner"
ATTESTATION_TTL_SECONDS = 120
ATTESTATION_KEY_ENV = "ELLA_HERMES_PROVISION_ATTESTATION_KEY"

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


def _attestation_key() -> bytes:
    raw = os.getenv(ATTESTATION_KEY_ENV, "")
    if raw != raw.strip() or len(raw) < 32:
        raise HonchoAttestationError("honcho_attestation_key_unavailable")
    return raw.encode("utf-8")


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
