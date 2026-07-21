#!/usr/bin/env python3
"""
Scoped repair for a long conversation that was discarded because summary generation failed.

Default mode is dry-run. Use --apply to write. The repair does not call /reprocess
and intentionally avoids integrations, notifications, n8n postprocess hooks, usage,
memory extraction, trend extraction, goal updates, app results, and audio work.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models.conversation import Conversation, ConversationStatus
from utils.conversations.historical_repair import (
    conversation_repair_metadata,
    is_long_discarded_summary_failure,
    structured_has_summary_content,
)

SCRIPT_NAME = "scripts/ella/repair_long_discarded_conversation.py"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _load_conversation(uid: str, conversation_id: str) -> Conversation:
    import database.conversations as conversations_db

    data = conversations_db.get_conversation(uid, conversation_id)
    if not data:
        raise SystemExit(f"Conversation not found: uid={uid} conversation_id={conversation_id}")
    return Conversation(**data)


def _load_people(uid: str, conversation: Conversation) -> list[Any]:
    import database.users as users_db
    from models.other import Person

    person_ids = conversation.get_person_ids()
    if not person_ids:
        return []
    people_data = users_db.get_people_by_ids(uid, list(set(person_ids)))
    return [Person(**person) for person in people_data]


def _fetch_existing_conversation_vector_ids(uid: str, conversation_ids: list[str]) -> list[str]:
    from database.vector_db import fetch_existing_conversation_vector_ids

    return fetch_existing_conversation_vector_ids(uid, conversation_ids)


def _generate_structured(uid: str, language: str, conversation: Conversation, people: list[Any]) -> tuple[Any, bool]:
    from utils.conversations.process_conversation import _get_structured

    return _get_structured(uid, language, conversation, force_process=False, people=people)


def _save_structured_vector(uid: str, conversation: Conversation) -> None:
    from utils.conversations.process_conversation import save_structured_vector

    save_structured_vector(uid, conversation, update_only=False)


def _build_summary_version_update(conversation_data: Dict[str, Any], structured_dict: Dict[str, Any]) -> Dict[str, Any]:
    import database.conversations as conversations_db

    return conversations_db.build_summary_version_update(
        conversation_data,
        next_structured=structured_dict,
        source="historical_repair",
        kind="long_discarded_summary_repair",
        activate=True,
    )


def _update_conversation(uid: str, conversation_id: str, update_payload: Dict[str, Any]) -> None:
    import database.conversations as conversations_db

    conversations_db.update_conversation(uid, conversation_id, update_payload)


def _build_update_payload(conversation: Conversation, structured: Any, *, vector_existed_before: bool) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    structured_dict = structured.model_dump()
    version_update = _build_summary_version_update(conversation.model_dump(), structured_dict)
    return {
        "structured": structured_dict,
        "discarded": False,
        "status": ConversationStatus.completed.value,
        "processing_error": None,
        "processing_error_at": None,
        "historical_repair": {
            "source": SCRIPT_NAME,
            "reason": "long_discarded_empty_structured",
            "repaired_at": now,
            "vector_existed_before": vector_existed_before,
        },
        **version_update,
    }


def repair_conversation(
    *,
    uid: str,
    conversation_id: str,
    language: str,
    min_transcript_chars: int,
    apply: bool,
) -> Dict[str, Any]:
    conversation = _load_conversation(uid, conversation_id)
    before = conversation_repair_metadata(conversation)
    eligible = is_long_discarded_summary_failure(conversation, min_transcript_chars=min_transcript_chars)

    if not eligible:
        return {
            "status": "skipped",
            "reason": "not_long_discarded_empty_summary_candidate",
            "apply": apply,
            "before": before,
        }

    vector_existed_before = conversation_id in _fetch_existing_conversation_vector_ids(uid, [conversation_id])
    people = _load_people(uid, conversation)
    try:
        structured, discarded = _generate_structured(uid, language, conversation, people)
    except Exception as error:
        return {
            "status": "summary_generation_failed",
            "apply": False,
            "uid": uid,
            "conversation_id": conversation_id,
            "before": before,
            "vector": {
                "existed_before": vector_existed_before,
                "upsert_attempted": False,
                "skipped": True,
            },
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
    if discarded:
        return {
            "status": "skipped",
            "reason": "summary_pipeline_still_classified_discarded",
            "apply": apply,
            "before": before,
            "generated": {
                "title_chars": len(structured.title or ""),
                "overview_chars": len(structured.overview or ""),
                "category": structured.category.value if structured.category else None,
            },
        }
    if not structured_has_summary_content(structured.model_dump()):
        return {
            "status": "summary_generation_empty",
            "apply": False,
            "uid": uid,
            "conversation_id": conversation_id,
            "before": before,
            "generated": {
                "title_chars": len(structured.title or ""),
                "overview_chars": len(structured.overview or ""),
                "category": structured.category.value if structured.category else None,
            },
            "vector": {
                "existed_before": vector_existed_before,
                "upsert_attempted": False,
                "skipped": True,
            },
        }

    repaired = conversation.model_copy(deep=True)
    repaired.structured = structured
    repaired.discarded = False
    repaired.status = ConversationStatus.completed
    repaired.processing_error = None
    repaired.processing_error_at = None
    after = conversation_repair_metadata(repaired)

    result = {
        "status": "would_repair" if not apply else "repaired",
        "apply": apply,
        "uid": uid,
        "conversation_id": conversation_id,
        "before": before,
        "after": after,
        "vector": {
            "existed_before": vector_existed_before,
            "upsert_attempted": False,
            "skipped": False,
        },
    }
    provider_override = {
        key: value
        for key, value in {
            "OMI_LLM_PROVIDER_ORDER": os.getenv("OMI_LLM_PROVIDER_ORDER"),
            "OMI_GROQ_MODEL": os.getenv("OMI_GROQ_MODEL"),
            "OMI_GEMINI_MODEL": os.getenv("OMI_GEMINI_MODEL"),
        }.items()
        if value
    }
    if provider_override:
        result["provider_config"] = provider_override

    if not apply:
        return result

    current = _load_conversation(uid, conversation_id)
    current_metadata = conversation_repair_metadata(current)
    if current_metadata != before or not is_long_discarded_summary_failure(
        current, min_transcript_chars=min_transcript_chars
    ):
        result["status"] = "concurrent_change_detected"
        result["apply"] = False
        result["current"] = current_metadata
        result["vector"]["skipped"] = True
        return result

    try:
        _save_structured_vector(uid, repaired)
        result["vector"]["upsert_attempted"] = True
    except Exception as error:
        result["status"] = "vector_upsert_failed"
        result["apply"] = False
        result["vector"]["upsert_attempted"] = True
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        return result

    update_payload = _build_update_payload(current, structured, vector_existed_before=vector_existed_before)
    update_payload["historical_repair"]["vector_upserted"] = True
    try:
        _update_conversation(uid, conversation_id, update_payload)
    except Exception as error:
        result["status"] = "firestore_update_failed_after_vector"
        result["apply"] = False
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        return result

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--min-transcript-chars", type=int, default=25_000)
    parser.add_argument("--apply", action="store_true", help="Write the repair. Without this, dry-run only.")
    parser.add_argument("--provider-order", help="Temporary provider order override, e.g. groq.")
    parser.add_argument("--groq-model", help="Temporary Groq model override, e.g. llama-3.3-70b-versatile.")
    parser.add_argument("--gemini-model", help="Temporary Gemini model override.")
    args = parser.parse_args()

    if args.provider_order:
        os.environ["OMI_LLM_PROVIDER_ORDER"] = args.provider_order
    if args.groq_model:
        os.environ["OMI_GROQ_MODEL"] = args.groq_model
    if args.gemini_model:
        os.environ["OMI_GEMINI_MODEL"] = args.gemini_model

    result = repair_conversation(
        uid=args.uid,
        conversation_id=args.conversation_id,
        language=args.language,
        min_transcript_chars=args.min_transcript_chars,
        apply=args.apply,
    )
    print(json.dumps(result, default=_json_default, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
