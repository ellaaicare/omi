#!/usr/bin/env python3
"""Secret-free Hermes Cloud warm-pool registration and inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from database.ella_provisioning import EllaProvisioningRepository
from database.runtime_targets import CLOUD_RUNTIME_MODEL, CLOUD_RUNTIME_PROVIDER
from ella.services.ai_consent import (
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud import HermesCloudPoolManager
from ella.services.hermes_cloud_policy import (
    cloud_synthetic_only,
    current_cloud_authority,
)


async def register(candidate_path: str) -> dict:
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    if not isinstance(candidate, dict):
        raise SystemExit("candidate must be a JSON object")
    repository = await EllaProvisioningRepository.create()
    await repository.assert_schema_ready()
    await repository.assert_cloud_schema_ready()
    return await HermesCloudPoolManager(repository=repository).register(candidate)


async def list_pool() -> dict:
    repository = await EllaProvisioningRepository.create()
    await repository.assert_cloud_schema_ready()
    rows = await repository.list_cloud_pool_bindings()
    return {
        "provider": "hermes_cloud",
        "count": len(rows),
        "bindings": [
            {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}
            for row in rows
        ],
        "content_free": True,
    }


async def promote(
    *,
    uid: str,
    binding_id: str,
    expected_revision: int,
    target_status: str,
) -> dict:
    repository = await EllaProvisioningRepository.create()
    await repository.assert_cloud_schema_ready()
    binding = await repository.get_cloud_binding_for_owner(
        uid=uid,
        binding_id=binding_id,
    )
    if not binding:
        raise SystemExit("Cloud binding not found")
    health_receipt = binding.get("health_receipt") or {}
    if isinstance(health_receipt, str):
        health_receipt = json.loads(health_receipt)
    admitted_entitlement_revision = int(health_receipt.get("admission_revision") or 0)
    if admitted_entitlement_revision <= 0:
        raise SystemExit("Cloud binding admission lineage is incomplete")
    required_profile_class = "synthetic" if cloud_synthetic_only() else "real"
    authority = current_cloud_authority(
        uid,
        profile_class=required_profile_class,
        profile_uid=uid,
        runtime_provider=CLOUD_RUNTIME_PROVIDER,
        model_route=f"openai-codex/{CLOUD_RUNTIME_MODEL}",
        memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
        photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
    )
    row = await repository.promote_cloud_binding(
        uid=uid,
        binding_id=binding_id,
        expected_revision=expected_revision,
        target_status=target_status,
        required_profile_class=required_profile_class,
        admitted_entitlement_revision=admitted_entitlement_revision,
        authority_lineage=authority.lineage,
        provider=CLOUD_RUNTIME_PROVIDER,
        model=CLOUD_RUNTIME_MODEL,
    )
    return {
        "binding_id": str(row["id"]),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "content_free": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--candidate", required=True)
    subparsers.add_parser("list")
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--uid", required=True)
    promote_parser.add_argument("--binding-id", required=True)
    promote_parser.add_argument("--expected-revision", type=int, required=True)
    promote_parser.add_argument(
        "--target-status",
        choices=("internal_canary", "active"),
        required=True,
    )
    args = parser.parse_args()
    if args.command == "register":
        result = asyncio.run(register(args.candidate))
    elif args.command == "promote":
        result = asyncio.run(
            promote(
                uid=args.uid,
                binding_id=args.binding_id,
                expected_revision=args.expected_revision,
                target_status=args.target_status,
            )
        )
    else:
        result = asyncio.run(list_pool())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
