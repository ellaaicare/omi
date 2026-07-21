#!/usr/bin/env python3
"""
Metadata-only inventory for long conversations stranded without a usable summary.

The script never prints transcript text and never mutates Firestore. Use --uid with
--conversation-id for exact incident lookup, or scan discarded records exhaustively
with bounded Firestore pages.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from utils.conversations.long_discarded_inventory import (
    classify_long_discarded_record,
    iter_paginated_documents,
    structured_is_empty,
)

_DATABASE_IMPORT_ERROR: ModuleNotFoundError | None = None
try:
    from google.cloud.firestore_v1 import FieldFilter

    from database._client import db, get_users_uid
    from database.conversations import _prepare_conversation_for_read, conversations_collection
except ModuleNotFoundError as error:
    _DATABASE_IMPORT_ERROR = error


def _transcript_metrics(uid: str, data: Dict[str, Any]) -> tuple[int, int, int]:
    plain = _prepare_conversation_for_read(data, uid) or data
    segments = plain.get("transcript_segments") or []
    texts = [str(segment.get("text") or "") for segment in segments if isinstance(segment, dict)]
    return len(segments), len(" ".join(texts)), sum(len(text.split()) for text in texts)


def _iter_user_ids(uid: str | None) -> Iterable[str]:
    if uid:
        yield uid
        return
    yield from get_users_uid()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _conversation_collection(uid: str):
    return db.collection("users").document(uid).collection(conversations_collection)


def _iter_discarded_documents(uid: str, page_size: int) -> Iterator[Tuple[Any, int]]:
    base_query = _conversation_collection(uid).where(filter=FieldFilter("discarded", "==", True)).order_by("__name__")

    def fetch_page(last_document: Any | None, limit: int):
        query = base_query
        if last_document is not None:
            query = query.start_after(last_document)
        return list(query.limit(limit).stream())

    yield from iter_paginated_documents(fetch_page, page_size)


def _iter_target_documents(uid: str, conversation_id: str | None, page_size: int) -> Iterator[Tuple[Any, int]]:
    if conversation_id:
        document = _conversation_collection(uid).document(conversation_id).get()
        if document.exists:
            yield document, 1
        return
    yield from _iter_discarded_documents(uid, page_size)


def _record_metadata(
    uid: str,
    document: Any,
    data: Dict[str, Any],
    classification: Dict[str, Any],
    segment_count: int,
    transcript_chars: int,
    transcript_words: int,
) -> Dict[str, Any]:
    return {
        "type": "conversation_inventory_record",
        "uid": uid,
        "conversation_id": document.id,
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "created_at": data.get("created_at"),
        "status": classification["status"],
        "discarded": data.get("discarded"),
        "structured_empty": structured_is_empty(data),
        "segment_count": segment_count,
        "transcript_chars": transcript_chars,
        "transcript_words": transcript_words,
        **classification,
    }


def _scan_user(
    *,
    uid: str,
    conversation_id: str | None,
    page_size: int,
    min_transcript_chars: int,
    stale_processing_after: timedelta,
    now: datetime,
) -> Dict[str, Any]:
    scanned = 0
    candidates = 0
    page_count = 0
    reasons: Counter[str] = Counter()
    statuses: Counter[str] = Counter()

    for document, page_number in _iter_target_documents(uid, conversation_id, page_size):
        page_count = max(page_count, page_number)
        scanned += 1
        data = document.to_dict() or {}
        segment_count, transcript_chars, transcript_words = _transcript_metrics(uid, data)
        classification = classify_long_discarded_record(
            data,
            transcript_chars=transcript_chars,
            min_transcript_chars=min_transcript_chars,
            now=now,
            stale_processing_after=stale_processing_after,
        )
        reasons[classification["reason"]] += 1
        statuses[classification["status"]] += 1
        if classification["candidate"]:
            candidates += 1

        if classification["candidate"] or conversation_id:
            print(
                json.dumps(
                    _record_metadata(
                        uid,
                        document,
                        data,
                        classification,
                        segment_count,
                        transcript_chars,
                        transcript_words,
                    ),
                    default=_json_default,
                    sort_keys=True,
                )
            )

    if conversation_id and scanned == 0:
        print(
            json.dumps(
                {
                    "type": "conversation_inventory_record",
                    "uid": uid,
                    "conversation_id": conversation_id,
                    "candidate": False,
                    "reason": "not_found",
                },
                sort_keys=True,
            )
        )
        reasons["not_found"] += 1

    summary = {
        "type": "user_inventory_summary",
        "uid": uid,
        "exact_conversation_id": conversation_id,
        "scanned": scanned,
        "candidates": candidates,
        "pages": page_count,
        "truncated": False,
        "status_counts": dict(sorted(statuses.items())),
        "reason_counts": dict(sorted(reasons.items())),
    }
    print(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", help="Optional user id to scan. Required with --conversation-id.")
    parser.add_argument("--conversation-id", help="Inspect one exact conversation without scanning the collection.")
    parser.add_argument("--min-transcript-chars", type=int, default=25_000)
    parser.add_argument(
        "--page-size",
        "--limit-per-user",
        dest="page_size",
        type=int,
        default=500,
        help="Firestore page size. All pages are scanned; this is not a result limit.",
    )
    parser.add_argument("--stale-processing-hours", type=float, default=6.0)
    args = parser.parse_args()

    if args.conversation_id and not args.uid:
        parser.error("--conversation-id requires --uid")
    if args.page_size <= 0:
        parser.error("--page-size must be greater than zero")
    if args.min_transcript_chars < 0:
        parser.error("--min-transcript-chars must be non-negative")
    if args.stale_processing_hours <= 0:
        parser.error("--stale-processing-hours must be greater than zero")
    if _DATABASE_IMPORT_ERROR is not None:
        parser.error(f"backend dependency unavailable: {_DATABASE_IMPORT_ERROR.name}")

    now = datetime.now(timezone.utc)
    stale_processing_after = timedelta(hours=args.stale_processing_hours)
    total_scanned = 0
    total_candidates = 0
    user_count = 0

    for uid in _iter_user_ids(args.uid):
        user_count += 1
        summary = _scan_user(
            uid=uid,
            conversation_id=args.conversation_id,
            page_size=args.page_size,
            min_transcript_chars=args.min_transcript_chars,
            stale_processing_after=stale_processing_after,
            now=now,
        )
        total_scanned += summary["scanned"]
        total_candidates += summary["candidates"]

    print(
        json.dumps(
            {
                "type": "inventory_summary",
                "users_scanned": user_count,
                "records_scanned": total_scanned,
                "total_candidates": total_candidates,
                "truncated": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
