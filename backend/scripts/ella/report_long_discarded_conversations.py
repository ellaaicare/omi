#!/usr/bin/env python3
"""
Dry-run report for long conversations that were marked discarded without a usable summary.

This script prints metadata only. It does not print transcript text and does not mutate
Firestore. Run from backend/ with production credentials configured.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _is_empty_structured(data: Dict[str, Any]) -> bool:
    structured = data.get("structured") or {}
    return not any(str(structured.get(field) or "").strip() for field in ("title", "overview"))


def _transcript_metrics(uid: str, data: Dict[str, Any]) -> tuple[int, int]:
    from database.conversations import _prepare_conversation_for_read

    plain = _prepare_conversation_for_read(data, uid) or data
    segments = plain.get("transcript_segments") or []
    transcript = " ".join(str(segment.get("text") or "") for segment in segments if isinstance(segment, dict))
    return len(segments), len(transcript)


def _iter_user_ids(uid: str | None) -> Iterable[str]:
    if uid:
        yield uid
        return
    from database._client import get_users_uid

    yield from get_users_uid()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", help="Optional user id to scan. Defaults to all users.")
    parser.add_argument("--min-transcript-chars", type=int, default=25_000)
    parser.add_argument("--limit-per-user", type=int, default=500)
    args = parser.parse_args()

    from google.cloud.firestore_v1 import FieldFilter

    from database._client import db
    from database.conversations import conversations_collection

    total_matches = 0
    for uid in _iter_user_ids(args.uid):
        conversations_ref = (
            db.collection("users")
            .document(uid)
            .collection(conversations_collection)
            .where(filter=FieldFilter("discarded", "==", True))
            .where(filter=FieldFilter("status", "==", "completed"))
            .limit(args.limit_per_user)
        )

        for doc in conversations_ref.stream():
            data = doc.to_dict() or {}
            if not _is_empty_structured(data):
                continue

            segment_count, transcript_chars = _transcript_metrics(uid, data)
            if transcript_chars < args.min_transcript_chars:
                continue

            total_matches += 1
            print(
                json.dumps(
                    {
                        "uid": uid,
                        "conversation_id": doc.id,
                        "started_at": data.get("started_at"),
                        "finished_at": data.get("finished_at"),
                        "created_at": data.get("created_at"),
                        "status": data.get("status"),
                        "discarded": data.get("discarded"),
                        "structured_empty": True,
                        "segment_count": segment_count,
                        "transcript_chars": transcript_chars,
                    },
                    default=_json_default,
                    sort_keys=True,
                )
            )

    print(json.dumps({"total_matches": total_matches}, sort_keys=True))


if __name__ == "__main__":
    main()
