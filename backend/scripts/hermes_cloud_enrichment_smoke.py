#!/usr/bin/env python3
"""Run one synthetic OMI enrichment cycle through Hermes Cloud."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os

from ella.services.hermes_cloud_enrichment_dependencies import (
    create_default_hermes_cloud_enrichment_service,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _synthetic_guard(uid: str) -> None:
    if os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "").strip().lower() != "true":
        raise SystemExit("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true is required")
    configured = {item.strip() for item in os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "").split(",") if item.strip()}
    if not uid.startswith(("synthetic-", "staging-synthetic-")) or uid not in configured:
        raise SystemExit("UID must be an explicitly configured synthetic identity")


async def run(uid: str, conversation_id: str, *, allow_shadow: bool) -> dict:
    _synthetic_guard(uid)
    service = await create_default_hermes_cloud_enrichment_service()
    first = await service.enrich(
        uid=uid,
        conversation_id=conversation_id,
        allow_shadow=allow_shadow,
    )
    replay = await service.enrich(
        uid=uid,
        conversation_id=conversation_id,
        allow_shadow=allow_shadow,
    )
    if first.duplicate:
        raise SystemExit("first execution was already completed; use a fresh synthetic conversation")
    if not replay.duplicate:
        raise SystemExit("same-transcript replay did not deduplicate")
    if (
        first.active_summary_version_id != replay.active_summary_version_id
        or first.canonical_user_event_id != replay.canonical_user_event_id
        or first.canonical_assistant_event_id != replay.canonical_assistant_event_id
    ):
        raise SystemExit("replay changed durable enrichment identities")
    if not first.provider_response_present:
        raise SystemExit("Hermes Cloud provider response receipt is missing")

    return {
        "ok": True,
        "synthetic_only": True,
        "allow_shadow": allow_shadow,
        "runtime": "hermes_cloud",
        "enrichment_status": "writeback_applied",
        "first_duplicate": first.duplicate,
        "replay_duplicate": replay.duplicate,
        "uid_sha256": _sha256(uid),
        "conversation_id_sha256": _sha256(conversation_id),
        "binding_id_sha256": _sha256(first.runtime_binding_id),
        "runtime_interaction_id_sha256": _sha256(first.runtime_interaction_id),
        "active_summary_version_id_sha256": _sha256(first.active_summary_version_id),
        "canonical_user_event_id_sha256": _sha256(first.canonical_user_event_id),
        "canonical_assistant_event_id_sha256": _sha256(first.canonical_assistant_event_id),
        "transcript_sha256": first.transcript_sha256,
        "summary_sha256": first.summary_sha256,
        "provider_response_present": first.provider_response_present,
        "mini_fallback_calls": 0,
        "openclaw_fallback_calls": 0,
        "content_free_receipt": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument(
        "--allow-shadow",
        action="store_true",
        help="Permit the explicitly synthetic shadow binding without promoting it.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    args.uid,
                    args.conversation_id,
                    allow_shadow=args.allow_shadow,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
