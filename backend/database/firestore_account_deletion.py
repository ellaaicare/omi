"""Idempotent recursive Firestore deletion for one authenticated account."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from google.cloud.firestore_v1 import FieldFilter


@dataclass(frozen=True)
class OwnedCollection:
    name: str
    owner_field: str
    api_key_cache: str | None = None


OWNED_TOP_LEVEL_COLLECTIONS = (
    OwnedCollection("analytics", "uid"),
    OwnedCollection("dev_api_keys", "user_id", "developer"),
    OwnedCollection("mcp_api_keys", "user_id", "mcp"),
    OwnedCollection("tasks", "user_uid"),
    OwnedCollection("import_jobs", "uid"),
    OwnedCollection("ella_hermes_cloud_enrichment_outbox", "uid"),
    OwnedCollection("plugins_data", "uid"),
    OwnedCollection("mcp_identity_grants", "profile_uid"),
)
OWNED_COLLECTION_GROUPS = (
    OwnedCollection("reviews", "uid"),
    OwnedCollection("usage_history", "uid"),
)
DIRECT_UID_DOCUMENT_COLLECTIONS = ("testers",)
OWNERSHIP_PROBE_FIELDS = ("uid", "user_id", "user_uid", "profile_uid")


class FirestoreAccountDeletionIncomplete(RuntimeError):
    """The bounded purge could not prove that Firestore authority is absent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _owned_top_level_collections() -> tuple[OwnedCollection, ...]:
    configured_grants = os.getenv("ELLA_MCP_IDENTITY_GRANTS_COLLECTION", "mcp_identity_grants").strip()
    if not configured_grants or configured_grants == "mcp_identity_grants":
        return OWNED_TOP_LEVEL_COLLECTIONS
    return (*OWNED_TOP_LEVEL_COLLECTIONS, OwnedCollection(configured_grants, "profile_uid"))


def _snapshot_data(document: Any) -> dict[str, Any]:
    data = document.to_dict()
    return dict(data or {})


def _invalidate_api_key_cache(cache_authority: Any, cache_kind: str, hashed_key: Any) -> None:
    if cache_authority is None:
        raise FirestoreAccountDeletionIncomplete("account_deletion_api_key_cache_unavailable")
    if not isinstance(hashed_key, str) or not hashed_key:
        raise FirestoreAccountDeletionIncomplete("account_deletion_api_key_cache_identity_missing")
    if cache_kind == "developer":
        absent = cache_authority.invalidate_cached_dev_api_key(hashed_key)
    elif cache_kind == "mcp":
        absent = cache_authority.invalidate_cached_mcp_api_key(hashed_key)
    else:  # pragma: no cover - inventory constants are closed above
        raise FirestoreAccountDeletionIncomplete("account_deletion_api_key_cache_inventory_invalid")
    if absent is not True:
        raise FirestoreAccountDeletionIncomplete("account_deletion_api_key_cache_retained")


def _delete_collection(
    firestore: Any,
    collection_ref: Any,
    *,
    batch_size: int,
    api_key_cache: str | None = None,
    cache_authority: Any = None,
) -> int:
    deleted = 0
    while True:
        docs = list(collection_ref.limit(batch_size).stream())
        if not docs:
            return deleted
        cache_hashes: list[str] = []
        for document in docs:
            if api_key_cache is not None:
                hashed_key = _snapshot_data(document).get("hashed_key")
                _invalidate_api_key_cache(cache_authority, api_key_cache, hashed_key)
                cache_hashes.append(hashed_key)
            for child_collection in document.reference.collections():
                deleted += _delete_collection(
                    firestore,
                    child_collection,
                    batch_size=batch_size,
                    cache_authority=cache_authority,
                )
        batch = firestore.batch()
        for document in docs:
            batch.delete(document.reference)
        batch.commit()
        deleted += len(docs)
        for hashed_key in cache_hashes:
            _invalidate_api_key_cache(cache_authority, api_key_cache, hashed_key)
        cache_hashes.clear()
        del docs


def _query_has_documents(query: Any) -> bool:
    return bool(list(query.limit(1).stream()))


def _verify_user_tree_absent(user_ref: Any) -> None:
    if user_ref.get().exists:
        raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_user_retained")
    for collection_ref in user_ref.collections():
        if _query_has_documents(collection_ref):
            raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_user_descendant_retained")


def _verify_known_inventory_absent(
    firestore: Any,
    uid: str,
    *,
    owned_collections: tuple[OwnedCollection, ...],
) -> None:
    for owned in owned_collections:
        query = firestore.collection(owned.name).where(filter=FieldFilter(owned.owner_field, "==", uid))
        if _query_has_documents(query):
            raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_owned_document_retained")
    for owned in OWNED_COLLECTION_GROUPS:
        query = firestore.collection_group(owned.name).where(filter=FieldFilter(owned.owner_field, "==", uid))
        if _query_has_documents(query):
            raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_owned_descendant_retained")
    for collection_name in DIRECT_UID_DOCUMENT_COLLECTIONS:
        if firestore.collection(collection_name).document(uid).get().exists:
            raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_direct_document_retained")


def _verify_no_uninventoried_top_level_ownership(firestore: Any, uid: str) -> None:
    """Fail closed when a top-level collection retains a recognized UID field."""
    for collection_ref in firestore.collections():
        if collection_ref.id == "users":
            continue
        for owner_field in OWNERSHIP_PROBE_FIELDS:
            query = collection_ref.where(filter=FieldFilter(owner_field, "==", uid))
            if _query_has_documents(query):
                raise FirestoreAccountDeletionIncomplete("account_deletion_firestore_inventory_incomplete")


def delete_firestore_user_data(
    firestore: Any,
    uid: str,
    *,
    batch_size: int = 450,
    cache_authority: Any = None,
) -> dict[str, Any]:
    """Delete and verify the complete known Firestore ownership inventory."""
    if batch_size < 1 or batch_size > 450:
        raise ValueError("batch_size must be between 1 and 450")
    deleted = 0
    owned_collections = _owned_top_level_collections()
    for owned in owned_collections:
        deleted += _delete_collection(
            firestore,
            firestore.collection(owned.name).where(filter=FieldFilter(owned.owner_field, "==", uid)),
            batch_size=batch_size,
            api_key_cache=owned.api_key_cache,
            cache_authority=cache_authority,
        )
    for owned in OWNED_COLLECTION_GROUPS:
        deleted += _delete_collection(
            firestore,
            firestore.collection_group(owned.name).where(filter=FieldFilter(owned.owner_field, "==", uid)),
            batch_size=batch_size,
            cache_authority=cache_authority,
        )
    for collection_name in DIRECT_UID_DOCUMENT_COLLECTIONS:
        document_ref = firestore.collection(collection_name).document(uid)
        for child_collection in document_ref.collections():
            deleted += _delete_collection(
                firestore,
                child_collection,
                batch_size=batch_size,
                cache_authority=cache_authority,
            )
        if document_ref.get().exists:
            document_ref.delete()
            deleted += 1
    user_ref = firestore.collection("users").document(uid)
    for collection_ref in user_ref.collections():
        deleted += _delete_collection(
            firestore,
            collection_ref,
            batch_size=batch_size,
            cache_authority=cache_authority,
        )
    if user_ref.get().exists:
        user_ref.delete()
        deleted += 1
    _verify_user_tree_absent(user_ref)
    _verify_known_inventory_absent(firestore, uid, owned_collections=owned_collections)
    _verify_no_uninventoried_top_level_ownership(firestore, uid)
    return {
        "status": "ok",
        "message": "Account data deleted successfully",
        "documents_deleted": deleted,
    }
