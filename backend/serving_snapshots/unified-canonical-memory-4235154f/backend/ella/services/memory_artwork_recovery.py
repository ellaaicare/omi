"""Idempotent recovery bridge for memories waiting on canonical enrichment."""

import asyncio
import hashlib
import json
import uuid
from typing import Any

import database.conversations as conversations_db


def _recovery_request_id(uid: str, memory_id: str, conversation: dict[str, Any]) -> str:
    transcript_segments = conversation.get("transcript_segments") or []
    transcript_sha256 = hashlib.sha256(
        json.dumps(
            transcript_segments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    active_version = str(conversation.get("active_summary_version_id") or "legacy")
    owner_digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"ella-memory-artwork-enrichment:{owner_digest}:{memory_id}:{active_version}:{transcript_sha256}",
        )
    )


async def claim_memory_artwork_enrichment_recovery(uid: str, memory_id: str) -> dict[str, Any]:
    """Claim the existing summary-recovery workflow without duplicating work."""

    conversation = await asyncio.to_thread(conversations_db.get_conversation, uid, memory_id)
    if conversation is None:
        return {"outcome": "not_found"}
    request_id = _recovery_request_id(uid, memory_id, conversation)
    claim = await asyncio.to_thread(
        conversations_db.claim_conversation_processing_retry,
        uid,
        memory_id,
        request_id,
    )
    return {**claim, "request_id": request_id}
