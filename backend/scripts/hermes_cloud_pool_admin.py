#!/usr/bin/env python3
"""Secret-free Hermes Cloud warm-pool registration and inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from database.ella_provisioning import EllaProvisioningRepository
from ella.services.hermes_cloud import HermesCloudPoolManager


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
            {
                key: value.isoformat() if hasattr(value, "isoformat") else value
                for key, value in row.items()
            }
            for row in rows
        ],
        "content_free": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--candidate", required=True)
    subparsers.add_parser("list")
    args = parser.parse_args()
    result = (
        asyncio.run(register(args.candidate))
        if args.command == "register"
        else asyncio.run(list_pool())
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
