"""Versioned, owner-bound memory artwork generation contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import urlparse

import httpx

import database.memory_artwork as artwork_db
from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_authority_identity
from utils.ella.memory_artwork_storage import GCSMemoryArtworkStore, MemoryArtworkStorageError, StoredArtwork

ARTWORK_SCHEMA_VERSION = "ella.memory_artwork.v1"
ARTWORK_CONSENT_VERSION = "ella.memory_artwork.consent.v1"
DEFAULT_STYLE_VERSION = "ella.memory_artwork.style.soft-gouache.v1"
SUPPORTED_STYLE_VERSIONS = {
    DEFAULT_STYLE_VERSION,
    "ella.memory_artwork.style.paper-collage.v1",
    "ella.memory_artwork.style.graphic-landscape.v1",
}
TARGET_WIDTH = 1536
TARGET_HEIGHT = 1024
MAX_BACKFILL_MEMORIES = 10
BACKFILL_SCAN_LIMIT = 50
PROVIDER_TIMEOUT_SECONDS = 45.0
PROVIDER_TOKEN_FILE_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_TOKEN_FILE"
PROVIDER_URL_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_URL"
PROVIDER_ALLOWED_HOST_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_ALLOWED_HOST"
WORKER_INTERVAL_SECONDS_ENV = "ELLA_MEMORY_ARTWORK_WORKER_INTERVAL_SECONDS"
WORKER_BATCH_SIZE = 10
WORKER_MAX_ATTEMPTS = 5


class MemoryArtworkError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ArtworkRuntimeAuthority:
    uid: str
    binding_id: str
    profile_id: str
    authority_digest: str


@dataclass(frozen=True)
class GeneratedArtwork:
    image_bytes: bytes
    content_type: str
    pixel_width: int
    pixel_height: int


@dataclass(frozen=True)
class MemoryArtworkConfig:
    enabled: bool
    release_enabled: bool
    provider_enabled: bool
    backfill_enabled: bool

    @classmethod
    def from_env(cls) -> "MemoryArtworkConfig":
        enabled = os.getenv("ELLA_MEMORY_ARTWORK_ENABLED", "false").strip().lower() == "true"
        return cls(
            enabled=enabled,
            release_enabled=os.getenv("ELLA_MEMORY_ARTWORK_RELEASE_ENABLED", "false").strip().lower() == "true",
            provider_enabled=os.getenv("ELLA_MEMORY_ARTWORK_PROVIDER_ENABLED", "false").strip().lower() == "true",
            backfill_enabled=os.getenv("ELLA_MEMORY_ARTWORK_BACKFILL_ENABLED", "false").strip().lower() == "true",
        )


class MemoryArtworkRepository(Protocol):
    def get_preferences(self, uid: str) -> dict[str, Any]: ...

    def set_preferences(self, uid: str, preferences: dict[str, Any]) -> None: ...

    def get_conversation(self, uid: str, memory_id: str) -> Optional[dict[str, Any]]: ...

    def list_recent_conversations(self, uid: str, *, limit: int) -> list[dict[str, Any]]: ...

    def reserve_generation(self, uid: str, memory_id: str, **kwargs) -> dict[str, Any]: ...

    def claim_generation(self, uid: str, memory_id: str, **kwargs) -> Optional[dict[str, Any]]: ...

    def finalize_generation(self, uid: str, memory_id: str, **kwargs) -> bool: ...

    def mark_generation_unavailable(self, uid: str, memory_id: str, **kwargs) -> bool: ...

    def list_pending_jobs(self, **kwargs) -> list[dict[str, Any]]: ...

    def complete_job(self, uid: str, memory_id: str, generation_key: str) -> None: ...

    def retry_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> None: ...

    def fail_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> None: ...

    def mark_storage_cleanup_required(self, uid: str) -> None: ...


class FirestoreMemoryArtworkRepository:
    get_preferences = staticmethod(artwork_db.get_preferences)
    set_preferences = staticmethod(artwork_db.set_preferences)
    get_conversation = staticmethod(artwork_db.get_conversation)
    list_recent_conversations = staticmethod(artwork_db.list_recent_conversations)
    reserve_generation = staticmethod(artwork_db.reserve_generation)
    claim_generation = staticmethod(artwork_db.claim_generation)
    finalize_generation = staticmethod(artwork_db.finalize_generation)
    mark_generation_unavailable = staticmethod(artwork_db.mark_generation_unavailable)
    list_pending_jobs = staticmethod(artwork_db.list_pending_jobs)
    complete_job = staticmethod(artwork_db.complete_job)
    retry_job = staticmethod(artwork_db.retry_job)
    fail_job = staticmethod(artwork_db.fail_job)
    mark_storage_cleanup_required = staticmethod(artwork_db.mark_storage_cleanup_required)


class MemoryArtworkProvider(Protocol):
    async def generate(self, *, prompt: str, style_version: str, idempotency_key: str) -> GeneratedArtwork: ...


class MemoryArtworkStore(Protocol):
    def put(self, **kwargs) -> StoredArtwork: ...

    def signed_get_url(self, **kwargs) -> str: ...

    def delete(self, **kwargs) -> None: ...

    def delete_memory_prefix(self, **kwargs) -> int: ...


def _read_protected_token(path_value: str) -> str:
    path = Path(path_value)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise MemoryArtworkError("memory_artwork_provider_credential_unavailable", retryable=True) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise MemoryArtworkError("memory_artwork_provider_credential_insecure", retryable=False)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MemoryArtworkError("memory_artwork_provider_credential_unavailable", retryable=True) from exc
    if not token:
        raise MemoryArtworkError("memory_artwork_provider_credential_unavailable", retryable=True)
    return token


class FirstPartyHTTPArtworkProvider:
    """Fixed first-party adapter boundary; it never accepts a caller-selected URL."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None):
        self.url = os.getenv(PROVIDER_URL_ENV, "").strip()
        parsed = urlparse(self.url)
        allowed_host = os.getenv(PROVIDER_ALLOWED_HOST_ENV, "").strip().lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not allowed_host
            or parsed.hostname.lower() != allowed_host
            or (parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"})
        ):
            raise MemoryArtworkError("memory_artwork_provider_endpoint_invalid", retryable=False)
        token_file = os.getenv(PROVIDER_TOKEN_FILE_ENV, "").strip()
        if not token_file:
            raise MemoryArtworkError("memory_artwork_provider_credential_unavailable", retryable=True)
        self.token = _read_protected_token(token_file)
        self.client = client

    async def generate(self, *, prompt: str, style_version: str, idempotency_key: str) -> GeneratedArtwork:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=PROVIDER_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            response = await client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": idempotency_key,
                    "X-Ella-Contract": ARTWORK_SCHEMA_VERSION,
                },
                json={
                    "prompt": prompt,
                    "style_version": style_version,
                    "width": TARGET_WIDTH,
                    "height": TARGET_HEIGHT,
                },
            )
        except httpx.HTTPError as exc:
            raise MemoryArtworkError("memory_artwork_provider_unavailable", retryable=True) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise MemoryArtworkError("memory_artwork_provider_rejected", retryable=response.status_code >= 500)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            width = int(response.headers.get("x-ella-image-width", "0"))
            height = int(response.headers.get("x-ella-image-height", "0"))
        except ValueError as exc:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False) from exc
        if content_type not in {"image/png", "image/webp", "image/jpeg"} or width <= 0 or height <= 0:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
        return GeneratedArtwork(
            image_bytes=response.content,
            content_type=content_type,
            pixel_width=width,
            pixel_height=height,
        )


async def resolve_memory_artwork_authority(uid: str) -> ArtworkRuntimeAuthority:
    try:
        runtime = await resolve_isolated_runtime(uid, target_mode="hermes-chat")
    except Exception as exc:
        raise MemoryArtworkError("memory_artwork_runtime_authority_unavailable", retryable=True) from exc
    if runtime is None:
        raise MemoryArtworkError("memory_artwork_runtime_not_ready", retryable=True)
    identity = runtime_authority_identity(runtime)
    profile_id = str(runtime.profile_user_id or runtime.account_user_id or "").strip()
    if not runtime.binding_id or not profile_id:
        raise MemoryArtworkError("memory_artwork_owner_scope_missing", retryable=False)
    return ArtworkRuntimeAuthority(
        uid=uid,
        binding_id=runtime.binding_id,
        profile_id=profile_id,
        authority_digest=identity.digest,
    )


def _terminal_enrichment(conversation: dict[str, Any]) -> Optional[str]:
    enrichment = conversation.get("enrichment_state") or {}
    revision = str(conversation.get("active_summary_version_id") or "").strip()
    if not revision or not isinstance(enrichment, dict):
        return None
    if (
        conversation.get("status") == "completed"
        and enrichment.get("status") == "writeback_applied"
        and enrichment.get("kind")
        in {"observer_enriched", "corrected_enriched", "hermes_enriched", "recovered_enriched"}
    ):
        return revision
    return None


def _source_is_sensitive(conversation: dict[str, Any]) -> bool:
    assessment = conversation.get("internal_assessment") or {}
    signal = conversation.get("ella_signal") or {}
    tags = {str(value).strip().lower() for value in (conversation.get("ella_tags") or [])}
    sensitive_tags = {"caregiver-private", "safety", "distress", "emergency", "self-harm"}
    risk = str(assessment.get("risk_level") or "").strip().lower() if isinstance(assessment, dict) else ""
    guardian_relevant = bool(signal.get("guardian_relevant")) if isinstance(signal, dict) else False
    return bool(tags & sensitive_tags or guardian_relevant or risk in {"medium", "high", "critical"})


def _prompt_for(conversation: dict[str, Any], style_version: str) -> tuple[str, str]:
    structured = conversation.get("structured") or {}
    title = " ".join(str(structured.get("title") or "").split())[:240]
    overview = " ".join(str(structured.get("overview") or "").split())[:1200]
    if not title and not overview:
        raise MemoryArtworkError("memory_artwork_summary_missing", retryable=False)
    prompt = (
        "Create a calm 3:2 editorial illustration inspired only by this memory summary. "
        "Use symbolic places, objects, color, and light. Do not depict identifiable faces, names, readable text, "
        "logos, medical conditions, or facts not present in the summary. "
        f"Style contract: {style_version}. Summary title: {title}. Summary overview: {overview}."
    )
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _generation_key(
    *,
    uid: str,
    authority: ArtworkRuntimeAuthority,
    memory_id: str,
    enrichment_revision: str,
    style_version: str,
    prompt_sha256: str,
) -> str:
    material = {
        "uid": uid,
        "binding_id": authority.binding_id,
        "profile_id": authority.profile_id,
        "memory_id": memory_id,
        "enrichment_revision": enrichment_revision,
        "style_version": style_version,
        "prompt_sha256": prompt_sha256,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _preferences_match_authority(
    preferences: dict[str, Any],
    authority: ArtworkRuntimeAuthority,
    *,
    style_version: Optional[str] = None,
) -> bool:
    return bool(
        preferences.get("consent") == "accepted"
        and preferences.get("consent_version") == ARTWORK_CONSENT_VERSION
        and preferences.get("binding_id") == authority.binding_id
        and preferences.get("profile_id") == authority.profile_id
        and preferences.get("authority_digest") == authority.authority_digest
        and (style_version is None or preferences.get("style_version") == style_version)
    )


class MemoryArtworkService:
    def __init__(
        self,
        *,
        repository: Optional[MemoryArtworkRepository] = None,
        authority_resolver: Callable[[str], Awaitable[ArtworkRuntimeAuthority]] = resolve_memory_artwork_authority,
        provider_factory: Callable[[], MemoryArtworkProvider] = FirstPartyHTTPArtworkProvider,
        store_factory: Callable[[], MemoryArtworkStore] = GCSMemoryArtworkStore,
        config: Optional[MemoryArtworkConfig] = None,
    ):
        self.repository = repository or FirestoreMemoryArtworkRepository()
        self.authority_resolver = authority_resolver
        self.provider_factory = provider_factory
        self.store_factory = store_factory
        self.config = config or MemoryArtworkConfig.from_env()

    async def preferences(self, uid: str) -> dict[str, Any]:
        stored = self.repository.get_preferences(uid)
        consent = str(stored.get("consent") or "not_set")
        style = str(stored.get("style_version") or DEFAULT_STYLE_VERSION)
        return {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "consent_version": ARTWORK_CONSENT_VERSION,
            "consent": consent if consent in {"accepted", "declined"} else "not_set",
            "style_version": style if style in SUPPORTED_STYLE_VERSIONS else DEFAULT_STYLE_VERSION,
            "supported_style_versions": sorted(SUPPORTED_STYLE_VERSIONS),
            "release_enabled": self.config.enabled and self.config.release_enabled,
        }

    async def set_preferences(self, uid: str, *, consent: str, consent_version: str, style_version: str) -> dict:
        if consent not in {"accepted", "declined"}:
            raise MemoryArtworkError("memory_artwork_consent_invalid")
        if consent_version != ARTWORK_CONSENT_VERSION:
            raise MemoryArtworkError("memory_artwork_consent_version_stale")
        if style_version not in SUPPORTED_STYLE_VERSIONS:
            raise MemoryArtworkError("memory_artwork_style_version_invalid")
        authority = await self.authority_resolver(uid)
        self.repository.set_preferences(
            uid,
            {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "consent": consent,
                "consent_version": consent_version,
                "style_version": style_version,
                "binding_id": authority.binding_id,
                "profile_id": authority.profile_id,
                "authority_digest": authority.authority_digest,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        return await self.preferences(uid)

    async def enqueue(
        self,
        uid: str,
        memory_id: str,
        *,
        preserve_job_attempts: bool = False,
    ) -> dict[str, Any]:
        conversation = self.repository.get_conversation(uid, memory_id)
        if conversation is None:
            raise MemoryArtworkError("memory_artwork_memory_not_found")
        enrichment_revision = _terminal_enrichment(conversation)
        if enrichment_revision is None:
            raise MemoryArtworkError("memory_artwork_enrichment_not_terminal", retryable=True)
        preferences = self.repository.get_preferences(uid)
        if preferences.get("consent") == "declined":
            return {"outcome": "declined", "status": "declined"}
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            return {"outcome": "disabled", "status": "unavailable"}
        if preferences.get("consent") != "accepted" or preferences.get("consent_version") != ARTWORK_CONSENT_VERSION:
            return {"outcome": "consent_required", "status": "unavailable"}
        if _source_is_sensitive(conversation):
            return {"outcome": "sensitive_source_excluded", "status": "unavailable"}
        authority = await self.authority_resolver(uid)
        if (
            preferences.get("binding_id") != authority.binding_id
            or preferences.get("profile_id") != authority.profile_id
            or preferences.get("authority_digest") != authority.authority_digest
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        style_version = str(preferences.get("style_version") or "")
        if style_version not in SUPPORTED_STYLE_VERSIONS:
            raise MemoryArtworkError("memory_artwork_style_version_invalid")
        prompt, prompt_sha256 = _prompt_for(conversation, style_version)
        generation_key = _generation_key(
            uid=uid,
            authority=authority,
            memory_id=memory_id,
            enrichment_revision=enrichment_revision,
            style_version=style_version,
            prompt_sha256=prompt_sha256,
        )
        now = datetime.now(timezone.utc)
        artwork_state = {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "status": "generating",
            "style_version": style_version,
            "enrichment_revision": enrichment_revision,
            "prompt_sha256": prompt_sha256,
            "generation_key": generation_key,
            "authority_digest": authority.authority_digest,
            "binding_id": authority.binding_id,
            "profile_id": authority.profile_id,
            "created_at": now,
            "updated_at": now,
        }
        job_state = {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "uid": uid,
            "memory_id": memory_id,
            "generation_key": generation_key,
            "status": "pending",
            "attempt_count": 0,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
        reservation = self.repository.reserve_generation(
            uid,
            memory_id,
            enrichment_revision=enrichment_revision,
            generation_key=generation_key,
            artwork_state=artwork_state,
            job_state=job_state,
            preserve_job_attempts=preserve_job_attempts,
        )
        return {
            "outcome": reservation.get("outcome"),
            "status": str((reservation.get("artwork") or artwork_state).get("status") or "generating"),
        }

    async def process(self, uid: str, memory_id: str) -> dict[str, Any]:
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            raise MemoryArtworkError("memory_artwork_generation_disabled")
        queued = self.repository.get_conversation(uid, memory_id)
        artwork = (queued or {}).get("artwork") or {}
        generation_key = str(artwork.get("generation_key") or "") if isinstance(artwork, dict) else ""
        if not generation_key:
            raise MemoryArtworkError("memory_artwork_generation_not_queued")
        lease_token = secrets.token_hex(24)
        claimed = self.repository.claim_generation(
            uid,
            memory_id,
            generation_key=generation_key,
            lease_token=lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=120,
        )
        if claimed is None:
            return {"outcome": "not_claimed", "status": str(artwork.get("status") or "unavailable")}
        try:
            authority = await self.authority_resolver(uid)
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_unavailable",
                lease_token=lease_token,
            )
            if isinstance(exc, MemoryArtworkError):
                raise
            raise MemoryArtworkError("memory_artwork_runtime_authority_unavailable", retryable=True) from exc
        if authority.authority_digest != claimed.get("authority_digest"):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_authority_changed")
        preferences = self.repository.get_preferences(uid)
        if not _preferences_match_authority(
            preferences,
            authority,
            style_version=str(claimed.get("style_version") or ""),
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="preference_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_preference_changed")
        conversation = self.repository.get_conversation(uid, memory_id) or {}
        if _terminal_enrichment(conversation) != claimed.get("enrichment_revision"):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="source_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_source_changed")
        if _source_is_sensitive(conversation):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="sensitive_source_excluded",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_sensitive_source_excluded")
        prompt, prompt_sha256 = _prompt_for(conversation, str(claimed.get("style_version") or ""))
        if prompt_sha256 != claimed.get("prompt_sha256"):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="prompt_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_prompt_changed")
        try:
            egress_authority = await self.authority_resolver(uid)
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_unavailable",
                lease_token=lease_token,
            )
            if isinstance(exc, MemoryArtworkError):
                raise
            raise MemoryArtworkError("memory_artwork_runtime_authority_unavailable", retryable=True) from exc
        egress_preferences = self.repository.get_preferences(uid)
        if egress_authority != authority or not _preferences_match_authority(
            egress_preferences,
            egress_authority,
            style_version=str(claimed.get("style_version") or ""),
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_authority_changed")
        # Re-read at the final synchronous boundary before provider egress.
        # Classification can change independently of the summary or prompt.
        egress_conversation = self.repository.get_conversation(uid, memory_id) or {}
        if _terminal_enrichment(egress_conversation) != claimed.get("enrichment_revision") or _source_is_sensitive(
            egress_conversation
        ):
            failure_code = (
                "sensitive_source_excluded" if _source_is_sensitive(egress_conversation) else "source_changed"
            )
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code=failure_code,
                lease_token=lease_token,
            )
            raise MemoryArtworkError(f"memory_artwork_{failure_code}")
        _, egress_prompt_sha256 = _prompt_for(
            egress_conversation,
            str(claimed.get("style_version") or ""),
        )
        if egress_prompt_sha256 != claimed.get("prompt_sha256"):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="prompt_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_prompt_changed")
        provider = self.provider_factory()
        try:
            generated = await provider.generate(
                prompt=prompt,
                style_version=str(claimed.get("style_version") or ""),
                idempotency_key=generation_key,
            )
        except MemoryArtworkError as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code=exc.code,
                lease_token=lease_token,
            )
            raise
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="memory_artwork_provider_failed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_provider_failed", retryable=True) from exc
        if generated.pixel_width != TARGET_WIDTH or generated.pixel_height != TARGET_HEIGHT:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="memory_artwork_dimensions_invalid",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_dimensions_invalid")
        try:
            latest_authority = await self.authority_resolver(uid)
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_unavailable",
                lease_token=lease_token,
            )
            if isinstance(exc, MemoryArtworkError):
                raise
            raise MemoryArtworkError("memory_artwork_runtime_authority_unavailable", retryable=True) from exc
        latest_preferences = self.repository.get_preferences(uid)
        if latest_authority != authority or not _preferences_match_authority(
            latest_preferences,
            latest_authority,
            style_version=str(claimed.get("style_version") or ""),
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_authority_changed")
        # Persist cleanup intent before the deterministic upload. If the upload
        # succeeds but its acknowledgement is lost, deletion still fails closed
        # unless it can remove the owner's private prefix.
        self.repository.mark_storage_cleanup_required(uid)
        store = self.store_factory()
        try:
            stored = store.put(
                uid=uid,
                profile_binding_id=authority.binding_id,
                memory_id=memory_id,
                generation_key=generation_key,
                content_type=generated.content_type,
                image_bytes=generated.image_bytes,
            )
        except MemoryArtworkStorageError as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code=str(exc),
                lease_token=lease_token,
            )
            raise MemoryArtworkError(str(exc), retryable=True) from exc
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="memory_artwork_storage_failed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_storage_failed", retryable=True) from exc
        try:
            final_authority = await self.authority_resolver(uid)
            final_preferences = self.repository.get_preferences(uid)
        except Exception as exc:
            store.delete(uid=uid, memory_id=memory_id, object_key=stored.object_key)
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_unavailable",
                lease_token=lease_token,
            )
            if isinstance(exc, MemoryArtworkError):
                raise
            raise MemoryArtworkError("memory_artwork_runtime_authority_unavailable", retryable=True) from exc
        if final_authority != authority or not _preferences_match_authority(
            final_preferences,
            final_authority,
            style_version=str(claimed.get("style_version") or ""),
        ):
            store.delete(uid=uid, memory_id=memory_id, object_key=stored.object_key)
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_authority_changed")
        ready_state = {
            **claimed,
            "status": "ready",
            "object_key": stored.object_key,
            "object_generation": stored.object_generation,
            "content_type": stored.content_type,
            "byte_size": stored.byte_size,
            "pixel_width": generated.pixel_width,
            "pixel_height": generated.pixel_height,
            "updated_at": datetime.now(timezone.utc),
        }
        ready_state.pop("lease_token", None)
        ready_state.pop("lease_expires_at", None)
        finalized = self.repository.finalize_generation(
            uid,
            memory_id,
            generation_key=generation_key,
            authority_digest=authority.authority_digest,
            lease_token=lease_token,
            ready_state=ready_state,
        )
        if not finalized:
            store.delete(uid=uid, memory_id=memory_id, object_key=stored.object_key)
            raise MemoryArtworkError("memory_artwork_finalize_conflict", retryable=True)
        return {"outcome": "ready", "status": "ready"}

    async def signed_url(self, uid: str, memory_id: str) -> dict[str, Any]:
        conversation = self.repository.get_conversation(uid, memory_id)
        if conversation is None:
            raise MemoryArtworkError("memory_artwork_memory_not_found")
        preferences = self.repository.get_preferences(uid)
        if preferences.get("consent") == "declined":
            return {"schema_version": ARTWORK_SCHEMA_VERSION, "status": "declined"}
        if preferences.get("consent") != "accepted" or preferences.get("consent_version") != ARTWORK_CONSENT_VERSION:
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_consent_required",
            }
        artwork = conversation.get("artwork") or {}
        status = str(artwork.get("status") or "unavailable") if isinstance(artwork, dict) else "unavailable"
        if status != "ready":
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": status if status in {"generating", "unavailable", "declined"} else "unavailable",
                "failure_code": artwork.get("failure_code") if isinstance(artwork, dict) else None,
            }
        authority = await self.authority_resolver(uid)
        if (
            artwork.get("authority_digest") != authority.authority_digest
            or artwork.get("binding_id") != authority.binding_id
            or artwork.get("profile_id") != authority.profile_id
            or not _preferences_match_authority(
                preferences,
                authority,
                style_version=str(artwork.get("style_version") or ""),
            )
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        object_key = str(artwork.get("object_key") or "")
        if not object_key:
            raise MemoryArtworkError("memory_artwork_object_missing", retryable=True)
        url = self.store_factory().signed_get_url(uid=uid, memory_id=memory_id, object_key=object_key)
        return {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "status": "ready",
            "style_version": artwork.get("style_version"),
            "enrichment_revision": artwork.get("enrichment_revision"),
            "content_type": artwork.get("content_type"),
            "pixel_width": artwork.get("pixel_width"),
            "pixel_height": artwork.get("pixel_height"),
            "url": url,
            "expires_in_seconds": 300,
        }

    async def backfill(self, uid: str) -> dict[str, Any]:
        if not (self.config.enabled and self.config.release_enabled and self.config.backfill_enabled):
            raise MemoryArtworkError("memory_artwork_backfill_disabled")
        recent = self.repository.list_recent_conversations(uid, limit=BACKFILL_SCAN_LIMIT)
        queued = existing = skipped = 0
        memory_ids: list[str] = []
        for conversation in recent:
            if len(memory_ids) >= MAX_BACKFILL_MEMORIES:
                break
            memory_id = str(conversation.get("id") or "")
            if not memory_id or _terminal_enrichment(conversation) is None:
                skipped += 1
                continue
            result = await self.enqueue(uid, memory_id)
            if result.get("outcome") == "reserved":
                queued += 1
                memory_ids.append(memory_id)
            elif result.get("outcome") == "existing":
                existing += 1
                memory_ids.append(memory_id)
            else:
                skipped += 1
        return {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "limit": MAX_BACKFILL_MEMORIES,
            "queued": queued,
            "existing": existing,
            "skipped": skipped,
            "memory_ids": memory_ids,
        }


class MemoryArtworkWorker:
    """Restart-safe consumer for content-free generation dispatch records."""

    def __init__(
        self,
        *,
        repository: Optional[MemoryArtworkRepository] = None,
        service_factory: Callable[[], MemoryArtworkService] = MemoryArtworkService,
        config: Optional[MemoryArtworkConfig] = None,
    ):
        self.repository = repository or FirestoreMemoryArtworkRepository()
        self.service_factory = service_factory
        self.config = config or MemoryArtworkConfig.from_env()

    async def run_once(self) -> int:
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            return 0
        processed = 0
        for job in self.repository.list_pending_jobs(limit=WORKER_BATCH_SIZE, now=datetime.now(timezone.utc)):
            uid = str(job.get("uid") or "")
            memory_id = str(job.get("memory_id") or "")
            generation_key = str(job.get("generation_key") or "")
            if not uid or not memory_id or len(generation_key) != 64:
                if uid and memory_id and generation_key:
                    self.repository.fail_job(
                        uid,
                        memory_id,
                        generation_key,
                        failure_code="memory_artwork_dispatch_invalid",
                    )
                continue
            service = self.service_factory()
            try:
                reservation = await service.enqueue(uid, memory_id, preserve_job_attempts=True)
                if reservation.get("status") == "ready":
                    self.repository.complete_job(uid, memory_id, generation_key)
                    processed += 1
                    continue
                if reservation.get("status") != "generating":
                    self.repository.fail_job(
                        uid,
                        memory_id,
                        generation_key,
                        failure_code=f"memory_artwork_{reservation.get('outcome') or 'dispatch_unavailable'}",
                    )
                    processed += 1
                    continue
                current = service.repository.get_conversation(uid, memory_id) or {}
                current_generation_key = str(((current.get("artwork") or {}).get("generation_key") or ""))
                if current_generation_key != generation_key:
                    # A correction reserved a newer generation. The newer
                    # transaction owns its own durable job.
                    self.repository.complete_job(uid, memory_id, generation_key)
                    processed += 1
                    continue
                result = await service.process(uid, memory_id)
                if result.get("status") == "ready":
                    self.repository.complete_job(uid, memory_id, generation_key)
                    processed += 1
            except MemoryArtworkError as exc:
                attempts = int(job.get("attempt_count") or 0) + 1
                if exc.retryable and attempts < WORKER_MAX_ATTEMPTS:
                    self.repository.retry_job(
                        uid,
                        memory_id,
                        generation_key,
                        attempt_count=attempts,
                        delay_seconds=min(300, 2**attempts),
                        failure_code=exc.code,
                    )
                else:
                    self.repository.fail_job(uid, memory_id, generation_key, failure_code=exc.code)
                processed += 1
            except Exception:
                attempts = int(job.get("attempt_count") or 0) + 1
                if attempts < WORKER_MAX_ATTEMPTS:
                    self.repository.retry_job(
                        uid,
                        memory_id,
                        generation_key,
                        attempt_count=attempts,
                        delay_seconds=min(300, 2**attempts),
                        failure_code="memory_artwork_worker_failed",
                    )
                else:
                    self.repository.fail_job(
                        uid,
                        memory_id,
                        generation_key,
                        failure_code="memory_artwork_worker_failed",
                    )
                processed += 1
        return processed


_worker_task: Optional[asyncio.Task] = None
_worker_stop: Optional[asyncio.Event] = None


async def _memory_artwork_worker_loop() -> None:
    assert _worker_stop is not None
    interval = max(1.0, float(os.getenv(WORKER_INTERVAL_SECONDS_ENV, "5")))
    worker = MemoryArtworkWorker()
    while not _worker_stop.is_set():
        try:
            processed = await worker.run_once()
        except Exception:
            processed = 0
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=0.1 if processed else interval)
        except asyncio.TimeoutError:
            continue


async def start_memory_artwork_worker() -> None:
    global _worker_stop, _worker_task
    config = MemoryArtworkConfig.from_env()
    if not (config.enabled and config.release_enabled and config.provider_enabled):
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_stop = asyncio.Event()
    _worker_task = asyncio.create_task(_memory_artwork_worker_loop(), name="ella-memory-artwork-worker")


async def stop_memory_artwork_worker() -> None:
    global _worker_stop, _worker_task
    if _worker_task is None:
        return
    assert _worker_stop is not None
    _worker_stop.set()
    await _worker_task
    _worker_task = None
    _worker_stop = None


async def enqueue_after_terminal_enrichment(uid: str, memory_id: str) -> None:
    """Durably queue work; image failures never change terminal text enrichment."""
    try:
        await MemoryArtworkService().enqueue(uid, memory_id)
    except Exception:
        return
