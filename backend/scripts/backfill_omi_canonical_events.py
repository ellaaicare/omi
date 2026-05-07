#!/usr/bin/env python3
"""Backfill enriched OMI conversations into the Ella canonical ledger."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import database.conversations as conversations_db  # noqa: E402
from utils.ella.canonical_omi import build_omi_canonical_event, write_omi_canonical_event  # noqa: E402

DEFAULT_PLATO_UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_enriched_summary(conversation: dict[str, Any]) -> bool:
    versions = conversation.get("summary_versions") or []
    active_id = conversation.get("active_summary_version_id")
    for version in versions:
        if active_id and version.get("id") != active_id:
            continue
        if version.get("source") == "observer" and version.get("kind") in {
            "observer_enriched",
            "corrected_enriched",
        }:
            return True
    state = conversation.get("enrichment_state") or {}
    if state.get("status") == "writeback_applied":
        return True
    structured = conversation.get("structured") or {}
    return bool(str(structured.get("title") or "").strip() and str(structured.get("overview") or "").strip())


def _conversation_label(conversation: dict[str, Any]) -> str:
    structured = conversation.get("structured") or {}
    return str(structured.get("title") or conversation.get("id") or "untitled")


def backfill_uid(
    *,
    uid: str,
    limit: int,
    since: Optional[datetime],
    apply: bool,
    sleep_seconds: float,
) -> dict[str, Any]:
    conversations = conversations_db.get_conversations(
        uid,
        limit=limit,
        offset=0,
        include_discarded=False,
        statuses=["completed"],
        start_date=since,
    )
    results: list[dict[str, Any]] = []
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            results.append({"conversation_id": None, "status": "skipped", "reason": "missing_id"})
            continue
        if not _has_enriched_summary(conversation):
            results.append(
                {
                    "conversation_id": conversation_id,
                    "status": "skipped",
                    "reason": "missing_enriched_summary",
                    "title": _conversation_label(conversation),
                }
            )
            continue

        try:
            event = build_omi_canonical_event(uid, conversation, summary_source="backfill", summary_kind="omi_enriched")
            if apply:
                write_result = write_omi_canonical_event(
                    uid,
                    conversation,
                    summary_source="backfill",
                    summary_kind="omi_enriched",
                )
                status = (
                    "inserted"
                    if write_result.get("inserted")
                    else "updated"
                    if write_result.get("updated")
                    else "duplicate"
                )
                results.append(
                    {
                        "conversation_id": conversation_id,
                        "event_id": event["event_id"],
                        "status": status,
                        "inserted": write_result.get("inserted"),
                        "updated": write_result.get("updated"),
                        "duplicates": write_result.get("duplicates"),
                        "title": _conversation_label(conversation),
                    }
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            else:
                results.append(
                    {
                        "conversation_id": conversation_id,
                        "event_id": event["event_id"],
                        "status": "dry_run",
                        "title": _conversation_label(conversation),
                        "started_at": event["started_at"],
                    }
                )
        except Exception as exc:
            results.append(
                {
                    "conversation_id": conversation_id,
                    "status": "error",
                    "title": _conversation_label(conversation),
                    "error": str(exc),
                }
            )
    return {
        "uid": uid,
        "apply": apply,
        "requested_limit": limit,
        "fetched": len(conversations),
        "results": results,
        "summary": {
            "dry_run": sum(1 for item in results if item["status"] == "dry_run"),
            "inserted": sum(int(item.get("inserted") or 0) for item in results),
            "updated": sum(int(item.get("updated") or 0) for item in results),
            "duplicates": sum(int(item.get("duplicates") or 0) for item in results),
            "skipped": sum(1 for item in results if item["status"] == "skipped"),
            "errors": sum(1 for item in results if item["status"] == "error"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", action="append", dest="uids", help="OMI uid to backfill. Repeatable.")
    parser.add_argument("--limit", type=int, default=100, help="Max completed conversations per uid.")
    parser.add_argument("--since", help="Optional ISO lower bound for Firestore created_at.")
    parser.add_argument("--apply", action="store_true", help="Write events. Default is dry-run.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between apply writes.")
    args = parser.parse_args()

    uids = args.uids or [DEFAULT_PLATO_UID]
    since = _parse_iso(args.since)
    any_errors = False
    for uid in uids:
        result = backfill_uid(uid=uid, limit=args.limit, since=since, apply=args.apply, sleep_seconds=args.sleep)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        any_errors = any_errors or result["summary"]["errors"] > 0
    return 1 if any_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
