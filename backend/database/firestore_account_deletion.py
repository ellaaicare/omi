"""Idempotent recursive Firestore deletion for one authenticated account."""

from __future__ import annotations

from typing import Any


def _delete_collection(
    firestore: Any,
    collection_ref: Any,
    *,
    batch_size: int,
) -> int:
    deleted = 0
    while True:
        docs = list(collection_ref.limit(batch_size).stream())
        if not docs:
            return deleted
        for document in docs:
            for child_collection in document.reference.collections():
                deleted += _delete_collection(
                    firestore,
                    child_collection,
                    batch_size=batch_size,
                )
        batch = firestore.batch()
        for document in docs:
            batch.delete(document.reference)
        batch.commit()
        deleted += len(docs)


def delete_firestore_user_data(
    firestore: Any,
    uid: str,
    *,
    batch_size: int = 450,
) -> dict[str, Any]:
    """Delete a full document tree; missing/partially deleted trees are success."""
    user_ref = firestore.collection("users").document(uid)
    deleted = 0
    for collection_ref in user_ref.collections():
        deleted += _delete_collection(
            firestore,
            collection_ref,
            batch_size=batch_size,
        )
    if user_ref.get().exists:
        user_ref.delete()
        deleted += 1
    return {
        "status": "ok",
        "message": "Account data deleted successfully",
        "documents_deleted": deleted,
    }
