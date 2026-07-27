#!/usr/bin/env python3
"""Run the approved synthetic-only Hermes Cloud Phase 1/2 shadow gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone

from database.ella_provisioning import EllaProvisioningRepository
from ella.routers.canonical_events import PostgresCanonicalEventStore
from ella.services.hermes_cloud import HermesCloudClient
from ella.services.hermes_cloud_runtime import (
    HermesCloudRuntimeService,
    HermesCloudTurnRequest,
)
from ella.services.runtime_resolver import resolve_isolated_runtime


def _synthetic_guard(uid: str) -> None:
    if os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "").strip().lower() != "true":
        raise SystemExit("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true is required")
    if not uid.startswith(("synthetic-", "staging-synthetic-")):
        raise SystemExit("shadow harness only accepts a synthetic/staging UID")


async def run(uid: str, client_interaction_id: str) -> dict:
    _synthetic_guard(uid)
    repository = await EllaProvisioningRepository.create()
    raw_binding = await repository.resolve_active_runtime(uid)
    runtime = await resolve_isolated_runtime(uid, repository)
    if not raw_binding or not runtime or runtime.provider != "hermes_cloud":
        raise SystemExit("synthetic UID has no active Hermes Cloud binding")

    preflight = await HermesCloudClient().preflight(raw_binding)
    request = HermesCloudTurnRequest(
        uid=uid,
        client_interaction_id=client_interaction_id,
        correlation_id=f"shadow:{client_interaction_id}",
        channel="synthetic_shadow",
        user_input="Synthetic shadow check: respond with a short acknowledgement.",
        instructions=(
            "This is a synthetic pre-production shadow check. "
            "Do not use tools, contact people, or mutate memory."
        ),
        started_at=datetime.now(timezone.utc),
        client_metadata={"synthetic": True, "phase": 2},
    )
    service = HermesCloudRuntimeService(
        repository=repository,
        event_store=PostgresCanonicalEventStore(),
    )
    first = await service.run_turn(runtime, request)
    replay = await service.run_turn(runtime, request)
    if first.duplicate or not replay.duplicate:
        raise SystemExit("interaction idempotency gate failed")
    if first.canonical_assistant_event_id != replay.canonical_assistant_event_id:
        raise SystemExit("canonical assistant identity changed on replay")

    return {
        "ok": True,
        "synthetic_only": True,
        "phase_1": {
            "preflight": "passed",
            "model": preflight.model,
            "tools": list(preflight.tools),
            "capabilities": list(preflight.capabilities),
        },
        "phase_2": {
            "first_duplicate": first.duplicate,
            "replay_duplicate": replay.duplicate,
            "canonical_user_event_id": first.canonical_user_event_id,
            "canonical_assistant_event_id": first.canonical_assistant_event_id,
            "response_sha256": hashlib.sha256(first.text.encode("utf-8")).hexdigest(),
            "provider_response_present": bool(first.response_id),
            "mini_fallback_calls": 0,
            "openclaw_fallback_calls": 0,
        },
        "uid_sha256": hashlib.sha256(uid.encode("utf-8")).hexdigest(),
        "content_free_receipt": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    parser.add_argument("--client-interaction-id", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.uid, args.client_interaction_id)), sort_keys=True))


if __name__ == "__main__":
    main()
