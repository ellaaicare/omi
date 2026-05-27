#!/usr/bin/env python3
"""Backfill OMI conversation vectors into Pinecone.

The script is intentionally idempotent: every upsert uses the stable
`{uid}-{conversation_id}` Pinecone id in namespace `ns1`, so reruns replace the
same vector instead of creating duplicates.

Examples:
    python backend/scripts/backfill_conversation_vectors.py --uid USER_ID --start-date 2026-05-01T00:00:00Z --dry-run
    python backend/scripts/backfill_conversation_vectors.py --uid USER_ID --start-date 2026-05-01T00:00:00Z --only-missing --min-coverage 0.95
    python backend/scripts/backfill_conversation_vectors.py --uid USER_ID --coverage-only --start-date 2026-05-01T00:00:00Z
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger("backfill_conversation_vectors")
firestore = None
FieldFilter = None
vector_db = None
_decrypt_conversation_data = None
generate_embedding = None


def _load_backend_dependencies() -> None:
    global vector_db, _decrypt_conversation_data, generate_embedding
    from database import vector_db as vector_db_module
    from database.conversations import _decrypt_conversation_data as decrypt_conversation_data
    from utils.llm.clients import generate_embedding as generate_embedding_func

    vector_db = vector_db_module
    _decrypt_conversation_data = decrypt_conversation_data
    generate_embedding = generate_embedding_func


def _init_firebase() -> Any:
    global firestore, FieldFilter
    import firebase_admin
    from firebase_admin import credentials, firestore as firestore_module
    from google.cloud.firestore_v1 import FieldFilter as field_filter

    firestore = firestore_module
    FieldFilter = field_filter
    try:
        firebase_admin.initialize_app(credentials.ApplicationDefault())
    except ValueError:
        pass
    return firestore_module.client()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _created_at_epoch(value: Any) -> int:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    if value:
        try:
            return int(_parse_datetime(str(value)).timestamp())
        except Exception:
            return int(time.time())
    return int(time.time())


def _iter_user_ids(db: Any, limit: int | None = None) -> list[str]:
    query = db.collection("users")
    if limit:
        query = query.limit(limit)
    return [doc.id for doc in query.stream()]


def _fetch_conversations(
    db: Any,
    uid: str,
    *,
    start_date: datetime | None,
    end_date: datetime | None,
    include_discarded: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    query = db.collection("users").document(uid).collection("conversations")
    if not include_discarded:
        query = query.where(filter=FieldFilter("discarded", "==", False))
    if start_date:
        query = query.where(filter=FieldFilter("created_at", ">=", start_date))
    if end_date:
        query = query.where(filter=FieldFilter("created_at", "<=", end_date))
    query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
    if limit:
        query = query.limit(limit)

    conversations = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        data["id"] = data.get("id") or doc.id
        conversations.append(data)
    return conversations


def _conversation_embedding_text(uid: str, conversation: dict[str, Any], max_chars: int) -> str:
    try:
        conversation = _decrypt_conversation_data(conversation, uid)
    except Exception as exc:
        logger.warning(
            "conversation_decrypt_failed uid=%s conversation_id=%s error=%s",
            uid,
            conversation.get("id"),
            exc,
        )

    structured = conversation.get("structured") or {}
    parts = [
        structured.get("title", ""),
        structured.get("overview", ""),
        structured.get("category", ""),
    ]
    for segment in conversation.get("transcript_segments") or []:
        if isinstance(segment, dict):
            text = segment.get("text") or segment.get("transcript") or ""
        else:
            text = getattr(segment, "text", "") or getattr(segment, "transcript", "")
        if text:
            parts.append(str(text))
        if sum(len(part) for part in parts) >= max_chars:
            break
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())[:max_chars]


def _metadata(conversation: dict[str, Any], content: str) -> dict[str, Any]:
    structured = conversation.get("structured") or {}
    created_at = conversation.get("created_at")
    return {
        "memory_id": conversation.get("id", ""),
        "conversation_id": conversation.get("id", ""),
        "created_at": _created_at_epoch(created_at),
        "source": conversation.get("source", ""),
        "status": conversation.get("status", ""),
        "category": structured.get("category", ""),
        "title": str(structured.get("title") or "")[:200],
        "vector_schema": "conversation_summary_transcript_v1",
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "backfilled_at": int(time.time()),
    }


def _existing_ids(uid: str, conversation_ids: list[str], chunk_size: int = 100) -> set[str]:
    existing = set()
    for i in range(0, len(conversation_ids), chunk_size):
        chunk = conversation_ids[i : i + chunk_size]
        existing.update(vector_db.fetch_existing_conversation_vector_ids(uid, chunk))
    return existing


def _process_user(db: Any, uid: str, args: argparse.Namespace) -> dict[str, Any]:
    conversations = _fetch_conversations(
        db,
        uid,
        start_date=args.start_date,
        end_date=args.end_date,
        include_discarded=args.include_discarded,
        limit=args.limit,
    )
    conversation_ids = [str(c.get("id")) for c in conversations if c.get("id")]
    existing = _existing_ids(uid, conversation_ids) if conversation_ids else set()
    missing = [conversation_id for conversation_id in conversation_ids if conversation_id not in existing]
    before_coverage = (len(existing) / len(conversation_ids)) if conversation_ids else 1.0

    logger.info(
        "conversation_vector_coverage uid=%s total=%s existing=%s missing=%s coverage=%.4f namespace=%s",
        uid,
        len(conversation_ids),
        len(existing),
        len(missing),
        before_coverage,
        vector_db.CONVERSATIONS_NAMESPACE,
    )

    stats = {
        "uid": uid,
        "total": len(conversation_ids),
        "existing": len(existing),
        "missing": len(missing),
        "processed": 0,
        "skipped_existing": 0,
        "skipped_empty": 0,
        "errors": 0,
        "coverage_before": before_coverage,
        "coverage_after": before_coverage,
    }
    if args.coverage_only:
        return stats

    existing_after = set(existing)
    for index, conversation in enumerate(conversations, start=1):
        conversation_id = str(conversation.get("id") or "")
        if not conversation_id:
            continue
        if args.only_missing and conversation_id in existing:
            stats["skipped_existing"] += 1
            continue

        content = _conversation_embedding_text(uid, conversation, args.max_chars)
        if not content.strip():
            stats["skipped_empty"] += 1
            logger.warning("conversation_vector_skipped_empty uid=%s conversation_id=%s", uid, conversation_id)
            continue

        if args.dry_run:
            stats["processed"] += 1
            existing_after.add(conversation_id)
            continue

        try:
            vector = generate_embedding(content)
            vector_db.upsert_conversation_vector(uid, conversation_id, vector, _metadata(conversation, content))
            stats["processed"] += 1
            existing_after.add(conversation_id)
        except Exception as exc:
            stats["errors"] += 1
            logger.exception(
                "conversation_vector_backfill_failed uid=%s conversation_id=%s error=%s",
                uid,
                conversation_id,
                exc,
            )

        if index % args.batch_size == 0:
            logger.info("conversation_vector_backfill_progress uid=%s processed=%s/%s", uid, index, len(conversations))
            if args.sleep_seconds:
                time.sleep(args.sleep_seconds)

    stats["coverage_after"] = (len(existing_after) / len(conversation_ids)) if conversation_ids else 1.0
    if args.min_coverage and stats["coverage_after"] < args.min_coverage:
        logger.warning(
            "conversation_vector_coverage_below_threshold uid=%s coverage=%.4f threshold=%.4f",
            uid,
            stats["coverage_after"],
            args.min_coverage,
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill OMI conversation vectors to Pinecone")
    parser.add_argument("--uid", help="Process a single OMI user id")
    parser.add_argument("--all-users", action="store_true", help="Process all users")
    parser.add_argument("--user-limit", type=int, help="Limit users when --all-users is set")
    parser.add_argument("--start-date", type=_parse_datetime, help="Inclusive ISO start datetime")
    parser.add_argument("--end-date", type=_parse_datetime, help="Inclusive ISO end datetime")
    parser.add_argument("--limit", type=int, help="Limit conversations per user")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--include-discarded", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-coverage", type=float, default=0.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s"
    )

    if not args.uid and not args.all_users:
        parser.error("Provide --uid or --all-users")
    if args.uid and args.all_users:
        parser.error("Use either --uid or --all-users, not both")

    _load_backend_dependencies()
    db = _init_firebase()
    user_ids = [args.uid] if args.uid else _iter_user_ids(db, args.user_limit)

    all_stats = []
    for uid in user_ids:
        all_stats.append(_process_user(db, uid, args))

    total = sum(item["total"] for item in all_stats)
    existing = sum(item["existing"] for item in all_stats)
    processed = sum(item["processed"] for item in all_stats)
    errors = sum(item["errors"] for item in all_stats)
    coverage_after = sum(item["coverage_after"] * item["total"] for item in all_stats) / total if total else 1.0
    logger.info(
        "conversation_vector_backfill_summary users=%s total=%s existing=%s processed=%s errors=%s coverage_after=%.4f dry_run=%s coverage_only=%s",
        len(all_stats),
        total,
        existing,
        processed,
        errors,
        coverage_after,
        args.dry_run,
        args.coverage_only,
    )
    if args.min_coverage and coverage_after < args.min_coverage:
        return 2
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
