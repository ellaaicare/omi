"""Private first-party object storage for generated memory artwork."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from google.cloud import storage
from google.cloud.exceptions import NotFound
from google.api_core.exceptions import PreconditionFailed

ARTWORK_OBJECT_RE = re.compile(
    r"^users/(?P<owner>[0-9a-f]{64})/profiles/(?P<profile>[0-9a-f]{64})/memories/"
    r"(?P<memory>[A-Za-z0-9_.:-]{1,256})/(?P<generation>[0-9a-f]{64})\.(?P<extension>png|webp|jpg)$"
)
MAX_ARTWORK_BYTES = 12 * 1024 * 1024
SIGNED_URL_TTL_SECONDS = 300


class MemoryArtworkStorageError(RuntimeError):
    """A content-free storage failure safe to surface as a fixed error code."""


@dataclass(frozen=True)
class StoredArtwork:
    object_key: str
    object_generation: str
    content_type: str
    byte_size: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extension(content_type: str) -> str:
    return {
        "image/png": "png",
        "image/webp": "webp",
        "image/jpeg": "jpg",
    }.get(content_type, "")


def object_key_for(
    *,
    uid: str,
    profile_binding_id: str,
    memory_id: str,
    generation_key: str,
    content_type: str,
) -> str:
    extension = _extension(content_type)
    if not extension:
        raise MemoryArtworkStorageError("memory_artwork_content_type_invalid")
    if not memory_id or len(memory_id) > 256 or re.fullmatch(r"[A-Za-z0-9_.:-]+", memory_id) is None:
        raise MemoryArtworkStorageError("memory_artwork_id_invalid")
    if re.fullmatch(r"[0-9a-f]{64}", generation_key) is None:
        raise MemoryArtworkStorageError("memory_artwork_generation_key_invalid")
    return (
        f"users/{_sha256(uid)}/profiles/{_sha256(profile_binding_id)}/memories/"
        f"{memory_id}/{generation_key}.{extension}"
    )


def _validated_owner_key(uid: str, memory_id: str, object_key: str) -> re.Match[str]:
    match = ARTWORK_OBJECT_RE.fullmatch(str(object_key or ""))
    if match is None:
        raise MemoryArtworkStorageError("memory_artwork_object_key_invalid")
    if match.group("owner") != _sha256(uid) or match.group("memory") != memory_id:
        raise MemoryArtworkStorageError("memory_artwork_object_owner_mismatch")
    return match


def _validated_artwork_key(
    uid: str,
    profile_binding_id: str,
    memory_id: str,
    generation_key: str,
    object_key: str,
) -> re.Match[str]:
    match = _validated_owner_key(uid, memory_id, object_key)
    if match.group("profile") != _sha256(profile_binding_id):
        raise MemoryArtworkStorageError("memory_artwork_object_profile_mismatch")
    if match.group("generation") != generation_key:
        raise MemoryArtworkStorageError("memory_artwork_object_generation_mismatch")
    return match


class GCSMemoryArtworkStore:
    """GCS-backed private store. The bucket must never be publicly readable."""

    def __init__(self, *, bucket_name: Optional[str] = None, client: Optional[storage.Client] = None):
        self.bucket_name = (bucket_name or os.getenv("ELLA_MEMORY_ARTWORK_BUCKET", "")).strip()
        if not self.bucket_name:
            raise MemoryArtworkStorageError("memory_artwork_storage_not_configured")
        self.client = client or storage.Client()

    def put(
        self,
        *,
        uid: str,
        profile_binding_id: str,
        memory_id: str,
        generation_key: str,
        content_type: str,
        image_bytes: bytes,
    ) -> StoredArtwork:
        if not image_bytes or len(image_bytes) > MAX_ARTWORK_BYTES:
            raise MemoryArtworkStorageError("memory_artwork_payload_size_invalid")
        object_key = object_key_for(
            uid=uid,
            profile_binding_id=profile_binding_id,
            memory_id=memory_id,
            generation_key=generation_key,
            content_type=content_type,
        )
        blob = self.client.bucket(self.bucket_name).blob(object_key)
        blob.cache_control = "private, max-age=300"
        try:
            blob.upload_from_string(image_bytes, content_type=content_type, if_generation_match=0)
        except PreconditionFailed:
            # The deterministic generation key makes an existing object the
            # idempotent result of an earlier outcome-ambiguous upload.
            pass
        blob.reload()
        if str(blob.content_type or "").split(";", 1)[0].strip().lower() != content_type:
            raise MemoryArtworkStorageError("memory_artwork_object_content_type_mismatch")
        return StoredArtwork(
            object_key=object_key,
            object_generation=str(blob.generation or ""),
            content_type=content_type,
            byte_size=len(image_bytes),
        )

    def signed_get_url(
        self,
        *,
        uid: str,
        profile_binding_id: str,
        memory_id: str,
        generation_key: str,
        object_key: str,
    ) -> str:
        _validated_artwork_key(uid, profile_binding_id, memory_id, generation_key, object_key)
        blob = self.client.bucket(self.bucket_name).blob(object_key)
        if not blob.exists():
            raise MemoryArtworkStorageError("memory_artwork_object_missing")
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=SIGNED_URL_TTL_SECONDS),
            method="GET",
        )

    def delete(self, *, uid: str, memory_id: str, object_key: str) -> None:
        _validated_owner_key(uid, memory_id, object_key)
        try:
            self.client.bucket(self.bucket_name).blob(object_key).delete()
        except NotFound:
            return

    def delete_memory_prefix(
        self,
        *,
        uid: str,
        memory_id: str,
        profile_binding_id: Optional[str] = None,
    ) -> int:
        if not memory_id or len(memory_id) > 256 or re.fullmatch(r"[A-Za-z0-9_.:-]+", memory_id) is None:
            raise MemoryArtworkStorageError("memory_artwork_id_invalid")
        owner_prefix = f"users/{_sha256(uid)}/"
        if not profile_binding_id:
            raise MemoryArtworkStorageError("memory_artwork_binding_required")
        prefix = f"{owner_prefix}profiles/{_sha256(profile_binding_id)}/memories/" f"{memory_id}/"
        deleted = 0
        for blob in self.client.bucket(self.bucket_name).list_blobs(prefix=prefix):
            match = ARTWORK_OBJECT_RE.fullmatch(str(blob.name or ""))
            if match is None or match.group("owner") != _sha256(uid) or match.group("memory") != memory_id:
                continue
            blob.delete()
            deleted += 1
        return deleted

    def delete_user_prefix(self, *, uid: str) -> int:
        prefix = f"users/{_sha256(uid)}/"
        deleted = 0
        for blob in self.client.bucket(self.bucket_name).list_blobs(prefix=prefix):
            blob.delete()
            deleted += 1
        return deleted


def delete_conversation_artwork_if_present(uid: str, memory_id: str, conversation: dict) -> None:
    artwork = conversation.get("artwork") if isinstance(conversation, dict) else None
    object_key = str((artwork or {}).get("object_key") or "") if isinstance(artwork, dict) else ""
    if not isinstance(artwork, dict) or not artwork:
        return
    store = GCSMemoryArtworkStore()
    if object_key:
        store.delete(uid=uid, memory_id=memory_id, object_key=object_key)
    store.delete_memory_prefix(
        uid=uid,
        memory_id=memory_id,
        profile_binding_id=str(artwork.get("binding_id") or "") or None,
    )


def delete_all_user_artwork(uid: str, *, cleanup_required: bool = False) -> int:
    if not os.getenv("ELLA_MEMORY_ARTWORK_BUCKET", "").strip():
        if cleanup_required:
            raise MemoryArtworkStorageError("memory_artwork_storage_cleanup_unavailable")
        return 0
    return GCSMemoryArtworkStore().delete_user_prefix(uid=uid)


def prepare_account_artwork_deletion(uid: str, *, repository=None) -> int:
    if repository is None:
        from database import memory_artwork as repository

    repository.begin_account_deletion(uid)
    if repository.has_processing_jobs(uid):
        raise MemoryArtworkStorageError("memory_artwork_worker_drain_pending")
    deleted = delete_all_user_artwork(
        uid,
        cleanup_required=repository.storage_cleanup_required(uid),
    )
    repository.delete_jobs_for_uid(uid)
    return deleted
