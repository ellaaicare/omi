#!/usr/bin/env python3
"""Root-only two-phase binding of iMessage to the preserved retained runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from database.ella_provisioning import get_pool
from database.imessage_retained_runtime import (
    CONTRACT_ID,
    RetainedImessageRuntimeError,
    RetainedImessageRuntimeRepository,
    RetainedImessageRuntimeSpec,
)
from ella.services.provisioning import PROFILE_NAME_RE, ProvisioningError, validate_internal_gateway_url

MANIFEST_CONTRACT = "ella.imessage.retained_runtime_manifest.v1"
HEALTH_CONTRACT = "ella.imessage.retained_runtime_health.v1"
_REFERENCE_RE = re.compile(r"^[A-Z][A-Z0-9_]{7,127}$")
_TEXT_RE = re.compile(r"^[A-Za-z0-9._:/+-]{1,255}$")
_SPEC_FIELDS = {
    "profile_name",
    "agent_id",
    "workspace_root",
    "internal_gateway_url",
    "gateway_port",
    "service_label",
    "credential_ref",
    "honcho_workspace",
    "observed_peer",
    "observer_peer",
    "template_version",
    "model_policy_version",
    "voice_policy_version",
}
_HEALTH_FIELDS = {
    "contract",
    "manifest_sha256",
    "profile_home_match",
    "workspace_match",
    "service_match",
    "endpoint_match",
    "credential_ref_resolved",
    "health_status",
}


class AdminInputError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _file_stat(path: Path) -> os.stat_result:
    return path.stat()


def _load_protected_json(path_value: str, *, kind: str) -> tuple[dict[str, Any], str]:
    if os.geteuid() != 0:
        raise AdminInputError("imessage_retained_admin_root_required")
    if not path_value or path_value != path_value.strip():
        raise AdminInputError(f"imessage_retained_{kind}_path_missing")
    path = Path(path_value)
    metadata = _file_stat(path)
    if metadata.st_uid != 0 or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdminInputError(f"imessage_retained_{kind}_file_insecure")
    raw = path.read_bytes()
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminInputError(f"imessage_retained_{kind}_invalid") from exc
    if not isinstance(body, dict):
        raise AdminInputError(f"imessage_retained_{kind}_invalid")
    return body, hashlib.sha256(raw).hexdigest()


def _parse_manifest(body: dict[str, Any]) -> RetainedImessageRuntimeSpec:
    if set(body) != {"contract", *_SPEC_FIELDS} or body.get("contract") != MANIFEST_CONTRACT:
        raise AdminInputError("imessage_retained_manifest_invalid")
    values = {field: body[field] for field in _SPEC_FIELDS}
    if values["profile_name"] != "plato-eval" or not PROFILE_NAME_RE.fullmatch(values["profile_name"]):
        raise AdminInputError("imessage_retained_profile_invalid")
    profiles_root = os.getenv("ELLA_HERMES_PROFILES_ROOT", "/Users/ellaai/.hermes/profiles")
    expected_workspace = posixpath.normpath(f"{profiles_root.rstrip('/')}/plato-eval/workspace")
    if posixpath.normpath(str(values["workspace_root"])) != expected_workspace:
        raise AdminInputError("imessage_retained_workspace_mismatch")
    if values["service_label"] != "ai.hermes.gateway-plato-eval":
        raise AdminInputError("imessage_retained_service_mismatch")
    if not isinstance(values["gateway_port"], int) or not 1024 <= values["gateway_port"] <= 65535:
        raise AdminInputError("imessage_retained_gateway_port_invalid")
    try:
        gateway_url = validate_internal_gateway_url(str(values["internal_gateway_url"]))
    except ProvisioningError as exc:
        raise AdminInputError("imessage_retained_gateway_not_loopback") from exc
    parsed = urlparse(gateway_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != values["gateway_port"]
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise AdminInputError("imessage_retained_gateway_not_loopback")
    if not _REFERENCE_RE.fullmatch(str(values["credential_ref"])):
        raise AdminInputError("imessage_retained_credential_ref_invalid")
    for field in _SPEC_FIELDS - {"workspace_root", "internal_gateway_url", "gateway_port", "credential_ref"}:
        if not isinstance(values[field], str) or not _TEXT_RE.fullmatch(values[field]):
            raise AdminInputError(f"imessage_retained_{field}_invalid")
    return RetainedImessageRuntimeSpec(**values)


def _parse_health(body: dict[str, Any], *, manifest_sha256: str) -> None:
    if set(body) != _HEALTH_FIELDS or body.get("contract") != HEALTH_CONTRACT:
        raise AdminInputError("imessage_retained_health_receipt_invalid")
    if body.get("manifest_sha256") != manifest_sha256 or body.get("health_status") != "healthy":
        raise AdminInputError("imessage_retained_health_receipt_mismatch")
    for field in (
        "profile_home_match",
        "workspace_match",
        "service_match",
        "endpoint_match",
        "credential_ref_resolved",
    ):
        if body.get(field) is not True:
            raise AdminInputError("imessage_retained_health_receipt_mismatch")


def _receipt(*, state: str, row: Any, manifest_sha256: str, health_receipt_sha256: str = "") -> dict[str, Any]:
    binding_id = str(row.get("id") if isinstance(row, dict) else "")
    return {
        "contract": CONTRACT_ID,
        "state": state,
        "binding_fingerprint": hashlib.sha256(binding_id.encode()).hexdigest()[:16] if binding_id else None,
        "manifest_sha256": manifest_sha256,
        "health_receipt_sha256": health_receipt_sha256 or None,
    }


async def _run(action: str) -> dict[str, Any]:
    owner_uid = os.getenv("ELLA_PLATO_UID", "").strip()
    if not owner_uid:
        raise AdminInputError("imessage_retained_owner_not_configured")
    manifest, manifest_sha256 = _load_protected_json(
        os.getenv("ELLA_IMESSAGE_RETAINED_RUNTIME_MANIFEST", ""),
        kind="manifest",
    )
    spec = _parse_manifest(manifest)
    pool = await get_pool()
    repository = RetainedImessageRuntimeRepository(pool, owner_uid=owner_uid)
    try:
        if action == "stage":
            row, created = await repository.stage(
                uid=owner_uid,
                spec=spec,
                manifest_sha256=manifest_sha256,
            )
            return _receipt(
                state="staged" if created else "staged_existing",
                row=row,
                manifest_sha256=manifest_sha256,
            )
        if action == "activate":
            health, health_sha256 = _load_protected_json(
                os.getenv("ELLA_IMESSAGE_RETAINED_RUNTIME_HEALTH_RECEIPT", ""),
                kind="health_receipt",
            )
            _parse_health(health, manifest_sha256=manifest_sha256)
            row, changed = await repository.activate(
                uid=owner_uid,
                spec=spec,
                manifest_sha256=manifest_sha256,
                health_receipt_sha256=health_sha256,
            )
            return _receipt(
                state="active" if changed else "active_existing",
                row=row,
                manifest_sha256=manifest_sha256,
                health_receipt_sha256=health_sha256,
            )
        removed = await repository.rollback(uid=owner_uid, manifest_sha256=manifest_sha256)
        return {
            "contract": CONTRACT_ID,
            "state": "removed" if removed else "absent",
            "binding_fingerprint": None,
            "manifest_sha256": manifest_sha256,
            "health_receipt_sha256": None,
        }
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "activate", "rollback"))
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args.action))
    except (AdminInputError, RetainedImessageRuntimeError) as exc:
        print(json.dumps({"status": "refused", "code": exc.code}, sort_keys=True))
        return 78
    except Exception:
        print(json.dumps({"status": "failed", "code": "imessage_retained_admin_failed"}, sort_keys=True))
        return 70
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
