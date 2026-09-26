"""Private GCS primitives shared by Ella authenticated media features."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    from google.api_core.exceptions import NotFound, PreconditionFailed
    from google.cloud import storage
except ImportError:  # pragma: no cover - production dependency, replaced by test doubles.
    storage = None

    class NotFound(Exception):
        pass

    class PreconditionFailed(Exception):
        pass


DREAM_MEDIA_PREFIX = "dreams/v1"
DREAM_MEDIA_CACHE_CONTROL = "private, no-store"
IMAGE_AUDIO_TTL_SECONDS = 300
VIDEO_TTL_SECONDS = 900
MAX_DREAM_MEDIA_BYTES = 64 * 1024 * 1024
SIGNING_SNIFF_BYTES = 64 * 1024

CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
}
ALLOWED_DREAM_MEDIA_TYPES = frozenset(CONTENT_TYPE_EXTENSIONS)
_DREAM_OBJECT_RE = re.compile(
    r"^dreams/v1/p(?P<version>[1-9][0-9]*)/(?P<owner>[0-9a-f]{64})/"
    r"(?P<dream>[0-9a-f]{32})/(?P<asset>[0-9a-f]{32})\."
    r"(?P<extension>mp4|png|jpg|webp|mp3|ogg)$"
)
_PEPPER_ENV_RE = re.compile(r"^DREAM_MEDIA_KEY_PEPPER_V(?P<version>[1-9][0-9]*)$")
_SIGNED_PARAMETER_RE = re.compile(r"(?i)(X-Goog-[A-Za-z0-9-]+)=([^&\s]+)")
_STORAGE_URL_RE = re.compile(r"https?://storage\.googleapis\.com/[^\s\"'<>]+", re.IGNORECASE)


class PrivateMediaStorageError(RuntimeError):
    """A content-free storage failure safe to map to a typed API error."""


@dataclass(frozen=True)
class DreamMediaObjectKey:
    object_key: str
    asset_key: str
    pepper_version: int
    content_type: str


@dataclass(frozen=True)
class StoredPrivateMedia:
    object_key: str
    content_type: str
    byte_size: int
    sha256: str
    object_generation: str = ""


@dataclass(frozen=True)
class SignedPrivateMedia:
    url: str
    content_type: str
    expires_in_seconds: int


class GCSPrivateObjectBucket:
    """Small shared adapter for immutable private GCS object operations."""

    def __init__(self, *, bucket_name: str, client=None):
        if not bucket_name:
            raise PrivateMediaStorageError("private_media_storage_not_configured")
        if storage is None and client is None:
            raise PrivateMediaStorageError("private_media_storage_dependency_unavailable")
        self.bucket_name = bucket_name
        self.client = client or storage.Client()

    @property
    def bucket(self):
        return self.client.bucket(self.bucket_name)

    def upload_immutable(
        self,
        *,
        object_key: str,
        content_type: str,
        payload: bytes,
        cache_control: str,
        metadata: Mapping[str, str],
    ):
        blob = self.bucket.blob(object_key)
        blob.cache_control = cache_control
        blob.metadata = dict(metadata)
        try:
            blob.upload_from_string(payload, content_type=content_type, if_generation_match=0)
        except PreconditionFailed:
            pass
        return self.load(object_key)

    def load(self, object_key: str):
        blob = self.bucket.blob(object_key)
        try:
            blob.reload()
        except NotFound as exc:
            raise PrivateMediaStorageError("private_media_object_missing") from exc
        except Exception as exc:
            raise PrivateMediaStorageError("private_media_storage_unavailable") from exc
        return blob

    def download_prefix(self, blob, byte_count: int) -> bytes:
        try:
            return blob.download_as_bytes(start=0, end=byte_count - 1)
        except NotFound as exc:
            raise PrivateMediaStorageError("private_media_object_missing") from exc
        except Exception as exc:
            raise PrivateMediaStorageError("private_media_storage_unavailable") from exc

    def signed_inline_get(self, blob, *, content_type: str, ttl_seconds: int) -> str:
        try:
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=ttl_seconds),
                method="GET",
                response_type=content_type,
                response_disposition="inline",
            )
        except Exception as exc:
            raise PrivateMediaStorageError("private_media_signing_unavailable") from exc

    def delete(self, object_key: str) -> None:
        try:
            self.bucket.blob(object_key).delete()
        except NotFound:
            return
        except Exception as exc:
            raise PrivateMediaStorageError("private_media_delete_failed") from exc

    def list_keys(self, prefix: str) -> list[str]:
        try:
            return [str(blob.name) for blob in self.bucket.list_blobs(prefix=prefix)]
        except Exception as exc:
            raise PrivateMediaStorageError("private_media_list_failed") from exc


@dataclass(frozen=True)
class DreamMediaPepperConfig:
    active_version: int
    known_versions: tuple[int, ...]
    peppers: Mapping[int, bytes]

    @classmethod
    def from_environment(cls) -> "DreamMediaPepperConfig":
        active_raw = os.getenv("DREAM_MEDIA_ACTIVE_PEPPER_VERSION", "").strip().lower().removeprefix("v")
        if not active_raw.isdigit() or int(active_raw) < 1:
            raise PrivateMediaStorageError("dream_media_active_pepper_version_invalid")
        active_version = int(active_raw)

        configured_versions: set[int] = set()
        versions_raw = os.getenv("DREAM_MEDIA_PEPPER_VERSIONS", "").strip()
        for value in versions_raw.split(",") if versions_raw else ():
            normalized = value.strip().lower().removeprefix("v")
            if not normalized.isdigit() or int(normalized) < 1:
                raise PrivateMediaStorageError("dream_media_pepper_versions_invalid")
            configured_versions.add(int(normalized))

        peppers: dict[int, bytes] = {}
        for name, value in os.environ.items():
            match = _PEPPER_ENV_RE.fullmatch(name)
            if match is None or not value:
                continue
            version = int(match.group("version"))
            configured_versions.add(version)
            encoded = value.encode("utf-8")
            if len(encoded) < 32:
                raise PrivateMediaStorageError("dream_media_pepper_too_short")
            peppers[version] = encoded

        configured_versions.add(active_version)
        if active_version not in peppers:
            raise PrivateMediaStorageError("dream_media_active_pepper_missing")
        return cls(
            active_version=active_version,
            known_versions=tuple(sorted(configured_versions)),
            peppers=peppers,
        )

    def pepper_for(self, version: int) -> bytes:
        pepper = self.peppers.get(version)
        if not pepper:
            raise PrivateMediaStorageError("dream_media_pepper_unavailable")
        return pepper


def derive_uid_hash(uid: str, pepper: bytes) -> str:
    if not uid or not pepper:
        raise PrivateMediaStorageError("dream_media_key_material_invalid")
    return hmac.new(pepper, uid.encode("utf-8"), hashlib.sha256).hexdigest()


def sniff_content_type(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return "video/mp4"
    if payload.startswith(b"OggS"):
        return "audio/ogg"
    if payload.startswith(b"ID3") or (len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    raise PrivateMediaStorageError("dream_media_content_type_not_allowed")


def require_faststart_mp4(payload: bytes) -> None:
    if sniff_content_type(payload) != "video/mp4":
        return
    offset = 0
    moov_offset: Optional[int] = None
    mdat_offset: Optional[int] = None
    while offset + 8 <= len(payload):
        size = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(payload):
                break
            size = int.from_bytes(payload[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(payload) - offset
        if size < header_size or offset + size > len(payload):
            break
        if kind == b"moov" and moov_offset is None:
            moov_offset = offset
        if kind == b"mdat" and mdat_offset is None:
            mdat_offset = offset
        offset += size
    if moov_offset is None or mdat_offset is None or moov_offset > mdat_offset:
        raise PrivateMediaStorageError("dream_media_mp4_faststart_required")


def build_dream_media_object_key(
    *,
    uid: str,
    pepper: bytes,
    pepper_version: int,
    content_type: str,
    dream_key: Optional[str] = None,
    asset_key: Optional[str] = None,
) -> DreamMediaObjectKey:
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if not extension or pepper_version < 1:
        raise PrivateMediaStorageError("dream_media_content_type_not_allowed")
    resolved_dream_key = dream_key or secrets.token_hex(16)
    resolved_asset_key = asset_key or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", resolved_dream_key):
        raise PrivateMediaStorageError("dream_media_dream_key_invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", resolved_asset_key):
        raise PrivateMediaStorageError("dream_media_asset_key_invalid")
    owner = derive_uid_hash(uid, pepper)
    object_key = (
        f"{DREAM_MEDIA_PREFIX}/p{pepper_version}/{owner}/" f"{resolved_dream_key}/{resolved_asset_key}.{extension}"
    )
    return DreamMediaObjectKey(
        object_key=object_key,
        asset_key=resolved_asset_key,
        pepper_version=pepper_version,
        content_type=content_type,
    )


def parse_dream_media_object_key(object_key: str) -> re.Match[str]:
    match = _DREAM_OBJECT_RE.fullmatch(str(object_key or ""))
    if match is None:
        raise PrivateMediaStorageError("dream_media_object_key_invalid")
    return match


def validate_dream_media_owner(
    *,
    uid: str,
    object_key: str,
    pepper_config: DreamMediaPepperConfig,
) -> re.Match[str]:
    match = parse_dream_media_object_key(object_key)
    version = int(match.group("version"))
    expected = derive_uid_hash(uid, pepper_config.pepper_for(version))
    if not hmac.compare_digest(match.group("owner"), expected):
        raise PrivateMediaStorageError("dream_media_object_owner_mismatch")
    return match


def ttl_for_content_type(content_type: str) -> int:
    if content_type == "video/mp4":
        return VIDEO_TTL_SECONDS
    if content_type in ALLOWED_DREAM_MEDIA_TYPES:
        return IMAGE_AUDIO_TTL_SECONDS
    raise PrivateMediaStorageError("dream_media_content_type_not_allowed")


def _redact_storage_url(match: re.Match[str]) -> str:
    candidate = match.group(0)
    parsed = urlsplit(candidate)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "[REDACTED]" if parsed.query else "", ""))


def redact_dream_media_credentials(value: object) -> str:
    text = str(value)
    text = _STORAGE_URL_RE.sub(_redact_storage_url, text)
    return _SIGNED_PARAMETER_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


class PrivateMediaCredentialRedactionFilter(logging.Filter):
    """Last-resort protection for accidental bearer-URL log arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        record.msg = redact_dream_media_credentials(rendered)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_dream_media_credentials(record.exc_text)
        return True


class GCSPrivateMediaStore:
    """Private bucket operations used by dream media and future artwork adapters."""

    def __init__(self, *, bucket_name: Optional[str] = None, client=None):
        self.bucket_name = (bucket_name or os.getenv("BUCKET_DREAM_MEDIA", "")).strip()
        if not self.bucket_name:
            raise PrivateMediaStorageError("dream_media_storage_not_configured")
        try:
            self.objects = GCSPrivateObjectBucket(bucket_name=self.bucket_name, client=client)
        except PrivateMediaStorageError as exc:
            raise PrivateMediaStorageError("dream_media_storage_dependency_unavailable") from exc

    @property
    def bucket(self):
        return self.objects.bucket

    def upload(self, *, object_key: str, content_type: str, payload: bytes) -> StoredPrivateMedia:
        parse_dream_media_object_key(object_key)
        sniffed = sniff_content_type(payload)
        if content_type != sniffed:
            raise PrivateMediaStorageError("dream_media_content_type_mismatch")
        if not payload or len(payload) > MAX_DREAM_MEDIA_BYTES:
            raise PrivateMediaStorageError("dream_media_payload_size_invalid")
        if content_type == "video/mp4":
            require_faststart_mp4(payload)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            blob = self.objects.upload_immutable(
                object_key=object_key,
                content_type=content_type,
                payload=payload,
                cache_control=DREAM_MEDIA_CACHE_CONTROL,
                metadata={"sha256": digest},
            )
        except Exception as exc:
            raise PrivateMediaStorageError("dream_media_storage_unavailable") from exc
        stored_type = str(blob.content_type or "").split(";", 1)[0].strip().lower()
        if stored_type != content_type or str(blob.cache_control or "").strip().lower() != DREAM_MEDIA_CACHE_CONTROL:
            raise PrivateMediaStorageError("dream_media_object_metadata_invalid")
        stored_digest = str((blob.metadata or {}).get("sha256") or "")
        if not hmac.compare_digest(stored_digest, digest):
            raise PrivateMediaStorageError("dream_media_object_digest_mismatch")
        return StoredPrivateMedia(
            object_key=object_key,
            content_type=content_type,
            byte_size=len(payload),
            sha256=digest,
            object_generation=str(blob.generation or ""),
        )

    def sign_get(
        self,
        *,
        uid: str,
        object_key: str,
        pepper_config: DreamMediaPepperConfig,
    ) -> SignedPrivateMedia:
        key_match = validate_dream_media_owner(uid=uid, object_key=object_key, pepper_config=pepper_config)
        try:
            blob = self.objects.load(object_key)
        except PrivateMediaStorageError as exc:
            if str(exc) == "private_media_object_missing":
                raise PrivateMediaStorageError("dream_media_object_missing") from exc
            raise PrivateMediaStorageError("dream_media_storage_unavailable") from exc
        content_type = str(blob.content_type or "").split(";", 1)[0].strip().lower()
        if content_type not in ALLOWED_DREAM_MEDIA_TYPES:
            raise PrivateMediaStorageError("dream_media_content_type_not_allowed")
        if key_match.group("extension") != CONTENT_TYPE_EXTENSIONS[content_type]:
            raise PrivateMediaStorageError("dream_media_content_type_mismatch")
        if str(blob.cache_control or "").strip().lower() != DREAM_MEDIA_CACHE_CONTROL:
            raise PrivateMediaStorageError("dream_media_object_cache_control_invalid")
        try:
            prefix = self.objects.download_prefix(blob, SIGNING_SNIFF_BYTES)
            sniffed_type = sniff_content_type(prefix)
        except PrivateMediaStorageError as exc:
            if str(exc) == "private_media_object_missing":
                raise PrivateMediaStorageError("dream_media_object_missing") from exc
            if str(exc) == "dream_media_content_type_not_allowed":
                raise
            raise PrivateMediaStorageError("dream_media_storage_unavailable") from exc
        except Exception as exc:
            raise PrivateMediaStorageError("dream_media_storage_unavailable") from exc
        if not hmac.compare_digest(content_type, sniffed_type):
            raise PrivateMediaStorageError("dream_media_content_type_mismatch")
        ttl = ttl_for_content_type(content_type)
        try:
            url = self.objects.signed_inline_get(blob, content_type=content_type, ttl_seconds=ttl)
        except Exception as exc:
            raise PrivateMediaStorageError("dream_media_signing_unavailable") from exc
        return SignedPrivateMedia(url=url, content_type=content_type, expires_in_seconds=ttl)

    def delete(self, object_key: str) -> None:
        parse_dream_media_object_key(object_key)
        try:
            self.objects.delete(object_key)
        except Exception as exc:
            raise PrivateMediaStorageError("dream_media_delete_failed") from exc

    def list_keys(self, prefix: str) -> list[str]:
        if not re.fullmatch(r"dreams/v1/p[1-9][0-9]*/(?:[0-9a-f]{64}/)?", prefix):
            raise PrivateMediaStorageError("dream_media_prefix_invalid")
        try:
            return self.objects.list_keys(prefix)
        except Exception as exc:
            raise PrivateMediaStorageError("dream_media_list_failed") from exc
