"""Owner-scoped Firestore inventory for private Ella dream media."""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from google.cloud import firestore

DREAMS_COLLECTION = "dreams"
RECONCILIATION_COLLECTION = "ella_dream_media_reconciliation"
DREAM_MEDIA_DELETION_PENDING_FIELD = "dream_media_deletion_pending"
USER_DELETION_FIELDS = (
    DREAM_MEDIA_DELETION_PENDING_FIELD,
    "memory_artwork_deletion_pending",
    "account_deletion_pending",
    "deletion_pending",
)


class DreamMediaRepositoryError(RuntimeError):
    """A content-free persistence failure."""


class DreamMediaRepository(Protocol):
    def get_user(self, uid: str) -> Optional[dict[str, Any]]: ...

    def get_dream(self, uid: str, dream_id: str) -> Optional[dict[str, Any]]: ...

    def list_dreams(self, uid: str) -> list[dict[str, Any]]: ...

    def get_source(self, uid: str, source_id: str) -> Optional[dict[str, Any]]: ...

    def begin_account_delete(self, uid: str, deleted_at: datetime) -> bool: ...

    def reserve_upload(
        self,
        *,
        uid: str,
        dream_id: str,
        dream_key: str,
        metadata: dict[str, Any],
        media_entry: dict[str, Any],
    ) -> dict[str, Any]: ...

    def commit_upload(
        self,
        *,
        uid: str,
        dream_id: str,
        asset_key: str,
        object_key: str,
        byte_size: int,
        sha256: str,
        committed_at: datetime,
    ) -> dict[str, Any]: ...

    def begin_delete(self, uid: str, dream_id: str, deleted_at: datetime) -> Optional[dict[str, Any]]: ...

    def finish_delete(self, uid: str, dream_id: str, deleted_at: datetime) -> None: ...

    def find_dreams_by_source(self, uid: str, source_id: str) -> list[dict[str, Any]]: ...

    def remove_media_entries(self, uid: str, dream_id: str, object_keys: set[str], updated_at: datetime) -> None: ...

    def all_dreams(self) -> list[tuple[str, dict[str, Any]]]: ...

    def record_reconciliation(self, receipt: dict[str, Any]) -> None: ...

    def latest_reconciliation(self, pepper_version: int) -> Optional[dict[str, Any]]: ...


def user_deletion_pending(user: Optional[dict[str, Any]]) -> bool:
    return user is None or any(bool(user.get(field)) for field in USER_DELETION_FIELDS)


def source_is_sensitive(source: dict[str, Any]) -> bool:
    assessment = source.get("internal_assessment") or {}
    signal = source.get("ella_signal") or {}
    tags = {str(value).strip().lower().replace("-", "_") for value in (source.get("ella_tags") or [])}
    risk = str(assessment.get("risk_level") or "").strip().lower() if isinstance(assessment, dict) else ""
    guardian_relevant = bool(signal.get("guardian_relevant")) if isinstance(signal, dict) else False
    return bool(
        tags & {"caregiver_private", "safety", "distress", "emergency", "self_harm"}
        or guardian_relevant
        or risk in {"medium", "high", "critical"}
    )


def source_is_excluded(source: Optional[dict[str, Any]]) -> bool:
    return bool(not source or source.get("deletion_pending") or source.get("discarded") or source_is_sensitive(source))


def dream_is_tombstoned(dream: Optional[dict[str, Any]]) -> bool:
    return bool(not dream or dream.get("deletion_pending") or dream.get("deleted_at") or dream.get("tombstoned"))


def dream_metadata_conflicts(existing: dict[str, Any], requested: dict[str, Any]) -> bool:
    """Keep one dream's authority metadata immutable while assets are appended."""
    if not existing.get("media"):
        return False
    scalar_fields = ("title", "narrative", "created_at")
    if any(existing.get(field) != requested.get(field) for field in scalar_fields):
        return True
    if list(existing.get("captions") or []) != list(requested.get("captions") or []):
        return True
    return set(existing.get("source_memory_ids") or []) != set(requested.get("source_memory_ids") or [])


class FirestoreDreamMediaRepository:
    def __init__(self, client=None):
        if client is None:
            from database._client import db as default_db

            client = default_db
        self.db = client

    def _user_ref(self, uid: str):
        return self.db.collection("users").document(uid)

    def _dream_ref(self, uid: str, dream_id: str):
        return self._user_ref(uid).collection(DREAMS_COLLECTION).document(dream_id)

    @staticmethod
    def _snapshot(snapshot, *, document_id: str = "") -> Optional[dict[str, Any]]:
        if not snapshot.exists:
            return None
        payload = snapshot.to_dict() or {}
        payload["dream_id"] = document_id or snapshot.id
        return payload

    def get_user(self, uid: str) -> Optional[dict[str, Any]]:
        snapshot = self._user_ref(uid).get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_dream(self, uid: str, dream_id: str) -> Optional[dict[str, Any]]:
        return self._snapshot(self._dream_ref(uid, dream_id).get(), document_id=dream_id)

    def list_dreams(self, uid: str) -> list[dict[str, Any]]:
        dreams = [self._snapshot(snapshot) for snapshot in self._user_ref(uid).collection(DREAMS_COLLECTION).stream()]
        return [dream for dream in dreams if dream is not None]

    def get_source(self, uid: str, source_id: str) -> Optional[dict[str, Any]]:
        user_ref = self._user_ref(uid)
        for collection_name in ("conversations", "memories"):
            snapshot = user_ref.collection(collection_name).document(source_id).get()
            if snapshot.exists:
                payload = snapshot.to_dict() or {}
                payload["id"] = source_id
                return payload
        return None

    def begin_account_delete(self, uid: str, deleted_at: datetime) -> bool:
        transaction = self.db.transaction()
        user_ref = self._user_ref(uid)

        @firestore.transactional
        def begin(txn):
            snapshot = user_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            user = snapshot.to_dict() or {}
            if user.get(DREAM_MEDIA_DELETION_PENDING_FIELD):
                return True
            txn.set(
                user_ref,
                {
                    DREAM_MEDIA_DELETION_PENDING_FIELD: True,
                    "dream_media_deletion_started_at": deleted_at,
                },
                merge=True,
            )
            return True

        return begin(transaction)

    def reserve_upload(
        self,
        *,
        uid: str,
        dream_id: str,
        dream_key: str,
        metadata: dict[str, Any],
        media_entry: dict[str, Any],
    ) -> dict[str, Any]:
        transaction = self.db.transaction()
        user_ref = self._user_ref(uid)
        dream_ref = self._dream_ref(uid, dream_id)

        @firestore.transactional
        def reserve(txn):
            user_snapshot = user_ref.get(transaction=txn)
            user = user_snapshot.to_dict() if user_snapshot.exists else None
            if user_deletion_pending(user):
                raise DreamMediaRepositoryError("dream_media_deletion_pending")
            dream_snapshot = dream_ref.get(transaction=txn)
            dream = dream_snapshot.to_dict() if dream_snapshot.exists else {}
            if dream_is_tombstoned(dream) and dream_snapshot.exists:
                raise DreamMediaRepositoryError("dream_media_not_found")
            if dream.get("dream_key") and dream.get("dream_key") != dream_key:
                raise DreamMediaRepositoryError("dream_media_dream_key_conflict")
            for existing in dream.get("media") or []:
                if existing.get("request_id") == media_entry["request_id"]:
                    return copy.deepcopy(existing)
            if dream_metadata_conflicts(dream, metadata):
                raise DreamMediaRepositoryError("dream_media_metadata_conflict")
            next_dream = {
                **({} if dream_snapshot.exists else metadata),
                "dream_key": dream.get("dream_key") or dream_key,
                "media": [*(dream.get("media") or []), media_entry],
                "updated_at": media_entry["created_at"],
                "tombstoned": False,
                "deletion_pending": False,
            }
            if not dream_snapshot.exists:
                next_dream["created_at"] = metadata.get("created_at") or media_entry["created_at"]
            txn.set(dream_ref, next_dream, merge=True)
            return copy.deepcopy(media_entry)

        return reserve(transaction)

    def commit_upload(
        self,
        *,
        uid: str,
        dream_id: str,
        asset_key: str,
        object_key: str,
        byte_size: int,
        sha256: str,
        committed_at: datetime,
    ) -> dict[str, Any]:
        transaction = self.db.transaction()
        user_ref = self._user_ref(uid)
        dream_ref = self._dream_ref(uid, dream_id)

        @firestore.transactional
        def commit(txn):
            user_snapshot = user_ref.get(transaction=txn)
            user = user_snapshot.to_dict() if user_snapshot.exists else None
            dream_snapshot = dream_ref.get(transaction=txn)
            dream = dream_snapshot.to_dict() if dream_snapshot.exists else None
            if user_deletion_pending(user) or dream_is_tombstoned(dream):
                raise DreamMediaRepositoryError("dream_media_deletion_pending")
            assert dream is not None
            for source_id in dream.get("source_memory_ids") or []:
                source = None
                for collection_name in ("conversations", "memories"):
                    source_snapshot = user_ref.collection(collection_name).document(source_id).get(transaction=txn)
                    if source_snapshot.exists:
                        source = source_snapshot.to_dict() or {}
                        break
                if source_is_excluded(source):
                    raise DreamMediaRepositoryError("dream_media_source_excluded")
            media = copy.deepcopy(dream.get("media") or [])
            committed = None
            for entry in media:
                if entry.get("asset_key") != asset_key:
                    continue
                if entry.get("object_key") != object_key:
                    raise DreamMediaRepositoryError("dream_media_inventory_conflict")
                entry.update(
                    {
                        "state": "committed",
                        "bytes": byte_size,
                        "sha256": sha256,
                        "committed_at": committed_at,
                    }
                )
                committed = copy.deepcopy(entry)
                break
            if committed is None:
                raise DreamMediaRepositoryError("dream_media_inventory_missing")
            txn.update(dream_ref, {"media": media, "updated_at": committed_at})
            return committed

        return commit(transaction)

    def begin_delete(self, uid: str, dream_id: str, deleted_at: datetime) -> Optional[dict[str, Any]]:
        transaction = self.db.transaction()
        dream_ref = self._dream_ref(uid, dream_id)

        @firestore.transactional
        def begin(txn):
            snapshot = dream_ref.get(transaction=txn)
            dream = self._snapshot(snapshot, document_id=dream_id)
            if dream is None or dream.get("deleted_at") or dream.get("tombstoned"):
                return None
            txn.update(dream_ref, {"deletion_pending": True, "updated_at": deleted_at})
            dream["deletion_pending"] = True
            return dream

        return begin(transaction)

    def finish_delete(self, uid: str, dream_id: str, deleted_at: datetime) -> None:
        self._dream_ref(uid, dream_id).set(
            {
                "media": [],
                "deletion_pending": False,
                "deleted_at": deleted_at,
                "tombstoned": True,
                "updated_at": deleted_at,
            },
            merge=True,
        )

    def find_dreams_by_source(self, uid: str, source_id: str) -> list[dict[str, Any]]:
        query = (
            self._user_ref(uid).collection(DREAMS_COLLECTION).where("source_memory_ids", "array_contains", source_id)
        )
        return [dream for snapshot in query.stream() if (dream := self._snapshot(snapshot)) is not None]

    def remove_media_entries(self, uid: str, dream_id: str, object_keys: set[str], updated_at: datetime) -> None:
        transaction = self.db.transaction()
        dream_ref = self._dream_ref(uid, dream_id)

        @firestore.transactional
        def remove(txn):
            snapshot = dream_ref.get(transaction=txn)
            if not snapshot.exists:
                return
            dream = snapshot.to_dict() or {}
            media = [entry for entry in (dream.get("media") or []) if entry.get("object_key") not in object_keys]
            txn.update(dream_ref, {"media": media, "updated_at": updated_at})

        remove(transaction)

    def all_dreams(self) -> list[tuple[str, dict[str, Any]]]:
        results: list[tuple[str, dict[str, Any]]] = []
        for snapshot in self.db.collection_group(DREAMS_COLLECTION).stream():
            dream = self._snapshot(snapshot)
            parent = snapshot.reference.parent.parent
            if dream is not None and parent is not None:
                results.append((str(parent.id), dream))
        return results

    def record_reconciliation(self, receipt: dict[str, Any]) -> None:
        document_id = f"{receipt['run_id']}-p{int(receipt['pepper_version'])}"
        self.db.collection(RECONCILIATION_COLLECTION).document(document_id).set(receipt)

    def latest_reconciliation(self, pepper_version: int) -> Optional[dict[str, Any]]:
        receipts = [
            snapshot.to_dict() or {}
            for snapshot in self.db.collection(RECONCILIATION_COLLECTION)
            .where("pepper_version", "==", pepper_version)
            .stream()
        ]
        successful = [receipt for receipt in receipts if receipt.get("status") == "completed"]

        def completed_timestamp(receipt: dict[str, Any]) -> float:
            completed_at = receipt.get("completed_at")
            if not isinstance(completed_at, datetime):
                return 0
            normalized = completed_at if completed_at.tzinfo is not None else completed_at.replace(tzinfo=timezone.utc)
            return normalized.timestamp()

        return max(successful, key=completed_timestamp, default=None)


class InMemoryDreamMediaRepository:
    """Thread-safe deterministic repository used by contract tests."""

    def __init__(self):
        self.users: dict[str, dict[str, Any]] = {}
        self.dreams: dict[tuple[str, str], dict[str, Any]] = {}
        self.sources: dict[tuple[str, str], dict[str, Any]] = {}
        self.reconciliations: list[dict[str, Any]] = []
        self.fail_commit = False
        self._lock = threading.RLock()

    def get_user(self, uid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self.users.get(uid)
            return copy.deepcopy(value) if value is not None else None

    def get_dream(self, uid: str, dream_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self.dreams.get((uid, dream_id))
            return copy.deepcopy(value) if value is not None else None

    def list_dreams(self, uid: str) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(value) for (owner, _), value in self.dreams.items() if owner == uid]

    def get_source(self, uid: str, source_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self.sources.get((uid, source_id))
            return copy.deepcopy(value) if value is not None else None

    def begin_account_delete(self, uid: str, deleted_at: datetime) -> bool:
        with self._lock:
            user = self.users.get(uid)
            if user is None:
                return False
            user[DREAM_MEDIA_DELETION_PENDING_FIELD] = True
            user.setdefault("dream_media_deletion_started_at", deleted_at)
            return True

    def reserve_upload(self, *, uid, dream_id, dream_key, metadata, media_entry):
        with self._lock:
            if user_deletion_pending(self.users.get(uid)):
                raise DreamMediaRepositoryError("dream_media_deletion_pending")
            key = (uid, dream_id)
            dream = self.dreams.get(key, {})
            if dream_is_tombstoned(dream) and key in self.dreams:
                raise DreamMediaRepositoryError("dream_media_not_found")
            if dream.get("dream_key") and dream.get("dream_key") != dream_key:
                raise DreamMediaRepositoryError("dream_media_dream_key_conflict")
            for existing in dream.get("media") or []:
                if existing.get("request_id") == media_entry["request_id"]:
                    return copy.deepcopy(existing)
            if dream_metadata_conflicts(dream, metadata):
                raise DreamMediaRepositoryError("dream_media_metadata_conflict")
            merged = {
                **dream,
                **({} if key in self.dreams else copy.deepcopy(metadata)),
                "dream_id": dream_id,
                "dream_key": dream.get("dream_key") or dream_key,
                "media": [*(dream.get("media") or []), copy.deepcopy(media_entry)],
                "updated_at": media_entry["created_at"],
                "tombstoned": False,
                "deletion_pending": False,
            }
            merged.setdefault("created_at", metadata.get("created_at") or media_entry["created_at"])
            self.dreams[key] = merged
            return copy.deepcopy(media_entry)

    def commit_upload(self, *, uid, dream_id, asset_key, object_key, byte_size, sha256, committed_at):
        with self._lock:
            if self.fail_commit:
                raise DreamMediaRepositoryError("dream_media_commit_failed")
            dream = self.dreams.get((uid, dream_id))
            if user_deletion_pending(self.users.get(uid)) or dream_is_tombstoned(dream):
                raise DreamMediaRepositoryError("dream_media_deletion_pending")
            assert dream is not None
            for source_id in dream.get("source_memory_ids") or []:
                if source_is_excluded(self.sources.get((uid, source_id))):
                    raise DreamMediaRepositoryError("dream_media_source_excluded")
            for entry in dream.get("media") or []:
                if entry.get("asset_key") == asset_key and entry.get("object_key") == object_key:
                    entry.update(
                        {
                            "state": "committed",
                            "bytes": byte_size,
                            "sha256": sha256,
                            "committed_at": committed_at,
                        }
                    )
                    dream["updated_at"] = committed_at
                    return copy.deepcopy(entry)
            raise DreamMediaRepositoryError("dream_media_inventory_missing")

    def begin_delete(self, uid, dream_id, deleted_at):
        with self._lock:
            dream = self.dreams.get((uid, dream_id))
            if dream is None or dream.get("deleted_at") or dream.get("tombstoned"):
                return None
            dream["deletion_pending"] = True
            dream["updated_at"] = deleted_at
            return copy.deepcopy(dream)

    def finish_delete(self, uid, dream_id, deleted_at):
        with self._lock:
            dream = self.dreams[(uid, dream_id)]
            dream.update(
                {
                    "media": [],
                    "deletion_pending": False,
                    "deleted_at": deleted_at,
                    "tombstoned": True,
                    "updated_at": deleted_at,
                }
            )

    def find_dreams_by_source(self, uid, source_id):
        with self._lock:
            return [
                copy.deepcopy(dream)
                for (owner, _), dream in self.dreams.items()
                if owner == uid and source_id in (dream.get("source_memory_ids") or [])
            ]

    def remove_media_entries(self, uid, dream_id, object_keys, updated_at):
        with self._lock:
            dream = self.dreams.get((uid, dream_id))
            if dream is None:
                return
            dream["media"] = [
                entry for entry in (dream.get("media") or []) if entry.get("object_key") not in object_keys
            ]
            dream["updated_at"] = updated_at

    def all_dreams(self):
        with self._lock:
            return [(owner, copy.deepcopy(dream)) for (owner, _), dream in self.dreams.items()]

    def record_reconciliation(self, receipt):
        with self._lock:
            self.reconciliations.append(copy.deepcopy(receipt))

    def latest_reconciliation(self, pepper_version):
        with self._lock:
            receipts = [
                receipt
                for receipt in self.reconciliations
                if receipt.get("pepper_version") == pepper_version and receipt.get("status") == "completed"
            ]
            return copy.deepcopy(receipts[-1]) if receipts else None
