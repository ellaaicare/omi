"""Authenticated, owner-scoped dream media lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from database.dream_media import (
    DreamMediaRepository,
    DreamMediaRepositoryError,
    FirestoreDreamMediaRepository,
    dream_is_tombstoned,
    source_is_excluded,
    user_deletion_pending,
)
from utils.ella.private_media_storage import (
    DREAM_MEDIA_PREFIX,
    MAX_DREAM_MEDIA_BYTES,
    DreamMediaPepperConfig,
    GCSPrivateMediaStore,
    PrivateMediaCredentialRedactionFilter,
    PrivateMediaStorageError,
    build_dream_media_object_key,
    derive_uid_hash,
    redact_dream_media_credentials,
    require_faststart_mp4,
    sniff_content_type,
)

logger = logging.getLogger(__name__)
logger.addFilter(PrivateMediaCredentialRedactionFilter())
PENDING_RETENTION = timedelta(hours=24)
DEFAULT_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60


class DreamMediaError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 503, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class DreamUpload:
    request_id: str
    title: str
    narrative: str
    captions: tuple[str, ...]
    source_memory_ids: tuple[str, ...]
    created_at: Optional[datetime] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _safe_log(dream_id: str, asset_key: str, status: str, expiry: int = 0) -> None:
    logger.info(
        "dream_media dream_id=%s asset_key=%s status=%s expiry=%s",
        dream_id,
        asset_key,
        status,
        expiry,
    )


def _inventory(dream: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in (dream.get("media") or []) if isinstance(entry, dict)]


def _public_media_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entry.get(key)
        for key in (
            "asset_key",
            "state",
            "content_type",
            "pepper_version",
            "created_at",
            "committed_at",
            "bytes",
        )
        if entry.get(key) is not None
    }


def _public_dream(dream: dict[str, Any]) -> dict[str, Any]:
    return {
        "dream_id": dream.get("dream_id"),
        "title": dream.get("title") or "",
        "narrative": dream.get("narrative") or "",
        "captions": list(dream.get("captions") or []),
        "source_memory_ids": list(dream.get("source_memory_ids") or []),
        "created_at": dream.get("created_at"),
        "updated_at": dream.get("updated_at"),
        "media": [_public_media_entry(entry) for entry in _inventory(dream)],
    }


class DreamMediaService:
    def __init__(
        self,
        repository: DreamMediaRepository,
        *,
        store=None,
        pepper_config: Optional[DreamMediaPepperConfig] = None,
        now=_utc_now,
    ):
        self.repository = repository
        self._configured_store = store
        self._configured_peppers = pepper_config
        self.now = now

    def _store(self):
        if self._configured_store is None:
            self._configured_store = GCSPrivateMediaStore()
        return self._configured_store

    def _peppers(self) -> DreamMediaPepperConfig:
        if self._configured_peppers is None:
            self._configured_peppers = DreamMediaPepperConfig.from_environment()
        return self._configured_peppers

    def _eligible_dream(self, uid: str, dream_id: str) -> dict[str, Any]:
        user = self.repository.get_user(uid)
        dream = self.repository.get_dream(uid, dream_id)
        if user_deletion_pending(user) or dream_is_tombstoned(dream):
            raise DreamMediaError("dream_media_not_found", status_code=404, retryable=False)
        assert dream is not None
        for source_id in dream.get("source_memory_ids") or []:
            if source_is_excluded(self.repository.get_source(uid, str(source_id))):
                raise DreamMediaError("dream_media_not_found", status_code=404, retryable=False)
        return dream

    def list_dreams(self, uid: str) -> list[dict[str, Any]]:
        if user_deletion_pending(self.repository.get_user(uid)):
            return []
        visible: list[dict[str, Any]] = []
        for dream in self.repository.list_dreams(uid):
            dream_id = str(dream.get("dream_id") or "")
            try:
                visible.append(_public_dream(self._eligible_dream(uid, dream_id)))
            except DreamMediaError as exc:
                if exc.status_code != 404:
                    raise
        visible.sort(key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return visible

    def get_media(self, uid: str, dream_id: str) -> dict[str, Any]:
        dream = self._eligible_dream(uid, dream_id)
        try:
            pepper_config = self._peppers()
        except PrivateMediaStorageError as exc:
            raise DreamMediaError(str(exc), status_code=503, retryable=True) from exc
        media = []
        for entry in _inventory(dream):
            if entry.get("state") != "committed":
                continue
            asset_key = str(entry.get("asset_key") or "")
            try:
                signed = self._store().sign_get(
                    uid=uid,
                    object_key=str(entry.get("object_key") or ""),
                    pepper_config=pepper_config,
                )
            except PrivateMediaStorageError as exc:
                _safe_log(dream_id, asset_key, str(exc), 0)
                raise DreamMediaError(str(exc), status_code=503, retryable=True) from exc
            _safe_log(dream_id, asset_key, "signed", signed.expires_in_seconds)
            media.append(
                {
                    "asset_key": asset_key,
                    "content_type": signed.content_type,
                    "bytes": entry.get("bytes"),
                    "url": signed.url,
                    "expires_in_seconds": signed.expires_in_seconds,
                }
            )
        return {"dream_id": dream_id, "media": media}

    def upload(
        self,
        *,
        uid: str,
        dream_id: str,
        upload: DreamUpload,
        payload: bytes,
        claimed_content_type: str,
    ) -> dict[str, Any]:
        if not payload or len(payload) > MAX_DREAM_MEDIA_BYTES:
            raise DreamMediaError("dream_media_payload_size_invalid", status_code=413, retryable=False)
        try:
            content_type = sniff_content_type(payload)
            normalized_claim = claimed_content_type.split(";", 1)[0].strip().lower()
            if normalized_claim and normalized_claim != content_type:
                raise PrivateMediaStorageError("dream_media_content_type_mismatch")
            if content_type == "video/mp4":
                require_faststart_mp4(payload)
        except PrivateMediaStorageError as exc:
            raise DreamMediaError(str(exc), status_code=415, retryable=False) from exc
        try:
            pepper_config = self._peppers()
            pepper = pepper_config.pepper_for(pepper_config.active_version)
        except PrivateMediaStorageError as exc:
            raise DreamMediaError(str(exc), status_code=503, retryable=True) from exc

        for source_id in upload.source_memory_ids:
            if source_is_excluded(self.repository.get_source(uid, source_id)):
                raise DreamMediaError("dream_media_source_excluded", status_code=409, retryable=False)
        if user_deletion_pending(self.repository.get_user(uid)):
            raise DreamMediaError("dream_media_deletion_pending", status_code=409, retryable=False)

        existing = self.repository.get_dream(uid, dream_id)
        dream_key = str((existing or {}).get("dream_key") or secrets.token_hex(16))
        key = build_dream_media_object_key(
            uid=uid,
            pepper=pepper,
            pepper_version=pepper_config.active_version,
            content_type=content_type,
            dream_key=dream_key,
        )
        now = self.now()
        pending = {
            "request_id": upload.request_id,
            "asset_key": key.asset_key,
            "object_key": key.object_key,
            "state": "pending",
            "content_type": content_type,
            "pepper_version": key.pepper_version,
            "created_at": now,
            "request_digest": hashlib.sha256(payload).hexdigest(),
        }
        metadata = {
            "title": upload.title,
            "narrative": upload.narrative,
            "captions": list(upload.captions),
            "source_memory_ids": list(upload.source_memory_ids),
            "created_at": upload.created_at or now,
        }
        try:
            reserved = self.repository.reserve_upload(
                uid=uid,
                dream_id=dream_id,
                dream_key=dream_key,
                metadata=metadata,
                media_entry=pending,
            )
        except DreamMediaRepositoryError as exc:
            raise DreamMediaError(str(exc), status_code=409, retryable=False) from exc
        if reserved.get("request_digest") != pending["request_digest"]:
            raise DreamMediaError("dream_media_idempotency_conflict", status_code=409, retryable=False)
        if reserved.get("state") == "committed":
            return _public_media_entry(reserved)

        object_key = str(reserved.get("object_key") or "")
        asset_key = str(reserved.get("asset_key") or "")
        try:
            stored = self._store().upload(object_key=object_key, content_type=content_type, payload=payload)
            committed = self.repository.commit_upload(
                uid=uid,
                dream_id=dream_id,
                asset_key=asset_key,
                object_key=object_key,
                byte_size=stored.byte_size,
                sha256=stored.sha256,
                committed_at=self.now(),
            )
        except (PrivateMediaStorageError, DreamMediaRepositoryError) as exc:
            try:
                self._store().delete(object_key)
            except PrivateMediaStorageError:
                _safe_log(dream_id, asset_key, "compensating_delete_failed", 0)
            raise DreamMediaError(str(exc), status_code=503, retryable=True) from exc
        finally:
            del payload
        _safe_log(dream_id, asset_key, "committed", 0)
        return _public_media_entry(committed)

    def _user_inventory_keys(self, uid: str) -> set[str]:
        return {
            str(entry.get("object_key"))
            for dream in self.repository.list_dreams(uid)
            for entry in _inventory(dream)
            if entry.get("object_key")
        }

    def sweep_user_orphans(self, uid: str) -> int:
        pepper_config = self._peppers()
        inventory = self._user_inventory_keys(uid)
        deleted = 0
        for version, pepper in pepper_config.peppers.items():
            owner = derive_uid_hash(uid, pepper)
            prefix = f"{DREAM_MEDIA_PREFIX}/p{version}/{owner}/"
            for object_key in self._store().list_keys(prefix):
                if object_key not in inventory:
                    self._store().delete(object_key)
                    deleted += 1
        return deleted

    def delete_dream(self, uid: str, dream_id: str, *, run_orphan_sweep: bool = True) -> bool:
        deleted_at = self.now()
        try:
            dream = self.repository.begin_delete(uid, dream_id, deleted_at)
        except DreamMediaRepositoryError as exc:
            raise DreamMediaError(str(exc)) from exc
        if dream is None:
            return False
        for entry in _inventory(dream):
            object_key = str(entry.get("object_key") or "")
            if object_key:
                try:
                    self._store().delete(object_key)
                except PrivateMediaStorageError as exc:
                    raise DreamMediaError(str(exc)) from exc
        if run_orphan_sweep:
            try:
                self.sweep_user_orphans(uid)
            except PrivateMediaStorageError as exc:
                raise DreamMediaError(str(exc)) from exc
        self.repository.finish_delete(uid, dream_id, deleted_at)
        return True

    def delete_for_source(self, uid: str, source_id: str) -> int:
        deleted = 0
        for dream in self.repository.find_dreams_by_source(uid, source_id):
            if self.delete_dream(uid, str(dream.get("dream_id") or ""), run_orphan_sweep=False):
                deleted += 1
        if deleted:
            try:
                self.sweep_user_orphans(uid)
            except PrivateMediaStorageError as exc:
                raise DreamMediaError(str(exc)) from exc
        return deleted

    def delete_account(self, uid: str) -> int:
        dreams = [
            dream
            for dream in self.repository.list_dreams(uid)
            if not dream.get("deleted_at") and not dream.get("tombstoned")
        ]
        deleted = 0
        for dream in dreams:
            if self.delete_dream(uid, str(dream.get("dream_id") or ""), run_orphan_sweep=False):
                deleted += 1
        try:
            self.sweep_user_orphans(uid)
        except PrivateMediaStorageError as exc:
            raise DreamMediaError(str(exc)) from exc
        return deleted

    def sweep_stale_pending(self) -> int:
        cutoff = self.now() - PENDING_RETENTION
        removed = 0
        for uid, dream in self.repository.all_dreams():
            stale = {
                str(entry.get("object_key"))
                for entry in _inventory(dream)
                if entry.get("state") == "pending"
                and (_as_utc(entry.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) <= cutoff
                and entry.get("object_key")
            }
            if not stale:
                continue
            for object_key in stale:
                self._store().delete(object_key)
            self.repository.remove_media_entries(uid, str(dream.get("dream_id") or ""), stale, self.now())
            removed += len(stale)
        return removed

    def bucket_wide_sweep(self) -> dict[str, Any]:
        self.sweep_stale_pending()
        pepper_config = self._peppers()
        inventory = {
            str(entry.get("object_key"))
            for _, dream in self.repository.all_dreams()
            for entry in _inventory(dream)
            if entry.get("object_key")
        }
        run_id = str(uuid.uuid4())
        completed_at = self.now()
        results = []
        for version in pepper_config.known_versions:
            prefix = f"{DREAM_MEDIA_PREFIX}/p{version}/"
            listed = self._store().list_keys(prefix)
            deleted = 0
            for object_key in listed:
                if object_key not in inventory:
                    self._store().delete(object_key)
                    deleted += 1
            remaining = self._store().list_keys(prefix)
            receipt = {
                "run_id": run_id,
                "pepper_version": version,
                "status": "completed",
                "initial_listed_count": len(listed),
                "deleted_count": deleted,
                "listed_count": len(remaining),
                "remaining_count": len(remaining),
                "completed_at": completed_at,
            }
            self.repository.record_reconciliation(receipt)
            results.append(receipt)
        return {"run_id": run_id, "versions": results}

    def assert_pepper_retirable(self, pepper_version: int) -> None:
        references = [
            entry
            for _, dream in self.repository.all_dreams()
            for entry in _inventory(dream)
            if entry.get("pepper_version") == pepper_version
        ]
        if references:
            raise DreamMediaError("dream_media_pepper_inventory_not_empty", status_code=409, retryable=False)
        receipt = self.repository.latest_reconciliation(pepper_version)
        if not receipt or receipt.get("listed_count") != 0 or receipt.get("remaining_count") != 0:
            raise DreamMediaError("dream_media_pepper_reconciliation_required", status_code=409, retryable=False)
        if self._store().list_keys(f"{DREAM_MEDIA_PREFIX}/p{pepper_version}/"):
            raise DreamMediaError("dream_media_pepper_prefix_not_empty", status_code=409, retryable=False)


_service: Optional[DreamMediaService] = None
_sweep_task: Optional[asyncio.Task] = None


def get_dream_media_service() -> DreamMediaService:
    global _service
    if _service is None:
        _service = DreamMediaService(FirestoreDreamMediaRepository())
    return _service


def configure_dream_media_service(service: Optional[DreamMediaService]) -> None:
    global _service
    _service = service


def prepare_source_memory_dream_deletion(uid: str, source_id: str) -> int:
    return get_dream_media_service().delete_for_source(uid, source_id)


def prepare_account_dream_media_deletion(uid: str) -> int:
    return get_dream_media_service().delete_account(uid)


async def _sweep_loop() -> None:
    interval = max(int(os.getenv("DREAM_MEDIA_SWEEP_INTERVAL_SECONDS", DEFAULT_SWEEP_INTERVAL_SECONDS)), 300)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(get_dream_media_service().bucket_wide_sweep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("dream_media_sweep status=failed error=%s", redact_dream_media_credentials(type(exc).__name__))


async def start_dream_media_sweeper() -> None:
    global _sweep_task
    if not os.getenv("BUCKET_DREAM_MEDIA", "").strip() or _sweep_task is not None:
        return
    _sweep_task = asyncio.create_task(_sweep_loop())


async def stop_dream_media_sweeper() -> None:
    global _sweep_task
    if _sweep_task is None:
        return
    _sweep_task.cancel()
    try:
        await _sweep_task
    except asyncio.CancelledError:
        pass
    _sweep_task = None
