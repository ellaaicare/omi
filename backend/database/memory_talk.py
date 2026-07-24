import copy
import json
import zlib
from datetime import datetime
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from database._client import db
from utils import encryption


def _conversation_ref(uid: str, conversation_id: str):
    return db.collection("users").document(uid).collection("conversations").document(conversation_id)


def _turns_ref(uid: str, conversation_id: str):
    return _conversation_ref(uid, conversation_id).collection("memory_talk_turns")


def _decode_conversation(data: dict[str, Any], uid: str) -> dict[str, Any]:
    value = copy.deepcopy(data)
    segments = value.get("transcript_segments")
    if value.get("data_protection_level") == "enhanced" and isinstance(segments, str):
        try:
            payload = encryption.decrypt(segments, uid)
            if value.get("transcript_segments_compressed"):
                payload = zlib.decompress(bytes.fromhex(payload)).decode("utf-8")
            value["transcript_segments"] = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError, zlib.error):
            value["transcript_segments"] = []
    elif value.get("transcript_segments_compressed") and isinstance(segments, bytes):
        try:
            value["transcript_segments"] = json.loads(zlib.decompress(segments).decode("utf-8"))
        except (json.JSONDecodeError, TypeError, zlib.error):
            value["transcript_segments"] = []
    return value


def get_conversation(uid: str, conversation_id: str) -> dict[str, Any] | None:
    snapshot = _conversation_ref(uid, conversation_id).get()
    data = snapshot.to_dict() if snapshot.exists else None
    return _decode_conversation(data, uid) if data else None


def get_people_by_ids(uid: str, person_ids: list[str]) -> list[dict]:
    people = []
    people_ref = db.collection("users").document(uid).collection("people")
    for index in range(0, len(person_ids), 30):
        query = people_ref.where(filter=FieldFilter("id", "in", person_ids[index : index + 30]))
        people.extend(snapshot.to_dict() or {} for snapshot in query.stream())
    return people


def list_turns(uid: str, conversation_id: str, limit: int, *, newest_first: bool = False) -> list[dict]:
    direction = firestore.Query.DESCENDING if newest_first else firestore.Query.ASCENDING
    snapshots = _turns_ref(uid, conversation_id).order_by("created_at", direction=direction).limit(limit).stream()
    return [snapshot.to_dict() or {} for snapshot in snapshots]


def write_turn(
    *,
    uid: str,
    conversation_id: str,
    document_id: str,
    role: str,
    text: str,
    created_at: datetime,
) -> None:
    _turns_ref(uid, conversation_id).document(document_id).set(
        {
            "id": document_id,
            "role": role,
            "text": text,
            "created_at": created_at,
            "conversation_id": conversation_id,
        },
        merge=True,
    )


def update_discussion_state(
    uid: str,
    conversation_id: str,
    *,
    has_discussion: bool,
    turn_count: int,
    updated_at: datetime,
) -> None:
    ref = _conversation_ref(uid, conversation_id)
    if not ref.get().exists:
        return
    ref.update(
        {
            "memory_talk_state": {
                "has_discussion": has_discussion,
                "turn_count": turn_count,
                "updated_at": updated_at,
            }
        }
    )
