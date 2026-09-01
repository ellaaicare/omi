"""Versioned, owner-bound memory artwork generation contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import urlparse

import httpx

try:
    from PIL import Image, ImageOps, UnidentifiedImageError

    _PIL_AVAILABLE = True
    _IMAGE_DECODE_ERRORS = (Image.DecompressionBombError, OSError, UnidentifiedImageError)
except ModuleNotFoundError:
    Image = None
    ImageOps = None
    _PIL_AVAILABLE = False
    _IMAGE_DECODE_ERRORS = (OSError,)

import database.memory_artwork as artwork_db
from ella.services.ai_consent import CURRENT_POLICY_VERSION, get_ai_consent_service
from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_authority_identity
from utils.ella.memory_artwork_storage import (
    MAX_ARTWORK_BYTES,
    GCSMemoryArtworkStore,
    MemoryArtworkStorageError,
    StoredArtwork,
    acquire_memory_artwork_publication_lock,
)

ARTWORK_SCHEMA_VERSION = "ella.memory_artwork.v1"
ARTWORK_CONSENT_VERSION = CURRENT_POLICY_VERSION
ARTWORK_QUEUE_SCHEMA_VERSION = "ella.memory_artwork.queue.v1"
ARTWORK_LIBRARIES_SCHEMA_VERSION = "ella.memory_artwork.libraries.v1"
ARTWORK_PROMPT_CONTRACT_VERSION = "ella.memory_artwork.prompt.v2"
ARTWORK_PROVIDER_CONTRACT_VERSION = "ella.artwork.service.v1"
ARTWORK_RECONCILIATION_SCHEMA_VERSION = "ella.memory_artwork.reconciliation.v1"
DEFAULT_STYLE_VERSION = "ella.memory_artwork.style.soft-gouache.v1"
SUPPORTED_STYLE_VERSIONS = {
    DEFAULT_STYLE_VERSION,
    "ella.memory_artwork.style.paper-collage.v1",
    "ella.memory_artwork.style.graphic-landscape.v1",
    "ella.memory_artwork.style.watercolor-journal.v1",
    "ella.memory_artwork.style.anime-storybook.v1",
    "ella.memory_artwork.style.cinematic-still.v1",
}
STYLE_PROMPT_BRIEFS = {
    "ella.memory_artwork.style.soft-gouache.v1": (
        "A soft gouache editorial painting on lightly textured paper, with layered opaque brushwork, "
        "subtle grain, natural proportions, and restrained storybook detail."
    ),
    "ella.memory_artwork.style.paper-collage.v1": (
        "A refined cut-paper collage with visibly layered matte paper, crisp hand-cut silhouettes, gentle "
        "overlap shadows, and a limited tactile color palette."
    ),
    "ella.memory_artwork.style.graphic-landscape.v1": (
        "A bold graphic landscape illustration with simplified architectural or natural forms, clean shapes, "
        "confident negative space, and an editorial travel-poster sensibility."
    ),
    "ella.memory_artwork.style.watercolor-journal.v1": (
        "A luminous watercolor journal painting with transparent washes, selective crisp detail, soft paper texture, "
        "and color grounded only in the remembered scene."
    ),
    "ella.memory_artwork.style.anime-storybook.v1": (
        "A contemporary anime-inspired storybook illustration with expressive environmental storytelling, clean "
        "draftsmanship, gentle cinematic light, and no imitation of a named artist, franchise, or character."
    ),
    "ella.memory_artwork.style.cinematic-still.v1": (
        "A restrained cinematic editorial still with natural light, believable materials, quiet human gesture, and "
        "a composed widescreen sense of place without photorealistic identity detail."
    ),
}
DESIGNER_STYLE_NAMES = {
    "ella.memory_artwork.style.soft-gouache.v1": "gouache",
    "ella.memory_artwork.style.paper-collage.v1": "paper-collage",
    "ella.memory_artwork.style.graphic-landscape.v1": "graphic-landscape",
    "ella.memory_artwork.style.watercolor-journal.v1": "watercolor",
    "ella.memory_artwork.style.anime-storybook.v1": "anime-storybook",
    "ella.memory_artwork.style.cinematic-still.v1": "cinematic",
}

MemoryArtworkEnrichmentRecovery = Callable[[str, str], Awaitable[dict[str, Any]]]
_memory_artwork_enrichment_recovery: Optional[MemoryArtworkEnrichmentRecovery] = None


def register_memory_artwork_enrichment_recovery(handler: MemoryArtworkEnrichmentRecovery) -> None:
    """Register canonical enrichment recovery without introducing a service import cycle."""

    global _memory_artwork_enrichment_recovery
    _memory_artwork_enrichment_recovery = handler


COMPOSITION_DIRECTIONS = (
    "Use layered depth led by a concrete subject already named in the memory, adding no new foreground object.",
    "Use an intimate eye-level view, placing the most specific named subject near one third of the frame.",
    "Use a wide environmental framing that gives the named setting room around the primary remembered subject.",
    "Use an asymmetric editorial composition with one named subject in crisp focus and only stated context around it.",
    "Use a gently elevated viewpoint when it suits the stated activity; otherwise preserve the activity's natural viewpoint.",
    "Use a close observational composition built only from one or more concrete elements named in the summary.",
)
LIGHT_AND_PALETTE_DIRECTIONS = (
    "Honor every time, weather, and color cue stated in the memory; if none is stated, use soft neutral illumination "
    "and a restrained natural palette.",
    "Let the most specific named object supply the principal accent color; keep supporting colors quiet and do not "
    "invent a season, weather condition, or time of day.",
    "Match light direction and intensity to the stated setting and time; when either is absent, use diffuse neutral "
    "light and derive color only from named objects and places.",
    "Preserve every explicit warm, cool, indoor, outdoor, day, and night cue; when unspecified, avoid implying one "
    "and use balanced editorial color.",
    "Build the palette from concrete materials, objects, and surroundings in the memory, preserving any stated "
    "colors exactly and introducing no competing narrative color cue.",
    "Use contrast to clarify the remembered subject, while deriving illumination, atmosphere, and accent colors "
    "strictly from the supplied title and overview.",
)
TARGET_WIDTH = 1536
TARGET_HEIGHT = 1024
MAX_BACKFILL_MEMORIES = 10
BACKFILL_SCAN_LIMIT = 50
DEFAULT_PREVIEW_DAY_LIMIT = 3
MAX_BACKFILL_ENRICHMENT_RECOVERIES = 3
PROVIDER_TIMEOUT_SECONDS_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_TIMEOUT_SECONDS"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600.0
MIN_PROVIDER_TIMEOUT_SECONDS = 60.0
MAX_PROVIDER_TIMEOUT_SECONDS = 900.0
PROVIDER_LEASE_MARGIN_SECONDS = 300
PUBLICATION_LEASE_SECONDS = 600
PROVIDER_CONNECT_TIMEOUT_SECONDS = 10.0
PROVIDER_TOKEN_FILE_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_TOKEN_FILE"
PROVIDER_URL_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_URL"
PROVIDER_ALLOWED_HOST_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER_ALLOWED_HOST"
INTERNAL_OWNER_UIDS_ENV = "ELLA_MEMORY_ARTWORK_INTERNAL_OWNER_UIDS"
WORKER_INTERVAL_SECONDS_ENV = "ELLA_MEMORY_ARTWORK_WORKER_INTERVAL_SECONDS"
WORKER_BATCH_SIZE = 10
WORKER_MAX_ATTEMPTS = 5
WORKER_RETRY_DELAYS_SECONDS = (30, 120, 300, 900)
DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE = artwork_db.DEFAULT_BACKFILL_BATCH_SIZE
TERMINAL_ENRICHMENT_ORIGIN = artwork_db.TERMINAL_ENRICHMENT_ORIGIN
HISTORICAL_BACKFILL_ORIGIN = artwork_db.HISTORICAL_BACKFILL_ORIGIN
PREVIEW_BACKFILL_ORIGIN = artwork_db.PREVIEW_BACKFILL_ORIGIN
ENRICHMENT_RECOVERY_PENDING_OUTCOMES = frozenset({"claimed", "processing", "busy", "superseded"})
ENRICHMENT_RECOVERY_TERMINAL_OUTCOMES = frozenset({"not_found", "invalid_state", "not_retryable", "failed"})
PROVIDER_KIND_ENV = "ELLA_MEMORY_ARTWORK_PROVIDER"
XAI_API_KEY_ENV = "XAI_API_KEY"
XAI_IMAGE_MODEL_ENV = "ELLA_MEMORY_ARTWORK_XAI_MODEL"
XAI_IMAGE_ENDPOINT = "https://api.x.ai/v1/images/generations"
DEFAULT_XAI_IMAGE_MODEL = "grok-imagine-image-2.0"
MAX_PROVIDER_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_BASE64_ARTWORK_CHARS = ((MAX_ARTWORK_BYTES + 2) // 3) * 4

logger = logging.getLogger(__name__)


def _provider_timeout_seconds() -> float:
    raw_value = os.getenv(PROVIDER_TIMEOUT_SECONDS_ENV, str(DEFAULT_PROVIDER_TIMEOUT_SECONDS)).strip()
    try:
        timeout_seconds = float(raw_value)
    except ValueError:
        timeout_seconds = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    if not math.isfinite(timeout_seconds):
        timeout_seconds = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return min(MAX_PROVIDER_TIMEOUT_SECONDS, max(MIN_PROVIDER_TIMEOUT_SECONDS, timeout_seconds))


def _worker_retry_delay_seconds(attempts: int) -> int:
    index = min(max(attempts, 1), len(WORKER_RETRY_DELAYS_SECONDS)) - 1
    return WORKER_RETRY_DELAYS_SECONDS[index]


def _artwork_lease_seconds() -> int:
    return max(120, math.ceil(_provider_timeout_seconds()) + PROVIDER_LEASE_MARGIN_SECONDS)


class MemoryArtworkError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class MemoryArtworkBackfillError(MemoryArtworkError):
    def __init__(self, code: str, *, retryable: bool, partial_result: dict[str, Any]):
        super().__init__(code, retryable=retryable)
        self.partial_result = partial_result


@dataclass(frozen=True)
class ArtworkRuntimeAuthority:
    uid: str
    binding_id: str
    profile_id: str
    revision: int
    authority_digest: str


@dataclass(frozen=True)
class GeneratedArtwork:
    image_bytes: bytes
    content_type: str
    pixel_width: int
    pixel_height: int


@dataclass(frozen=True)
class ArtworkProviderContext:
    owner_uid: str
    profile_binding: str
    authority_generation: int
    source_revision: str
    consent_version: str
    title: str
    summary: str


@dataclass(frozen=True)
class MemoryArtworkConfig:
    enabled: bool
    release_enabled: bool
    provider_enabled: bool
    backfill_enabled: bool
    internal_owner_uids: Optional[frozenset[str]] = None

    @classmethod
    def from_env(cls) -> "MemoryArtworkConfig":
        enabled = os.getenv("ELLA_MEMORY_ARTWORK_ENABLED", "false").strip().lower() == "true"
        internal_owner_uids = frozenset(
            uid.strip() for uid in os.getenv(INTERNAL_OWNER_UIDS_ENV, "").split(",") if uid.strip()
        )
        return cls(
            enabled=enabled,
            release_enabled=os.getenv("ELLA_MEMORY_ARTWORK_RELEASE_ENABLED", "false").strip().lower() == "true",
            provider_enabled=os.getenv("ELLA_MEMORY_ARTWORK_PROVIDER_ENABLED", "false").strip().lower() == "true",
            backfill_enabled=os.getenv("ELLA_MEMORY_ARTWORK_BACKFILL_ENABLED", "false").strip().lower() == "true",
            internal_owner_uids=internal_owner_uids,
        )

    def allows_uid(self, uid: str) -> bool:
        # Injected test configs predate the owner gate; environment-derived
        # production configs always provide a set and fail closed when empty.
        return self.internal_owner_uids is None or uid in self.internal_owner_uids


class MemoryArtworkRepository(Protocol):
    def get_preferences(self, uid: str) -> dict[str, Any]: ...

    def set_preferences(self, uid: str, preferences: dict[str, Any], **kwargs) -> None: ...

    def get_backfill_control(self, uid: str) -> dict[str, Any]: ...

    def set_backfill_control(self, uid: str, **kwargs) -> dict[str, Any]: ...

    def list_jobs_for_uid(self, uid: str, *, migrate_legacy_jobs: bool = True) -> list[dict[str, Any]]: ...

    def get_conversation(self, uid: str, memory_id: str) -> Optional[dict[str, Any]]: ...

    def list_conversations_page(
        self,
        uid: str,
        *,
        limit: int,
        cursor_memory_id: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    def list_ready_artwork_conversations(self, uid: str) -> list[dict[str, Any]]: ...

    def reserve_generation(self, uid: str, memory_id: str, **kwargs) -> dict[str, Any]: ...

    def claim_generation(self, uid: str, memory_id: str, **kwargs) -> Optional[dict[str, Any]]: ...

    def finalize_generation(self, uid: str, memory_id: str, **kwargs) -> bool: ...

    def clear_published_artwork(self, uid: str, memory_id: str, **kwargs) -> bool: ...

    def mark_generation_unavailable(self, uid: str, memory_id: str, **kwargs) -> bool: ...

    def list_pending_jobs(self, **kwargs) -> list[dict[str, Any]]: ...

    def claim_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> Optional[dict[str, Any]]: ...

    def job_claim_is_current(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def complete_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def retry_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def fail_job(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def mark_storage_cleanup_required(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def renew_publication_claim(self, uid: str, memory_id: str, generation_key: str, **kwargs) -> bool: ...

    def create_reconciliation_job(self, uid: str, **kwargs) -> dict[str, Any]: ...

    def get_reconciliation_job(self, uid: str, job_id: str) -> Optional[dict[str, Any]]: ...

    def list_pending_reconciliation_jobs(self, **kwargs) -> list[dict[str, Any]]: ...

    def claim_reconciliation_job(self, uid: str, job_id: str, **kwargs) -> Optional[dict[str, Any]]: ...

    def finish_reconciliation_job(self, job_id: str, **kwargs) -> bool: ...


class FirestoreMemoryArtworkRepository:
    get_preferences = staticmethod(artwork_db.get_preferences)
    set_preferences = staticmethod(artwork_db.set_preferences)
    get_backfill_control = staticmethod(artwork_db.get_backfill_control)
    set_backfill_control = staticmethod(artwork_db.set_backfill_control)
    list_jobs_for_uid = staticmethod(artwork_db.list_jobs_for_uid)
    get_conversation = staticmethod(artwork_db.get_conversation)
    list_conversations_page = staticmethod(artwork_db.list_conversations_page)
    list_ready_artwork_conversations = staticmethod(artwork_db.list_ready_artwork_conversations)
    reserve_generation = staticmethod(artwork_db.reserve_generation)
    claim_generation = staticmethod(artwork_db.claim_generation)
    finalize_generation = staticmethod(artwork_db.finalize_generation)
    clear_published_artwork = staticmethod(artwork_db.clear_published_artwork)
    mark_generation_unavailable = staticmethod(artwork_db.mark_generation_unavailable)
    list_pending_jobs = staticmethod(artwork_db.list_pending_jobs)
    claim_job = staticmethod(artwork_db.claim_job)
    job_claim_is_current = staticmethod(artwork_db.job_claim_is_current)
    complete_job = staticmethod(artwork_db.complete_job)
    retry_job = staticmethod(artwork_db.retry_job)
    fail_job = staticmethod(artwork_db.fail_job)
    mark_storage_cleanup_required = staticmethod(artwork_db.mark_storage_cleanup_required)
    renew_publication_claim = staticmethod(artwork_db.renew_publication_claim)
    create_reconciliation_job = staticmethod(artwork_db.create_reconciliation_job)
    get_reconciliation_job = staticmethod(artwork_db.get_reconciliation_job)
    list_pending_reconciliation_jobs = staticmethod(artwork_db.list_pending_reconciliation_jobs)
    claim_reconciliation_job = staticmethod(artwork_db.claim_reconciliation_job)
    finish_reconciliation_job = staticmethod(artwork_db.finish_reconciliation_job)


class MemoryArtworkProvider(Protocol):
    async def generate(
        self,
        *,
        prompt: str,
        style_version: str,
        idempotency_key: str,
        context: Optional[ArtworkProviderContext] = None,
    ) -> GeneratedArtwork: ...


class MemoryArtworkStore(Protocol):
    def put(self, **kwargs) -> StoredArtwork: ...

    def signed_get_url(self, **kwargs) -> str: ...

    def delete(self, **kwargs) -> None: ...

    def delete_memory_prefix(self, **kwargs) -> int: ...


async def _bounded_provider_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_response_bytes: int,
    **kwargs,
) -> tuple[int, dict[str, str], bytes]:
    async with client.stream("POST", url, **kwargs) as response:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_response_bytes:
                    raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
            except ValueError as exc:
                raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False) from exc
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_response_bytes:
                raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
            body.extend(chunk)
        return response.status_code, dict(response.headers), bytes(body)


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
        self.timeout_seconds = _provider_timeout_seconds()

    async def generate(
        self,
        *,
        prompt: str,
        style_version: str,
        idempotency_key: str,
        context: Optional[ArtworkProviderContext] = None,
    ) -> GeneratedArtwork:
        if context is None:
            raise MemoryArtworkError("memory_artwork_provider_context_missing", retryable=False)
        designer_style = DESIGNER_STYLE_NAMES.get(style_version)
        if designer_style is None:
            raise MemoryArtworkError("memory_artwork_style_version_invalid", retryable=False)
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=PROVIDER_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                status_code, headers, image_bytes = await _bounded_provider_post(
                    client,
                    self.url,
                    max_response_bytes=MAX_ARTWORK_BYTES,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Idempotency-Key": idempotency_key,
                        "X-Ella-Contract": ARTWORK_PROVIDER_CONTRACT_VERSION,
                    },
                    json={
                        "schemaVersion": "ella.artwork.brief.v1",
                        "jobId": idempotency_key,
                        "ownerUid": context.owner_uid,
                        "profileBinding": context.profile_binding,
                        "authorityGeneration": context.authority_generation,
                        "sourceRevision": context.source_revision,
                        "consentVersion": context.consent_version,
                        "synthetic": False,
                        "style": designer_style,
                        "title": context.title,
                        "summary": context.summary,
                    },
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MemoryArtworkError("memory_artwork_provider_unavailable", retryable=True) from exc
        finally:
            if owns_client:
                await client.aclose()
        if status_code != 200:
            raise MemoryArtworkError("memory_artwork_provider_rejected", retryable=status_code >= 500)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            width = int(headers.get("x-ella-image-width", "0"))
            height = int(headers.get("x-ella-image-height", "0"))
        except ValueError as exc:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False) from exc
        if content_type not in {"image/png", "image/webp", "image/jpeg"} or width <= 0 or height <= 0:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
        return GeneratedArtwork(
            image_bytes=image_bytes,
            content_type=content_type,
            pixel_width=width,
            pixel_height=height,
        )


class XaiMemoryArtworkProvider:
    """Direct server-side xAI Imagine adapter with no vendor URL persistence."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None):
        self.api_key = os.getenv(XAI_API_KEY_ENV, "").strip()
        if not self.api_key:
            raise MemoryArtworkError("memory_artwork_provider_credential_unavailable", retryable=True)
        self.model = os.getenv(XAI_IMAGE_MODEL_ENV, DEFAULT_XAI_IMAGE_MODEL).strip()
        if not self.model:
            raise MemoryArtworkError("memory_artwork_provider_model_invalid", retryable=False)
        self.client = client
        self.timeout_seconds = _provider_timeout_seconds()

    @staticmethod
    def _normalize_image(image_bytes: bytes) -> bytes:
        if not _PIL_AVAILABLE:
            raise MemoryArtworkError("memory_artwork_image_codec_unavailable", retryable=False)
        if not image_bytes or len(image_bytes) > MAX_ARTWORK_BYTES:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > 24_000_000:
                    raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
                normalized = ImageOps.fit(
                    source.convert("RGB"),
                    (TARGET_WIDTH, TARGET_HEIGHT),
                    method=Image.Resampling.LANCZOS,
                )
                output = io.BytesIO()
                normalized.save(output, format="JPEG", quality=88, optimize=True)
                result = output.getvalue()
        except _IMAGE_DECODE_ERRORS as exc:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False) from exc
        if not result or len(result) > MAX_ARTWORK_BYTES:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False)
        return result

    async def generate(
        self,
        *,
        prompt: str,
        style_version: str,
        idempotency_key: str,
        context: Optional[ArtworkProviderContext] = None,
    ) -> GeneratedArtwork:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=PROVIDER_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                status_code, _, response_body = await _bounded_provider_post(
                    client,
                    XAI_IMAGE_ENDPOINT,
                    max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": idempotency_key,
                    },
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "n": 1,
                        "aspect_ratio": "3:2",
                        "resolution": "2k",
                        "quality": "low",
                        "response_format": "b64_json",
                    },
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MemoryArtworkError("memory_artwork_provider_unavailable", retryable=True) from exc
        finally:
            if owns_client:
                await client.aclose()
        if status_code != 200:
            raise MemoryArtworkError("memory_artwork_provider_rejected", retryable=status_code >= 500)
        try:
            payload = json.loads(response_body)
            item = payload["data"][0]
            encoded = item["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > MAX_BASE64_ARTWORK_CHARS:
                raise TypeError("invalid image payload")
            raw_image = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MemoryArtworkError("memory_artwork_provider_response_invalid", retryable=False) from exc
        normalized = self._normalize_image(raw_image)
        return GeneratedArtwork(
            image_bytes=normalized,
            content_type="image/jpeg",
            pixel_width=TARGET_WIDTH,
            pixel_height=TARGET_HEIGHT,
        )


def memory_artwork_provider_factory() -> MemoryArtworkProvider:
    provider_kind = os.getenv(PROVIDER_KIND_ENV, "first_party_adapter").strip().lower()
    if provider_kind == "xai":
        return XaiMemoryArtworkProvider()
    if provider_kind == "first_party_adapter":
        return FirstPartyHTTPArtworkProvider()
    raise MemoryArtworkError("memory_artwork_provider_kind_invalid", retryable=False)


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
        revision=runtime.revision,
        authority_digest=identity.digest,
    )


def _terminal_enrichment(conversation: dict[str, Any]) -> Optional[str]:
    enrichment = conversation.get("enrichment_state") or {}
    revision = str(conversation.get("active_summary_version_id") or "").strip()
    if not revision or not isinstance(enrichment, dict):
        return None
    if (
        not conversation.get("deletion_pending")
        and not conversation.get("discarded")
        and conversation.get("status") == "completed"
        and enrichment.get("status") == "writeback_applied"
        and enrichment.get("kind")
        in {"observer_enriched", "corrected_enriched", "hermes_enriched", "recovered_enriched"}
    ):
        return revision
    return None


def _conversation_day(conversation: dict[str, Any]) -> str:
    for field in ("started_at", "created_at"):
        value = conversation.get(field)
        if isinstance(value, datetime):
            normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return normalized.astimezone(timezone.utc).date().isoformat()
    return ""


def _source_is_sensitive(conversation: dict[str, Any]) -> bool:
    assessment = conversation.get("internal_assessment") or {}
    signal = conversation.get("ella_signal") or {}
    tags = {str(value).strip().lower() for value in (conversation.get("ella_tags") or [])}
    sensitive_tags = {"caregiver-private", "safety", "distress", "emergency", "self-harm"}
    risk = str(assessment.get("risk_level") or "").strip().lower() if isinstance(assessment, dict) else ""
    guardian_relevant = bool(signal.get("guardian_relevant")) if isinstance(signal, dict) else False
    return bool(tags & sensitive_tags or guardian_relevant or risk in {"medium", "high", "critical"})


def _prompt_for(conversation: dict[str, Any], style_version: str) -> tuple[str, str]:
    style_brief = STYLE_PROMPT_BRIEFS.get(style_version)
    if style_brief is None:
        raise MemoryArtworkError("memory_artwork_style_version_invalid", retryable=False)
    structured = conversation.get("structured") or {}
    title = " ".join(str(structured.get("title") or "").split())[:240]
    overview = " ".join(str(structured.get("overview") or "").split())[:1200]
    if not title and not overview:
        raise MemoryArtworkError("memory_artwork_summary_missing", retryable=False)

    variation_material = "\n".join(
        (
            str(conversation.get("id") or conversation.get("conversation_id") or ""),
            style_version,
            title,
            overview,
        )
    )
    variation_digest = hashlib.sha256(variation_material.encode("utf-8")).digest()
    composition = COMPOSITION_DIRECTIONS[variation_digest[0] % len(COMPOSITION_DIRECTIONS)]
    light_and_palette = LIGHT_AND_PALETTE_DIRECTIONS[variation_digest[1] % len(LIGHT_AND_PALETTE_DIRECTIONS)]
    prompt = (
        f"Prompt contract: {ARTWORK_PROMPT_CONTRACT_VERSION}. "
        "Create a semantically specific 3:2 editorial illustration based only on the supplied memory title and overview. "
        "Make the concrete place, activity, objects, time, weather, and colors actually named in the memory the primary "
        "visual evidence. Fill the full frame edge to edge with a complete scene rather than a small icon or centered vignette. "
        "Do not default to a generic family gathering, generic living room, generic sunset, generic skyline, or repeated stock "
        "composition unless the memory explicitly supports it. If people are needed, keep them unidentifiable and secondary "
        "to the remembered action and setting. Do not show identifiable faces, names, readable text, logos, medical conditions, "
        "or any fact not present in the memory. "
        f"Style contract: {style_version}. Style direction: {style_brief} "
        f"Composition direction: {composition} "
        f"Light and palette direction: {light_and_palette} "
        f"Memory title: {title or 'Untitled memory'}. Memory overview: {overview or title}."
    )
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _provider_context(
    *,
    uid: str,
    authority: ArtworkRuntimeAuthority,
    conversation: dict[str, Any],
    source_revision: str,
) -> ArtworkProviderContext:
    structured = conversation.get("structured") or {}
    title = " ".join(str(structured.get("title") or "").split())[:240]
    summary = " ".join(str(structured.get("overview") or title).split())[:1800]
    if not title and not summary:
        raise MemoryArtworkError("memory_artwork_summary_missing", retryable=False)
    if authority.revision < 1:
        raise MemoryArtworkError("memory_artwork_authority_generation_invalid", retryable=False)
    return ArtworkProviderContext(
        owner_uid=uid,
        profile_binding=authority.profile_id,
        authority_generation=authority.revision,
        source_revision=source_revision,
        consent_version=ARTWORK_CONSENT_VERSION,
        title=title or "Untitled memory",
        summary=summary or title,
    )


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
        and not preferences.get(artwork_db.DELETION_PENDING_FIELD)
        and preferences.get("binding_id") == authority.binding_id
        and preferences.get("profile_id") == authority.profile_id
        and preferences.get("authority_digest") == authority.authority_digest
        and (style_version is None or preferences.get("style_version") == style_version)
    )


def _release_artwork(conversation: dict[str, Any]) -> tuple[dict[str, Any], bool, Optional[str]]:
    current = conversation.get("artwork") or {}
    if isinstance(current, dict) and current.get("status") == "ready":
        return current, False, None
    published = conversation.get(artwork_db.PUBLISHED_ARTWORK_FIELD) or {}
    if isinstance(published, dict) and published.get("status") == "ready":
        current_status = str(current.get("status") or "") if isinstance(current, dict) else ""
        failure_code = str(current.get("failure_code") or "") if isinstance(current, dict) else ""
        return published, current_status == "generating", failure_code or None
    return current if isinstance(current, dict) else {}, False, None


def _inventory_release_artwork(
    conversation: dict[str, Any],
    *,
    preferences: dict[str, Any],
    authority: ArtworkRuntimeAuthority,
) -> Optional[dict[str, Any]]:
    """Return the artwork the signed URL path can serve from this snapshot."""

    if conversation.get("deletion_pending") or conversation.get("discarded") or _source_is_sensitive(conversation):
        return None
    artwork, refresh_pending, refresh_failure_code = _release_artwork(conversation)
    if not isinstance(artwork, dict) or artwork.get("status") != "ready" or not artwork.get("object_key"):
        return None
    if (
        artwork.get("authority_digest") != authority.authority_digest
        or artwork.get("binding_id") != authority.binding_id
        or artwork.get("profile_id") != authority.profile_id
        or not _preferences_match_authority(
            preferences,
            authority,
            style_version=None if refresh_pending or refresh_failure_code else str(artwork.get("style_version") or ""),
        )
    ):
        return None
    if refresh_pending or refresh_failure_code:
        generation_request = conversation.get("artwork") or {}
        if (
            not isinstance(generation_request, dict)
            or generation_request.get("style_version") != preferences.get("style_version")
            or generation_request.get("enrichment_revision") != artwork.get("enrichment_revision")
        ):
            return None
    enrichment_revision = _terminal_enrichment(conversation)
    try:
        _, prompt_sha256 = _prompt_for(conversation, str(artwork.get("style_version") or ""))
    except MemoryArtworkError:
        return None
    if enrichment_revision != artwork.get("enrichment_revision") or prompt_sha256 != artwork.get("prompt_sha256"):
        return None
    return artwork


def _generation_claim_is_current(
    conversation: dict[str, Any],
    *,
    generation_key: str,
    lease_token: str,
    now: datetime,
) -> bool:
    artwork = conversation.get("artwork") or {}
    lease_expires_at = artwork.get("lease_expires_at") if isinstance(artwork, dict) else None
    return bool(
        not conversation.get("deletion_pending")
        and not conversation.get("discarded")
        and isinstance(artwork, dict)
        and artwork.get("status") == "generating"
        and artwork.get("generation_key") == generation_key
        and artwork.get("lease_token") == lease_token
        and isinstance(lease_expires_at, datetime)
        and lease_expires_at > now
    )


def has_current_global_ai_consent(uid: str) -> bool:
    try:
        status = get_ai_consent_service().status(uid)
    except Exception:
        return False
    return status.get("authorized") is True


class MemoryArtworkService:
    def __init__(
        self,
        *,
        repository: Optional[MemoryArtworkRepository] = None,
        authority_resolver: Callable[[str], Awaitable[ArtworkRuntimeAuthority]] = resolve_memory_artwork_authority,
        provider_factory: Optional[Callable[[], MemoryArtworkProvider]] = None,
        store_factory: Callable[[], MemoryArtworkStore] = GCSMemoryArtworkStore,
        global_consent_checker: Optional[Callable[[str], bool]] = None,
        config: Optional[MemoryArtworkConfig] = None,
    ):
        self.repository = repository or FirestoreMemoryArtworkRepository()
        self.authority_resolver = authority_resolver
        self.provider_factory = provider_factory or memory_artwork_provider_factory
        self.store_factory = store_factory
        self.global_consent_checker = global_consent_checker or has_current_global_ai_consent
        self.config = config or MemoryArtworkConfig.from_env()

    async def _cleanup_published_artwork(self, uid: str, memory_id: str, *, required: bool) -> bool:
        try:
            async with acquire_memory_artwork_publication_lock(uid):
                conversation = self.repository.get_conversation(uid, memory_id) or {}
                current = conversation.get(artwork_db.ARTWORK_FIELD) or {}
                published = conversation.get(artwork_db.PUBLISHED_ARTWORK_FIELD) or {}
                if not isinstance(published, dict) or not published:
                    return True
                if not isinstance(current, dict) or current.get("status") != "ready":
                    return True
                object_key = str(published.get("object_key") or "")
                object_generation = str(published.get("object_generation") or "")
                if not object_key:
                    raise MemoryArtworkStorageError("memory_artwork_published_object_missing")
                if object_key != str(current.get("object_key") or ""):
                    self.store_factory().delete(uid=uid, memory_id=memory_id, object_key=object_key)
                if not self.repository.clear_published_artwork(
                    uid,
                    memory_id,
                    object_key=object_key,
                    object_generation=object_generation,
                ):
                    raise MemoryArtworkStorageError("memory_artwork_published_cleanup_conflict")
                return True
        except Exception as exc:
            if required:
                if isinstance(exc, MemoryArtworkError):
                    raise
                raise MemoryArtworkError("memory_artwork_published_cleanup_failed", retryable=True) from exc
            logger.warning("Memory artwork published-object cleanup remains pending: %s", type(exc).__name__)
            return False

    async def preferences(self, uid: str) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "consent_version": ARTWORK_CONSENT_VERSION,
                "consent": "not_set",
                "style_version": DEFAULT_STYLE_VERSION,
                "supported_style_versions": sorted(SUPPORTED_STYLE_VERSIONS),
                "release_enabled": False,
            }
        stored = self.repository.get_preferences(uid)
        consent = str(stored.get("consent") or "not_set")
        style = str(stored.get("style_version") or DEFAULT_STYLE_VERSION)
        return {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "consent_version": ARTWORK_CONSENT_VERSION,
            "consent": consent if consent in {"accepted", "declined"} else "not_set",
            "style_version": style if style in SUPPORTED_STYLE_VERSIONS else DEFAULT_STYLE_VERSION,
            "supported_style_versions": sorted(SUPPORTED_STYLE_VERSIONS),
            "release_enabled": self.config.enabled and self.config.release_enabled and self.config.allows_uid(uid),
        }

    async def set_preferences(self, uid: str, *, consent: str, consent_version: str, style_version: str) -> dict:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
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
            backfill_control_state="running" if consent == "accepted" else "cancelled",
        )
        return await self.preferences(uid)

    @staticmethod
    def _public_reconciliation(job: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not job:
            return {
                "schema_version": ARTWORK_RECONCILIATION_SCHEMA_VERSION,
                "status": "idle",
                "pages_processed": 0,
                "scanned": 0,
                "queued": 0,
                "existing": 0,
                "skipped": 0,
            }
        return {
            "schema_version": ARTWORK_RECONCILIATION_SCHEMA_VERSION,
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or "idle"),
            "style_version": str(job.get("style_version") or ""),
            "pages_processed": int(job.get("pages_processed") or 0),
            "scanned": int(job.get("scanned") or 0),
            "queued": int(job.get("queued") or 0),
            "existing": int(job.get("existing") or 0),
            "skipped": int(job.get("skipped") or 0),
            "failure_code": str(job.get("failure_code") or "") or None,
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
        }

    async def start_reconciliation(self, uid: str) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        if not (
            self.config.enabled
            and self.config.release_enabled
            and self.config.provider_enabled
            and self.config.backfill_enabled
        ):
            raise MemoryArtworkError("memory_artwork_backfill_disabled")
        authority = await self.authority_resolver(uid)
        preferences = self.repository.get_preferences(uid)
        style_version = str(preferences.get("style_version") or "")
        if style_version not in SUPPORTED_STYLE_VERSIONS:
            raise MemoryArtworkError("memory_artwork_style_version_invalid")
        if not self.global_consent_checker(uid) or not _preferences_match_authority(
            preferences,
            authority,
            style_version=style_version,
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        reservation = self.repository.create_reconciliation_job(
            uid,
            authority_digest=authority.authority_digest,
            style_version=style_version,
        )
        if reservation.get("outcome") == "deletion_pending":
            raise MemoryArtworkError("memory_artwork_deletion_pending")
        return self._public_reconciliation(reservation.get("job"))

    async def reconciliation_status(self, uid: str) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        authority = await self.authority_resolver(uid)
        preferences = self.repository.get_preferences(uid)
        style_version = str(preferences.get("style_version") or "")
        if not self.global_consent_checker(uid) or not _preferences_match_authority(
            preferences,
            authority,
            style_version=style_version,
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        job_id = artwork_db.reconciliation_job_id(uid, authority.authority_digest, style_version)
        return self._public_reconciliation(self.repository.get_reconciliation_job(uid, job_id))

    @staticmethod
    def _queue_counts(jobs: list[dict[str, Any]], *, authority_digest: str, style_version: str) -> dict[str, int]:
        counts = {"ready": 0, "active": 0, "queued": 0, "retrying": 0, "failed": 0}
        now = datetime.now(timezone.utc)
        for job in jobs:
            if job.get("authority_digest") != authority_digest or job.get("style_version") != style_version:
                continue
            status = str(job.get("status") or "")
            attempts = int(job.get("attempt_count") or 0)
            if status == "completed":
                counts["ready"] += 1
            elif status == "processing":
                lease_expires_at = job.get("lease_expires_at")
                if isinstance(lease_expires_at, datetime) and lease_expires_at > now:
                    counts["active"] += 1
                else:
                    counts["retrying" if attempts else "queued"] += 1
            elif status == "pending":
                counts["retrying" if attempts else "queued"] += 1
            elif status == "failed":
                counts["failed"] += 1
        counts["total"] = sum(counts.values())
        counts["remaining"] = counts["active"] + counts["queued"] + counts["retrying"] + counts["failed"]
        return counts

    async def queue_status(self, uid: str) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        authority = await self.authority_resolver(uid)
        preferences = self.repository.get_preferences(uid)
        style_version = str(preferences.get("style_version") or "")
        if not self.global_consent_checker(uid) or not _preferences_match_authority(
            preferences,
            authority,
            style_version=style_version,
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        generation_id = artwork_db.reconciliation_job_id(uid, authority.authority_digest, style_version)
        control = self.repository.get_backfill_control(uid)
        control_is_current = bool(
            control.get("generation_id") == generation_id
            and control.get("authority_digest") == authority.authority_digest
            and control.get("style_version") == style_version
            and control.get("state") in {"running", "paused", "cancelled"}
        )
        if control_is_current and bool(control.get("auto_continue")):
            update = self.repository.set_backfill_control(
                uid,
                expected_generation_id=generation_id,
                state="paused",
                auto_continue=False,
            )
            if update.get("outcome") == "updated":
                control = update.get("control") or {}
        state = str(control.get("state")) if control_is_current else "running"
        auto_continue = bool(control.get("auto_continue")) if control_is_current else False
        batch_size = int(control.get("batch_size") or DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE)
        batch_remaining = int(control.get("batch_remaining", batch_size) or 0) if control_is_current else batch_size
        pause_reason = str(control.get("pause_reason") or "") if control_is_current else ""
        # Queue status is a read path. Legacy migration can fan out into one
        # conversation read per unattributable job and push this endpoint past
        # the mobile client's timeout. Migration remains available to explicit
        # maintenance callers; status excludes owner/style-less legacy rows.
        jobs = self.repository.list_jobs_for_uid(uid, migrate_legacy_jobs=False)
        current_counts = self._queue_counts(
            jobs,
            authority_digest=authority.authority_digest,
            style_version=style_version,
        )
        reconciliation = self.repository.get_reconciliation_job(uid, generation_id)
        scan_status = str((reconciliation or {}).get("status") or "idle")
        style_progress = []
        for supported_style in sorted(SUPPORTED_STYLE_VERSIONS):
            counts = self._queue_counts(
                jobs,
                authority_digest=authority.authority_digest,
                style_version=supported_style,
            )
            if supported_style == style_version or counts["total"] > 0:
                style_progress.append(
                    {
                        "style_version": supported_style,
                        "state": state if supported_style == style_version else "paused",
                        **counts,
                    }
                )
        effective_state = state
        if state == "running" and scan_status == "completed" and current_counts["remaining"] == 0:
            effective_state = "completed"
        elif state == "running" and current_counts["failed"] > 0:
            effective_state = "needs_attention"
        return {
            "schema_version": ARTWORK_QUEUE_SCHEMA_VERSION,
            "generation_id": generation_id,
            "style_version": style_version,
            "state": effective_state,
            "control_state": state,
            "auto_continue": auto_continue,
            "batch_size": batch_size,
            "batch_remaining": batch_remaining,
            "pause_reason": pause_reason,
            "scan_status": scan_status,
            "scanned": int((reconciliation or {}).get("scanned") or 0),
            "pages_processed": int((reconciliation or {}).get("pages_processed") or 0),
            **current_counts,
            "styles": style_progress,
            "updated_at": max(
                [
                    value
                    for value in (
                        control.get("updated_at") if control_is_current else None,
                        (reconciliation or {}).get("updated_at"),
                        *[job.get("updated_at") for job in jobs],
                    )
                    if isinstance(value, datetime)
                ],
                default=None,
            ),
        }

    async def libraries(self, uid: str) -> dict[str, Any]:
        """Describe artwork objects that are actually available, not historical job totals."""

        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        authority = await self.authority_resolver(uid)
        preferences = self.repository.get_preferences(uid)
        selected_style = str(preferences.get("style_version") or "")
        if not self.global_consent_checker(uid) or not _preferences_match_authority(
            preferences,
            authority,
            style_version=selected_style,
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")

        memories_by_style: dict[str, set[str]] = {style: set() for style in SUPPORTED_STYLE_VERSIONS}
        days_by_style: dict[str, set[str]] = {style: set() for style in SUPPORTED_STYLE_VERSIONS}
        for conversation in self.repository.list_ready_artwork_conversations(uid):
            memory_id = str(conversation.get("id") or "")
            if not memory_id:
                continue
            day = _conversation_day(conversation)
            artwork = _inventory_release_artwork(conversation, preferences=preferences, authority=authority)
            style_version = str((artwork or {}).get("style_version") or "")
            if style_version not in SUPPORTED_STYLE_VERSIONS:
                continue
            memories_by_style[style_version].add(memory_id)
            if day:
                days_by_style[style_version].add(day)

        libraries = []
        for style_version in sorted(SUPPORTED_STYLE_VERSIONS):
            days = sorted(days_by_style[style_version])
            libraries.append(
                {
                    "style_version": style_version,
                    "selected": style_version == selected_style,
                    "ready_memories": len(memories_by_style[style_version]),
                    "ready_days": len(days),
                    "oldest_day": days[0] if days else None,
                    "newest_day": days[-1] if days else None,
                }
            )
        return {
            "schema_version": ARTWORK_LIBRARIES_SCHEMA_VERSION,
            "selected_style_version": selected_style,
            "default_preview_days": DEFAULT_PREVIEW_DAY_LIMIT,
            "historical_batch_size": DEFAULT_HISTORICAL_BACKFILL_BATCH_SIZE,
            "libraries": libraries,
        }

    async def set_queue_control(
        self,
        uid: str,
        *,
        action: str,
        generation_id: str,
        auto_continue: bool = False,
    ) -> dict[str, Any]:
        states = {"pause": "paused", "resume": "running", "cancel": "cancelled"}
        state = states.get(action)
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        if state is None or len(generation_id) != 64 or any(char not in "0123456789abcdef" for char in generation_id):
            raise MemoryArtworkError("memory_artwork_queue_control_invalid")
        authority = await self.authority_resolver(uid)
        preferences = self.repository.get_preferences(uid)
        style_version = str(preferences.get("style_version") or "")
        expected_generation_id = artwork_db.reconciliation_job_id(uid, authority.authority_digest, style_version)
        if generation_id != expected_generation_id:
            raise MemoryArtworkError("memory_artwork_queue_generation_stale")
        if not self.global_consent_checker(uid) or not _preferences_match_authority(
            preferences,
            authority,
            style_version=style_version,
        ):
            raise MemoryArtworkError("memory_artwork_preference_authority_stale")
        update = self.repository.set_backfill_control(
            uid,
            expected_generation_id=generation_id,
            state=state,
            auto_continue=False,
        )
        outcome = str(update.get("outcome") or "")
        if outcome == "deletion_pending":
            raise MemoryArtworkError("memory_artwork_deletion_pending")
        if outcome != "updated":
            raise MemoryArtworkError("memory_artwork_queue_generation_stale")
        reconciliation = self.repository.get_reconciliation_job(uid, generation_id)
        if action == "resume" and str((reconciliation or {}).get("status") or "") == "failed":
            self.repository.create_reconciliation_job(
                uid,
                authority_digest=authority.authority_digest,
                style_version=style_version,
            )
        return await self.queue_status(uid)

    async def enqueue(
        self,
        uid: str,
        memory_id: str,
        *,
        origin: str = TERMINAL_ENRICHMENT_ORIGIN,
        preserve_job_attempts: bool = False,
    ) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            return {"outcome": "disabled", "status": "unavailable"}
        if origin not in {
            TERMINAL_ENRICHMENT_ORIGIN,
            HISTORICAL_BACKFILL_ORIGIN,
            PREVIEW_BACKFILL_ORIGIN,
        }:
            raise MemoryArtworkError("memory_artwork_job_origin_invalid")
        conversation = self.repository.get_conversation(uid, memory_id)
        if conversation is None:
            raise MemoryArtworkError("memory_artwork_memory_not_found")
        current_artwork = conversation.get(artwork_db.ARTWORK_FIELD) or {}
        published_artwork = conversation.get(artwork_db.PUBLISHED_ARTWORK_FIELD) or {}
        if (
            isinstance(current_artwork, dict)
            and current_artwork.get("status") == "ready"
            and isinstance(published_artwork, dict)
            and published_artwork
        ):
            await self._cleanup_published_artwork(uid, memory_id, required=True)
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
        if not self.global_consent_checker(uid):
            return {"outcome": "consent_required", "status": "unavailable"}
        if _source_is_sensitive(conversation):
            return {"outcome": "sensitive_source_excluded", "status": "unavailable"}
        authority = await self.authority_resolver(uid)
        if not preferences:
            self.repository.set_preferences(
                uid,
                {
                    "schema_version": ARTWORK_SCHEMA_VERSION,
                    "consent": "accepted",
                    "consent_version": ARTWORK_CONSENT_VERSION,
                    "style_version": DEFAULT_STYLE_VERSION,
                    "binding_id": authority.binding_id,
                    "profile_id": authority.profile_id,
                    "authority_digest": authority.authority_digest,
                    "updated_at": datetime.now(timezone.utc),
                },
                backfill_control_state="running",
            )
            preferences = self.repository.get_preferences(uid)
        if preferences.get("consent") != "accepted" or preferences.get("consent_version") != ARTWORK_CONSENT_VERSION:
            return {"outcome": "consent_required", "status": "unavailable"}
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
            "authority_digest": authority.authority_digest,
            "style_version": style_version,
            "origin": origin,
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
        if reservation.get("outcome") == "deletion_pending":
            raise MemoryArtworkError("memory_artwork_deletion_pending")
        return {
            "outcome": reservation.get("outcome"),
            "status": str((reservation.get("artwork") or artwork_state).get("status") or "generating"),
        }

    async def process(
        self,
        uid: str,
        memory_id: str,
        *,
        generation_key: str,
        job_lease_token: str,
    ) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            raise MemoryArtworkError("memory_artwork_generation_disabled")
        queued = self.repository.get_conversation(uid, memory_id)
        artwork = (queued or {}).get("artwork") or {}
        queued_generation_key = str(artwork.get("generation_key") or "") if isinstance(artwork, dict) else ""
        if not generation_key or queued_generation_key != generation_key:
            raise MemoryArtworkError("memory_artwork_generation_not_queued")
        if not self.repository.job_claim_is_current(
            uid,
            memory_id,
            generation_key,
            lease_token=job_lease_token,
            now=datetime.now(timezone.utc),
        ):
            raise MemoryArtworkError("memory_artwork_job_claim_invalid")
        lease_token = secrets.token_hex(24)
        claimed = self.repository.claim_generation(
            uid,
            memory_id,
            generation_key=generation_key,
            lease_token=lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=_artwork_lease_seconds(),
        )
        if claimed is None:
            return {"outcome": "not_claimed", "status": str(artwork.get("status") or "unavailable")}

        def reject_claim_drift(conversation: dict[str, Any]) -> None:
            now = datetime.now(timezone.utc)
            if self.repository.job_claim_is_current(
                uid,
                memory_id,
                generation_key,
                lease_token=job_lease_token,
                now=now,
            ) and _generation_claim_is_current(
                conversation,
                generation_key=generation_key,
                lease_token=lease_token,
                now=now,
            ):
                return
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="job_claim_invalid",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_job_claim_invalid", retryable=True)

        def reject_deletion_pending(preferences: dict[str, Any]) -> None:
            if not preferences.get(artwork_db.DELETION_PENDING_FIELD):
                return
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="deletion_pending",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_deletion_pending")

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
        if not self.global_consent_checker(uid):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="memory_artwork_consent_required",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_consent_required")
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
        reject_deletion_pending(preferences)
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
        reject_deletion_pending(egress_preferences)
        if (
            not self.global_consent_checker(uid)
            or egress_authority != authority
            or not _preferences_match_authority(
                egress_preferences,
                egress_authority,
                style_version=str(claimed.get("style_version") or ""),
            )
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
        reject_claim_drift(egress_conversation)
        provider = self.provider_factory()
        try:
            generated = await provider.generate(
                prompt=prompt,
                style_version=str(claimed.get("style_version") or ""),
                idempotency_key=generation_key,
                context=_provider_context(
                    uid=uid,
                    authority=authority,
                    conversation=egress_conversation,
                    source_revision=str(claimed.get("enrichment_revision") or ""),
                ),
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
        latest_conversation = self.repository.get_conversation(uid, memory_id) or {}
        reject_deletion_pending(latest_preferences)
        if (
            not self.global_consent_checker(uid)
            or latest_authority != authority
            or not _preferences_match_authority(
                latest_preferences,
                latest_authority,
                style_version=str(claimed.get("style_version") or ""),
            )
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="authority_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_authority_changed")
        reject_claim_drift(latest_conversation)
        if _terminal_enrichment(latest_conversation) != claimed.get("enrichment_revision") or _source_is_sensitive(
            latest_conversation
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="source_changed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_source_changed")
        try:
            async with acquire_memory_artwork_publication_lock(uid):
                # Renew both durable claims and persist cleanup intent while the
                # crash-released distributed lock excludes destructive cleanup.
                if not self.repository.renew_publication_claim(
                    uid,
                    memory_id,
                    generation_key,
                    generation_lease_token=lease_token,
                    job_lease_token=job_lease_token,
                    now=datetime.now(timezone.utc),
                    lease_seconds=PUBLICATION_LEASE_SECONDS,
                ):
                    self.repository.mark_generation_unavailable(
                        uid,
                        memory_id,
                        generation_key=generation_key,
                        failure_code="deletion_pending",
                        lease_token=lease_token,
                    )
                    raise MemoryArtworkError("memory_artwork_deletion_pending")
                upload_preferences = self.repository.get_preferences(uid)
                reject_deletion_pending(upload_preferences)
                upload_conversation = self.repository.get_conversation(uid, memory_id) or {}
                reject_claim_drift(upload_conversation)
                store = self.store_factory()
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
        except MemoryArtworkError:
            raise
        except Exception as exc:
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code="memory_artwork_storage_failed",
                lease_token=lease_token,
            )
            raise MemoryArtworkError("memory_artwork_storage_failed", retryable=True) from exc
        # The object key is deterministic for this generation and can be shared
        # by an outcome-ambiguous retry. Workers therefore never delete it after
        # upload; the persisted cleanup marker delegates destructive cleanup to
        # the explicit memory/account deletion paths.
        generated_width = generated.pixel_width
        generated_height = generated.pixel_height
        del generated
        try:
            final_authority = await self.authority_resolver(uid)
            final_preferences = self.repository.get_preferences(uid)
            final_conversation = self.repository.get_conversation(uid, memory_id) or {}
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
        final_now = datetime.now(timezone.utc)
        final_claim_is_current = self.repository.job_claim_is_current(
            uid,
            memory_id,
            generation_key,
            lease_token=job_lease_token,
            now=final_now,
        ) and _generation_claim_is_current(
            final_conversation,
            generation_key=generation_key,
            lease_token=lease_token,
            now=final_now,
        )
        final_source_is_current = _terminal_enrichment(final_conversation) == claimed.get(
            "enrichment_revision"
        ) and not _source_is_sensitive(final_conversation)
        if (
            not self.global_consent_checker(uid)
            or final_authority != authority
            or not final_claim_is_current
            or not final_source_is_current
            or not _preferences_match_authority(
                final_preferences,
                final_authority,
                style_version=str(claimed.get("style_version") or ""),
            )
        ):
            self.repository.mark_generation_unavailable(
                uid,
                memory_id,
                generation_key=generation_key,
                failure_code=(
                    "deletion_pending"
                    if final_preferences.get(artwork_db.DELETION_PENDING_FIELD)
                    else (
                        "job_claim_invalid"
                        if not final_claim_is_current
                        else "source_changed" if not final_source_is_current else "authority_changed"
                    )
                ),
                lease_token=lease_token,
            )
            raise MemoryArtworkError(
                "memory_artwork_deletion_pending"
                if final_preferences.get(artwork_db.DELETION_PENDING_FIELD)
                else (
                    "memory_artwork_job_claim_invalid"
                    if not final_claim_is_current
                    else (
                        "memory_artwork_source_changed"
                        if not final_source_is_current
                        else "memory_artwork_authority_changed"
                    )
                )
            )
        ready_state = {
            **claimed,
            "status": "ready",
            "object_key": stored.object_key,
            "object_generation": stored.object_generation,
            "content_type": stored.content_type,
            "byte_size": stored.byte_size,
            "pixel_width": generated_width,
            "pixel_height": generated_height,
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
            raise MemoryArtworkError("memory_artwork_finalize_conflict", retryable=True)
        await self._cleanup_published_artwork(uid, memory_id, required=False)
        return {"outcome": "ready", "status": "ready"}

    async def signed_url(self, uid: str, memory_id: str) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_internal_owner_required",
            }
        if not (self.config.enabled and self.config.release_enabled):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_release_disabled",
            }
        conversation = self.repository.get_conversation(uid, memory_id)
        if conversation is None:
            raise MemoryArtworkError("memory_artwork_memory_not_found")
        if conversation.get("discarded"):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_discarded",
            }
        artwork, refresh_pending, refresh_failure_code = _release_artwork(conversation)
        status = str(artwork.get("status") or "unavailable") if isinstance(artwork, dict) else "unavailable"
        if status != "ready":
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": status if status in {"generating", "unavailable", "declined"} else "unavailable",
                "failure_code": artwork.get("failure_code") if isinstance(artwork, dict) else None,
            }
        if _source_is_sensitive(conversation):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_sensitive_source_excluded",
            }
        authority = await self.authority_resolver(uid)
        # Re-read consent and artwork after the awaited authority lookup. There
        # is no await between this snapshot and the synchronous signer call.
        preferences = self.repository.get_preferences(uid)
        conversation = self.repository.get_conversation(uid, memory_id)
        if conversation is None:
            raise MemoryArtworkError("memory_artwork_memory_not_found")
        if conversation.get("discarded"):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_discarded",
            }
        current_artwork, current_refresh_pending, current_refresh_failure_code = _release_artwork(conversation)
        generation_request = conversation.get("artwork") or {}
        if preferences.get(artwork_db.DELETION_PENDING_FIELD):
            raise MemoryArtworkError("memory_artwork_deletion_pending")
        if preferences.get("consent") == "declined":
            return {"schema_version": ARTWORK_SCHEMA_VERSION, "status": "declined"}
        if (
            not self.global_consent_checker(uid)
            or preferences.get("consent") != "accepted"
            or preferences.get("consent_version") != ARTWORK_CONSENT_VERSION
        ):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_consent_required",
            }
        if _source_is_sensitive(conversation):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_sensitive_source_excluded",
            }
        if (
            not isinstance(current_artwork, dict)
            or current_artwork.get("status") != "ready"
            or current_artwork.get("generation_key") != artwork.get("generation_key")
            or current_artwork.get("object_key") != artwork.get("object_key")
            or current_artwork.get("authority_digest") != authority.authority_digest
            or current_artwork.get("binding_id") != authority.binding_id
            or current_artwork.get("profile_id") != authority.profile_id
            or not _preferences_match_authority(
                preferences,
                authority,
                style_version=(
                    None
                    if current_refresh_pending or current_refresh_failure_code
                    else str(current_artwork.get("style_version") or "")
                ),
            )
        ):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_preference_authority_stale",
            }
        if current_refresh_pending or current_refresh_failure_code:
            if (
                not isinstance(generation_request, dict)
                or generation_request.get("style_version") != preferences.get("style_version")
                or generation_request.get("enrichment_revision") != current_artwork.get("enrichment_revision")
            ):
                return {
                    "schema_version": ARTWORK_SCHEMA_VERSION,
                    "status": "unavailable",
                    "failure_code": "memory_artwork_preference_authority_stale",
                }
        current_enrichment_revision = _terminal_enrichment(conversation)
        _, current_prompt_sha256 = _prompt_for(
            conversation,
            str(current_artwork.get("style_version") or ""),
        )
        if current_enrichment_revision != current_artwork.get(
            "enrichment_revision"
        ) or current_prompt_sha256 != current_artwork.get("prompt_sha256"):
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "status": "unavailable",
                "failure_code": "memory_artwork_source_stale",
            }
        object_key = str(current_artwork.get("object_key") or "")
        if not object_key:
            raise MemoryArtworkError("memory_artwork_object_missing", retryable=True)
        url = self.store_factory().signed_get_url(uid=uid, memory_id=memory_id, object_key=object_key)
        return {
            "schema_version": ARTWORK_SCHEMA_VERSION,
            "status": "ready",
            "style_version": current_artwork.get("style_version"),
            "enrichment_revision": current_artwork.get("enrichment_revision"),
            "content_type": current_artwork.get("content_type"),
            "pixel_width": current_artwork.get("pixel_width"),
            "pixel_height": current_artwork.get("pixel_height"),
            "url": url,
            "expires_in_seconds": 300,
            "refresh_pending": current_refresh_pending,
            "refresh_failure_code": current_refresh_failure_code,
            "requested_style_version": (
                generation_request.get("style_version")
                if current_refresh_pending or current_refresh_failure_code
                else current_artwork.get("style_version")
            ),
        }

    async def backfill(
        self,
        uid: str,
        *,
        cursor_memory_id: Optional[str] = None,
        origin: str = HISTORICAL_BACKFILL_ORIGIN,
    ) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        if not (
            self.config.enabled
            and self.config.release_enabled
            and self.config.provider_enabled
            and self.config.backfill_enabled
        ):
            raise MemoryArtworkError("memory_artwork_backfill_disabled")
        if origin not in {HISTORICAL_BACKFILL_ORIGIN, PREVIEW_BACKFILL_ORIGIN}:
            raise MemoryArtworkError("memory_artwork_backfill_origin_invalid")
        try:
            recent = self.repository.list_conversations_page(
                uid,
                limit=BACKFILL_SCAN_LIMIT + 1,
                cursor_memory_id=cursor_memory_id,
            )
        except ValueError as exc:
            raise MemoryArtworkError("memory_artwork_backfill_cursor_invalid") from exc
        has_more = len(recent) > BACKFILL_SCAN_LIMIT
        page = recent[:BACKFILL_SCAN_LIMIT]
        queued = existing = skipped = 0
        scanned = 0
        memory_ids: list[str] = []
        recovery_memory_ids: list[str] = []
        next_cursor: Optional[str] = None
        preview_days: set[str] = set()

        def page_result() -> dict[str, Any]:
            return {
                "schema_version": ARTWORK_SCHEMA_VERSION,
                "limit": MAX_BACKFILL_MEMORIES,
                "scan_limit": BACKFILL_SCAN_LIMIT,
                "scanned": scanned,
                "queued": queued,
                "existing": existing,
                "skipped": skipped,
                "memory_ids": list(memory_ids),
                "next_cursor": next_cursor if has_more else None,
                "has_more": has_more,
                "preview_day_limit": DEFAULT_PREVIEW_DAY_LIMIT,
                "preview_days": len(preview_days),
                "_recovery_memory_ids": list(recovery_memory_ids),
            }

        for index, conversation in enumerate(page):
            scanned += 1
            memory_id = str(conversation.get("id") or "")
            if not memory_id:
                skipped += 1
                continue
            if _terminal_enrichment(conversation) is None:
                next_cursor = memory_id
                skipped += 1
                if (
                    conversation.get("status") == "completed"
                    and len(recovery_memory_ids) < MAX_BACKFILL_ENRICHMENT_RECOVERIES
                ):
                    recovery_memory_ids.append(memory_id)
                    if len(recovery_memory_ids) >= MAX_BACKFILL_ENRICHMENT_RECOVERIES:
                        has_more = has_more or index < len(page) - 1
                        break
                continue
            if origin == PREVIEW_BACKFILL_ORIGIN:
                day = _conversation_day(conversation) or f"unknown:{memory_id}"
                if day not in preview_days and len(preview_days) >= DEFAULT_PREVIEW_DAY_LIMIT:
                    has_more = True
                    break
                preview_days.add(day)
            next_cursor = memory_id
            try:
                result = await self.enqueue(uid, memory_id, origin=origin)
            except MemoryArtworkError as exc:
                skipped += 1
                recovery_memory_ids.append(memory_id)
                has_more = has_more or index < len(page) - 1
                raise MemoryArtworkBackfillError(
                    exc.code,
                    retryable=exc.retryable,
                    partial_result=page_result(),
                ) from exc
            except Exception as exc:
                skipped += 1
                recovery_memory_ids.append(memory_id)
                has_more = has_more or index < len(page) - 1
                raise MemoryArtworkBackfillError(
                    "memory_artwork_backfill_failed",
                    retryable=True,
                    partial_result=page_result(),
                ) from exc
            if result.get("outcome") == "reserved":
                queued += 1
                memory_ids.append(memory_id)
            elif result.get("outcome") == "existing":
                existing += 1
            else:
                skipped += 1
            if queued >= MAX_BACKFILL_MEMORIES:
                has_more = has_more or index < len(page) - 1
                break
        return page_result()


class MemoryArtworkWorker:
    """Restart-safe consumer for content-free generation dispatch records."""

    def __init__(
        self,
        *,
        repository: Optional[MemoryArtworkRepository] = None,
        service_factory: Callable[[], MemoryArtworkService] = MemoryArtworkService,
        config: Optional[MemoryArtworkConfig] = None,
        enrichment_recovery: Optional[MemoryArtworkEnrichmentRecovery] = None,
    ):
        self.repository = repository or FirestoreMemoryArtworkRepository()
        self.service_factory = service_factory
        self.config = config or MemoryArtworkConfig.from_env()
        self.enrichment_recovery = enrichment_recovery

    async def run_job(
        self,
        uid: str,
        memory_id: str,
        generation_key: str,
        *,
        raise_errors: bool = False,
    ) -> dict[str, Any]:
        if not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_internal_owner_required")
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            raise MemoryArtworkError("memory_artwork_generation_disabled")
        if not uid or not memory_id or len(generation_key) != 64:
            raise MemoryArtworkError("memory_artwork_dispatch_invalid")
        job_lease_token = secrets.token_hex(24)
        claimed_job = self.repository.claim_job(
            uid,
            memory_id,
            generation_key,
            lease_token=job_lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=_artwork_lease_seconds(),
        )
        if claimed_job is None:
            return {"outcome": "not_claimed", "status": "unavailable"}
        service = self.service_factory()
        try:
            reservation = await service.enqueue(
                uid,
                memory_id,
                origin=str(claimed_job.get("origin") or HISTORICAL_BACKFILL_ORIGIN),
                preserve_job_attempts=True,
            )
            if reservation.get("status") == "ready":
                self.repository.complete_job(uid, memory_id, generation_key, lease_token=job_lease_token)
                return {"outcome": "ready", "status": "ready"}
            if reservation.get("status") != "generating":
                self.repository.fail_job(
                    uid,
                    memory_id,
                    generation_key,
                    lease_token=job_lease_token,
                    failure_code=f"memory_artwork_{reservation.get('outcome') or 'dispatch_unavailable'}",
                )
                return reservation
            current = service.repository.get_conversation(uid, memory_id) or {}
            current_generation_key = str(((current.get("artwork") or {}).get("generation_key") or ""))
            if current_generation_key != generation_key:
                # A correction reserved a newer generation. The newer
                # transaction owns its own durable job.
                self.repository.complete_job(uid, memory_id, generation_key, lease_token=job_lease_token)
                return {"outcome": "superseded", "status": "unavailable"}
            result = await service.process(
                uid,
                memory_id,
                generation_key=generation_key,
                job_lease_token=job_lease_token,
            )
            if result.get("status") == "ready":
                self.repository.complete_job(uid, memory_id, generation_key, lease_token=job_lease_token)
            return result
        except MemoryArtworkError as exc:
            attempts = int(claimed_job.get("attempt_count") or 0) + 1
            if exc.retryable and attempts < WORKER_MAX_ATTEMPTS:
                self.repository.retry_job(
                    uid,
                    memory_id,
                    generation_key,
                    lease_token=job_lease_token,
                    attempt_count=attempts,
                    delay_seconds=_worker_retry_delay_seconds(attempts),
                    failure_code=exc.code,
                )
            else:
                self.repository.fail_job(
                    uid,
                    memory_id,
                    generation_key,
                    lease_token=job_lease_token,
                    failure_code=exc.code,
                )
            if raise_errors:
                raise
            return {"outcome": "failed", "status": "unavailable", "failure_code": exc.code}
        except Exception as exc:
            attempts = int(claimed_job.get("attempt_count") or 0) + 1
            if attempts < WORKER_MAX_ATTEMPTS:
                self.repository.retry_job(
                    uid,
                    memory_id,
                    generation_key,
                    lease_token=job_lease_token,
                    attempt_count=attempts,
                    delay_seconds=_worker_retry_delay_seconds(attempts),
                    failure_code="memory_artwork_worker_failed",
                )
            else:
                self.repository.fail_job(
                    uid,
                    memory_id,
                    generation_key,
                    lease_token=job_lease_token,
                    failure_code="memory_artwork_worker_failed",
                )
            if raise_errors:
                raise MemoryArtworkError("memory_artwork_worker_failed", retryable=True) from exc
            return {
                "outcome": "failed",
                "status": "unavailable",
                "failure_code": "memory_artwork_worker_failed",
            }

    async def run_reconciliation_job(self, job: dict[str, Any]) -> dict[str, Any]:
        uid = str(job.get("uid") or "")
        job_id = str(job.get("job_id") or "")
        if not uid or len(job_id) != 64 or not self.config.allows_uid(uid):
            raise MemoryArtworkError("memory_artwork_reconciliation_invalid")
        lease_token = secrets.token_hex(24)
        claimed = self.repository.claim_reconciliation_job(
            uid,
            job_id,
            lease_token=lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=_artwork_lease_seconds(),
        )
        if claimed is None:
            return {"outcome": "not_claimed", "status": "unavailable"}
        service = self.service_factory()
        result: dict[str, Any] | None = None
        recovery_memory_ids: list[str] = []
        remaining_recovery_memory_ids: list[str] = []
        recovery_index = 0
        recovery_queued = 0
        recovery_existing = 0
        recovery_accounting_applied = False
        try:
            authority = await service.authority_resolver(uid)
            preferences = service.repository.get_preferences(uid)
            if (
                claimed.get("authority_digest") != authority.authority_digest
                or claimed.get("style_version") != preferences.get("style_version")
                or not service.global_consent_checker(uid)
                or not _preferences_match_authority(
                    preferences,
                    authority,
                    style_version=str(claimed.get("style_version") or ""),
                )
            ):
                self.repository.finish_reconciliation_job(
                    job_id,
                    lease_token=lease_token,
                    update={
                        "status": "failed",
                        "failure_code": "memory_artwork_preference_authority_stale",
                        "completed_at": datetime.now(timezone.utc),
                    },
                )
                return {
                    "outcome": "failed",
                    "status": "failed",
                    "failure_code": "memory_artwork_preference_authority_stale",
                }
            recovery_page = claimed.get("recovery_page")
            if isinstance(recovery_page, dict):
                stored_result = recovery_page.get("result")
                stored_memory_ids = recovery_page.get("memory_ids")
                if not isinstance(stored_result, dict) or not isinstance(stored_memory_ids, list):
                    raise MemoryArtworkError("memory_artwork_reconciliation_recovery_page_invalid")
                result = dict(stored_result)
                recovery_memory_ids = [
                    str(memory_id) for memory_id in stored_memory_ids if isinstance(memory_id, str) and memory_id
                ]
                if len(recovery_memory_ids) != len(stored_memory_ids):
                    raise MemoryArtworkError("memory_artwork_reconciliation_recovery_page_invalid")
            else:
                try:
                    result = await service.backfill(uid, cursor_memory_id=claimed.get("cursor"))
                    recovery_memory_ids = result.pop("_recovery_memory_ids", [])
                except MemoryArtworkBackfillError as exc:
                    result = dict(exc.partial_result)
                    recovery_memory_ids = result.pop("_recovery_memory_ids", [])
                    raise
            recovery_pending = False
            recovery_handler = self.enrichment_recovery or _memory_artwork_enrichment_recovery
            for index, memory_id in enumerate(recovery_memory_ids):
                recovery_index = index
                enqueue_result = None
                try:
                    enqueue_result = await service.enqueue(uid, memory_id, origin=HISTORICAL_BACKFILL_ORIGIN)
                except MemoryArtworkError as exc:
                    if exc.code != "memory_artwork_enrichment_not_terminal":
                        raise
                if enqueue_result is None:
                    if recovery_handler is None:
                        raise MemoryArtworkError("memory_artwork_enrichment_recovery_unavailable", retryable=True)
                    recovery = await recovery_handler(uid, memory_id)
                    recovery_outcome = str(recovery.get("outcome") or "")
                    if recovery_outcome == "completed":
                        try:
                            enqueue_result = await service.enqueue(uid, memory_id, origin=HISTORICAL_BACKFILL_ORIGIN)
                        except MemoryArtworkError as exc:
                            if exc.code != "memory_artwork_enrichment_not_terminal":
                                raise
                            recovery_pending = True
                            remaining_recovery_memory_ids.append(memory_id)
                    elif recovery_outcome in ENRICHMENT_RECOVERY_PENDING_OUTCOMES:
                        recovery_pending = True
                        remaining_recovery_memory_ids.append(memory_id)
                    elif recovery_outcome not in ENRICHMENT_RECOVERY_TERMINAL_OUTCOMES:
                        raise MemoryArtworkError("memory_artwork_enrichment_recovery_outcome_invalid")
                if enqueue_result is not None:
                    enqueue_outcome = str(enqueue_result.get("outcome") or "")
                    if enqueue_outcome == "reserved":
                        recovery_queued += 1
                    elif enqueue_outcome == "existing":
                        recovery_existing += 1
                    elif enqueue_outcome not in {
                        "consent_required",
                        "declined",
                        "disabled",
                        "sensitive_source_excluded",
                    }:
                        raise MemoryArtworkError("memory_artwork_enqueue_outcome_invalid")
                recovery_index = index + 1
            result["queued"] = int(result.get("queued") or 0) + recovery_queued
            result["existing"] = int(result.get("existing") or 0) + recovery_existing
            result["skipped"] = max(
                0,
                int(result.get("skipped") or 0) - recovery_queued - recovery_existing,
            )
            recovery_accounting_applied = True
            has_more = result.get("has_more") is True
            now = datetime.now(timezone.utc)
            if recovery_pending:
                self.repository.finish_reconciliation_job(
                    job_id,
                    lease_token=lease_token,
                    update={
                        "status": "pending",
                        "cursor": claimed.get("cursor"),
                        "available_at": now + timedelta(seconds=5),
                        "attempt_count": 0,
                        "failure_code": None,
                        "recovery_page": {
                            "result": result,
                            "memory_ids": remaining_recovery_memory_ids,
                        },
                    },
                )
                return {
                    "outcome": "enrichment_recovery_pending",
                    "status": "pending",
                }
            update = {
                "status": "pending" if has_more else "completed",
                "cursor": result.get("next_cursor") if has_more else None,
                "pages_processed": int(claimed.get("pages_processed") or 0) + 1,
                "scanned": int(claimed.get("scanned") or 0) + int(result.get("scanned") or 0),
                "queued": int(claimed.get("queued") or 0) + int(result.get("queued") or 0),
                "existing": int(claimed.get("existing") or 0) + int(result.get("existing") or 0),
                "skipped": int(claimed.get("skipped") or 0) + int(result.get("skipped") or 0),
                "available_at": now,
                "attempt_count": 0,
                "failure_code": None,
                "recovery_page": None,
            }
            if not has_more:
                update["completed_at"] = now
            self.repository.finish_reconciliation_job(job_id, lease_token=lease_token, update=update)
            return {"outcome": "continued" if has_more else "completed", "status": update["status"]}
        except MemoryArtworkError as exc:
            attempts = int(claimed.get("attempt_count") or 0) + 1
            retryable = exc.retryable and attempts < WORKER_MAX_ATTEMPTS
            now = datetime.now(timezone.utc)
            update = {
                "status": "pending" if retryable else "failed",
                "attempt_count": attempts,
                "available_at": now + timedelta(seconds=min(300, 2**attempts)),
                "failure_code": exc.code,
            }
            if isinstance(result, dict):
                checkpoint_result = dict(result)
                if not recovery_accounting_applied:
                    checkpoint_result["queued"] = int(checkpoint_result.get("queued") or 0) + recovery_queued
                    checkpoint_result["existing"] = int(checkpoint_result.get("existing") or 0) + recovery_existing
                    checkpoint_result["skipped"] = max(
                        0,
                        int(checkpoint_result.get("skipped") or 0) - recovery_queued - recovery_existing,
                    )
                outstanding_memory_ids = list(
                    dict.fromkeys(remaining_recovery_memory_ids + recovery_memory_ids[recovery_index:])
                )
                update["cursor"] = claimed.get("cursor")
                update["recovery_page"] = {
                    "result": checkpoint_result,
                    "memory_ids": outstanding_memory_ids,
                }
            if not retryable:
                update["completed_at"] = now
            self.repository.finish_reconciliation_job(job_id, lease_token=lease_token, update=update)
            return {"outcome": "retry" if retryable else "failed", "status": update["status"], "failure_code": exc.code}
        except Exception:
            attempts = int(claimed.get("attempt_count") or 0) + 1
            retryable = attempts < WORKER_MAX_ATTEMPTS
            now = datetime.now(timezone.utc)
            failure_code = "memory_artwork_reconciliation_worker_failed"
            update = {
                "status": "pending" if retryable else "failed",
                "attempt_count": attempts,
                "available_at": now + timedelta(seconds=min(300, 2**attempts)),
                "failure_code": failure_code,
            }
            if isinstance(result, dict):
                checkpoint_result = dict(result)
                if not recovery_accounting_applied:
                    checkpoint_result["queued"] = int(checkpoint_result.get("queued") or 0) + recovery_queued
                    checkpoint_result["existing"] = int(checkpoint_result.get("existing") or 0) + recovery_existing
                    checkpoint_result["skipped"] = max(
                        0,
                        int(checkpoint_result.get("skipped") or 0) - recovery_queued - recovery_existing,
                    )
                outstanding_memory_ids = list(
                    dict.fromkeys(remaining_recovery_memory_ids + recovery_memory_ids[recovery_index:])
                )
                update["cursor"] = claimed.get("cursor")
                update["recovery_page"] = {
                    "result": checkpoint_result,
                    "memory_ids": outstanding_memory_ids,
                }
            if not retryable:
                update["completed_at"] = now
            self.repository.finish_reconciliation_job(job_id, lease_token=lease_token, update=update)
            return {
                "outcome": "retry" if retryable else "failed",
                "status": update["status"],
                "failure_code": failure_code,
            }

    async def run_once(self) -> int:
        if not (self.config.enabled and self.config.release_enabled and self.config.provider_enabled):
            return 0
        processed = 0
        for job in self.repository.list_pending_reconciliation_jobs(limit=5, now=datetime.now(timezone.utc)):
            result = await self.run_reconciliation_job(job)
            if result.get("outcome") != "not_claimed":
                processed += 1
        for job in self.repository.list_pending_jobs(limit=WORKER_BATCH_SIZE, now=datetime.now(timezone.utc)):
            uid = str(job.get("uid") or "")
            memory_id = str(job.get("memory_id") or "")
            generation_key = str(job.get("generation_key") or "")
            if not uid or not memory_id or len(generation_key) != 64:
                continue
            result = await self.run_job(uid, memory_id, generation_key)
            if result.get("outcome") != "not_claimed":
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
        logger.exception(
            "Memory artwork reservation failed after terminal enrichment",
            extra={"memory_id": memory_id},
        )
