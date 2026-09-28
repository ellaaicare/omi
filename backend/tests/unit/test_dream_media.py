import copy
import hashlib
import json
import logging
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8787")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "dream-media-unit-tests")

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ella_package = types.ModuleType("ella")
ella_package.__path__ = [str(BACKEND_ROOT / "ella")]
services_package = types.ModuleType("ella.services")
services_package.__path__ = [str(BACKEND_ROOT / "ella" / "services")]
routers_package = types.ModuleType("ella.routers")
routers_package.__path__ = [str(BACKEND_ROOT / "ella" / "routers")]
sys.modules.setdefault("ella", ella_package)
sys.modules.setdefault("ella.services", services_package)
sys.modules.setdefault("ella.routers", routers_package)

from database.dream_media import DREAM_MEDIA_DELETION_PENDING_FIELD, InMemoryDreamMediaRepository
from ella.routers import dream_media as dream_router
from ella.services.dream_media import DreamMediaError, DreamMediaService, DreamUpload
from utils.ella.exact_firebase_auth import EllaRequestAuthority, get_exact_firebase_uid
from utils.ella.private_media_storage import (
    ALLOWED_DREAM_MEDIA_TYPES,
    DREAM_MEDIA_CACHE_CONTROL,
    GCSPrivateMediaStore,
    DreamMediaPepperConfig,
    PrivateMediaStorageError,
    SignedPrivateMedia,
    StoredPrivateMedia,
    build_dream_media_object_key,
    derive_uid_hash,
    redact_dream_media_credentials,
    require_faststart_mp4,
    sniff_content_type,
    ttl_for_content_type,
    validate_dream_media_owner,
)

UID_A = "firebase-user-a"
UID_B = "firebase-user-b"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\nfixture"
JPEG = b"\xff\xd8\xfffixture"
WEBP = b"RIFF\x04\x00\x00\x00WEBPfixture"
OGG = b"OggSfixture"
MP3 = b"ID3fixture"
HTML = b"<!doctype html><script>alert(1)</script>"


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


FASTSTART_MP4 = _box(b"ftyp", b"isom") + _box(b"moov") + _box(b"mdat", b"video")
NON_FASTSTART_MP4 = _box(b"ftyp", b"isom") + _box(b"mdat", b"video") + _box(b"moov")


@dataclass
class MutableClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


class FakePrivateStore:
    def __init__(self, clock: MutableClock):
        self.clock = clock
        self.objects: dict[str, dict] = {}
        self.signed_calls: list[str] = []
        self.deleted: list[str] = []
        self.fail_delete = False
        self.on_upload = None

    def upload(self, *, object_key: str, content_type: str, payload: bytes) -> StoredPrivateMedia:
        sniffed = sniff_content_type(payload)
        if sniffed != content_type:
            raise PrivateMediaStorageError("dream_media_content_type_mismatch")
        if content_type == "video/mp4":
            require_faststart_mp4(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.objects[object_key] = {
            "payload": payload,
            "content_type": content_type,
            "cache_control": DREAM_MEDIA_CACHE_CONTROL,
            "sha256": digest,
        }
        if self.on_upload is not None:
            self.on_upload()
        return StoredPrivateMedia(object_key, content_type, len(payload), digest, "1")

    def sign_get(self, *, uid: str, object_key: str, pepper_config: DreamMediaPepperConfig) -> SignedPrivateMedia:
        validate_dream_media_owner(uid=uid, object_key=object_key, pepper_config=pepper_config)
        stored = self.objects.get(object_key)
        if stored is None:
            raise PrivateMediaStorageError("dream_media_object_missing")
        content_type = stored["content_type"]
        sniffed = sniff_content_type(stored["payload"])
        if content_type not in ALLOWED_DREAM_MEDIA_TYPES or sniffed != content_type:
            raise PrivateMediaStorageError("dream_media_content_type_not_allowed")
        if stored["cache_control"] != DREAM_MEDIA_CACHE_CONTROL:
            raise PrivateMediaStorageError("dream_media_object_cache_control_invalid")
        ttl = ttl_for_content_type(content_type)
        query = urlencode(
            {
                "X-Goog-Signature": "unit-secret-signature",
                "X-Goog-Date": str(int(self.clock.value.timestamp())),
                "X-Goog-Expires": str(ttl),
            }
        )
        self.signed_calls.append(object_key)
        return SignedPrivateMedia(
            url=f"https://storage.googleapis.com/private-dream-media/{object_key}?{query}",
            content_type=content_type,
            expires_in_seconds=ttl,
        )

    def fetch_status(self, url: str) -> int:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if not query.get("X-Goog-Signature"):
            return 403
        issued = datetime.fromtimestamp(int(query["X-Goog-Date"][0]), tz=timezone.utc)
        expires = int(query["X-Goog-Expires"][0])
        return 200 if self.clock.value <= issued + timedelta(seconds=expires) else 403

    def delete(self, object_key: str) -> None:
        if self.fail_delete:
            raise PrivateMediaStorageError("dream_media_delete_failed")
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


@pytest.fixture
def harness():
    clock = MutableClock()
    repository = InMemoryDreamMediaRepository()
    repository.users[UID_A] = {}
    repository.users[UID_B] = {}
    store = FakePrivateStore(clock)
    peppers = DreamMediaPepperConfig(active_version=1, known_versions=(1,), peppers={1: b"pepper-one"})
    service = DreamMediaService(repository, store=store, pepper_config=peppers, now=clock.now)
    return repository, store, clock, service


def _upload(
    service: DreamMediaService,
    *,
    uid: str = UID_A,
    dream_id: str = "dream-1",
    request_id: str = "request-0001",
    payload: bytes = PNG,
    content_type: str = "image/png",
    source_memory_ids: tuple[str, ...] = (),
) -> dict:
    return service.upload(
        uid=uid,
        dream_id=dream_id,
        upload=DreamUpload(
            request_id=request_id,
            title="A private dream",
            narrative="Structured narrative",
            captions=("A caption",),
            source_memory_ids=source_memory_ids,
            created_at=NOW,
        ),
        payload=payload,
        claimed_content_type=content_type,
    )


def _client(service: DreamMediaService, uid: str = UID_A) -> TestClient:
    app = FastAPI()
    app.include_router(dream_router.router)
    app.dependency_overrides[get_exact_firebase_uid] = lambda: uid
    app.dependency_overrides[dream_router.get_dream_media_service] = lambda: service
    return TestClient(app)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
        (FASTSTART_MP4, "video/mp4"),
        (MP3, "audio/mpeg"),
        (OGG, "audio/ogg"),
    ],
)
def test_content_type_is_sniffed_from_bytes(payload, expected):
    assert sniff_content_type(payload) == expected


@pytest.mark.parametrize("payload", [HTML, b"<svg/>", b"# markdown", b"%PDF-1.7"])
def test_active_and_document_content_is_not_allowlisted(payload):
    with pytest.raises(PrivateMediaStorageError, match="dream_media_content_type_not_allowed"):
        sniff_content_type(payload)


def test_hmac_argument_order_and_object_key_are_pinned():
    assert derive_uid_hash("firebase-user-123", b"pepper") == (
        "a49954755d0a4926996cd238236e3aca0746b20c59656b4e483eec1ab8a4bcab"
    )
    key = build_dream_media_object_key(
        uid="firebase-user-123",
        pepper=b"pepper",
        pepper_version=7,
        content_type="image/png",
        dream_key="1" * 32,
        asset_key="2" * 32,
    )
    assert key.object_key == (
        "dreams/v1/p7/a49954755d0a4926996cd238236e3aca0746b20c59656b4e483eec1ab8a4bcab/" f"{'1' * 32}/{'2' * 32}.png"
    )


def test_environment_rejects_short_active_pepper(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("DREAM_MEDIA_KEY_PEPPER_V"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DREAM_MEDIA_ACTIVE_PEPPER_VERSION", "1")
    monkeypatch.setenv("DREAM_MEDIA_KEY_PEPPER_V1", "too-short")
    with pytest.raises(PrivateMediaStorageError, match="dream_media_pepper_too_short"):
        DreamMediaPepperConfig.from_environment()


def test_upload_rejects_non_allowlisted_content_before_inventory(harness):
    repository, store, _clock, service = harness
    with pytest.raises(DreamMediaError, match="dream_media_content_type_not_allowed") as exc:
        _upload(service, payload=HTML, content_type="text/html")
    assert exc.value.status_code == 415
    assert repository.dreams == {}
    assert store.objects == {}


def test_upload_rejects_content_type_mismatch_and_non_faststart_mp4(harness):
    repository, _store, _clock, service = harness
    with pytest.raises(DreamMediaError, match="dream_media_content_type_mismatch"):
        _upload(service, payload=PNG, content_type="image/jpeg")
    with pytest.raises(DreamMediaError, match="dream_media_mp4_faststart_required"):
        _upload(service, request_id="request-0002", payload=NON_FASTSTART_MP4, content_type="video/mp4")
    assert repository.dreams == {}


def test_missing_pepper_configuration_is_retryable_not_a_content_error(harness):
    repository, store, clock, _service = harness
    service = DreamMediaService(repository, store=store, pepper_config=None, now=clock.now)
    with pytest.raises(DreamMediaError, match="dream_media_active_pepper_version_invalid") as exc:
        _upload(service)
    assert exc.value.status_code == 503
    assert exc.value.retryable is True


def test_account_deletion_with_empty_inventory_needs_no_media_configuration(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("DREAM_MEDIA_") or name == "BUCKET_DREAM_MEDIA":
            monkeypatch.delenv(name, raising=False)

    class FencedRepository(InMemoryDreamMediaRepository):
        def list_dreams(self, uid):
            assert self.users[uid][DREAM_MEDIA_DELETION_PENDING_FIELD] is True
            return super().list_dreams(uid)

    repository = FencedRepository()
    repository.users[UID_A] = {}
    service = DreamMediaService(repository, store=None, pepper_config=None, now=lambda: NOW)

    assert service.delete_account(UID_A) == 0
    assert repository.users[UID_A][DREAM_MEDIA_DELETION_PENDING_FIELD] is True
    assert service._configured_store is None
    assert service._configured_peppers is None


def test_account_deletion_fence_rejects_commit_raced_after_upload(harness):
    repository, store, clock, service = harness
    store.on_upload = lambda: repository.begin_account_delete(UID_A, clock.now())

    with pytest.raises(DreamMediaError, match="dream_media_deletion_pending") as exc:
        _upload(service)

    assert exc.value.status_code == 503
    assert repository.users[UID_A][DREAM_MEDIA_DELETION_PENDING_FIELD] is True
    assert store.objects == {}
    pending = repository.dreams[(UID_A, "dream-1")]["media"]
    assert len(pending) == 1 and pending[0]["state"] == "pending"
    assert service.delete_account(UID_A) == 1
    assert repository.dreams[(UID_A, "dream-1")]["tombstoned"] is True


def test_dream_metadata_and_source_authority_cannot_be_replaced(harness):
    repository, store, _clock, service = harness
    repository.sources[(UID_A, "source-a")] = {"state": "active"}
    repository.sources[(UID_A, "source-b")] = {"state": "active"}
    _upload(service, source_memory_ids=("source-a",))
    original = copy.deepcopy(repository.dreams[(UID_A, "dream-1")])

    with pytest.raises(DreamMediaError, match="dream_media_metadata_conflict") as exc:
        _upload(
            service, request_id="request-0002", payload=JPEG, content_type="image/jpeg", source_memory_ids=("source-b",)
        )
    assert exc.value.status_code == 409
    assert repository.dreams[(UID_A, "dream-1")]["source_memory_ids"] == ["source-a"]
    assert repository.dreams[(UID_A, "dream-1")]["media"] == original["media"]
    assert len(store.objects) == 1

    changed_retry = DreamUpload(
        request_id="request-0001",
        title="Changed title",
        narrative="Structured narrative",
        captions=("A caption",),
        source_memory_ids=("source-a",),
        created_at=NOW,
    )
    with pytest.raises(DreamMediaError, match="dream_media_idempotency_conflict"):
        service.upload(
            uid=UID_A,
            dream_id="dream-1",
            upload=changed_retry,
            payload=PNG,
            claimed_content_type="image/png",
        )
    assert repository.dreams[(UID_A, "dream-1")]["title"] == "A private dream"


class _Blob:
    def __init__(self, *, payload: bytes, content_type: str):
        self.payload = payload
        self.content_type = content_type
        self.cache_control = DREAM_MEDIA_CACHE_CONTROL
        self.metadata = {"sha256": hashlib.sha256(payload).hexdigest()}
        self.generation = 1
        self.signed = False

    def reload(self):
        return None

    def upload_from_string(self, payload, *, content_type: str, if_generation_match: int):
        assert if_generation_match == 0
        self.payload = payload
        self.content_type = content_type

    def download_as_bytes(self, *, start: int, end: int):
        return self.payload[start : end + 1]

    def generate_signed_url(self, **_kwargs):
        self.signed = True
        return "https://storage.googleapis.com/private/object?X-Goog-Signature=secret"


class _Bucket:
    def __init__(self, blobs):
        self.blobs = blobs

    def blob(self, name):
        return self.blobs[name]


class _Client:
    def __init__(self, blobs):
        self._bucket = _Bucket(blobs)

    def bucket(self, _name):
        return self._bucket


@pytest.mark.parametrize(
    ("stored_type", "payload"),
    [("text/html", HTML), ("image/png", HTML), ("image/jpeg", PNG), ("image/jpeg", JPEG)],
)
def test_signer_refuses_planted_or_mislabeled_objects(stored_type, payload):
    config = DreamMediaPepperConfig(active_version=1, known_versions=(1,), peppers={1: b"pepper"})
    key = build_dream_media_object_key(
        uid=UID_A,
        pepper=b"pepper",
        pepper_version=1,
        content_type="image/png",
        dream_key="1" * 32,
        asset_key="2" * 32,
    ).object_key
    blob = _Blob(payload=payload, content_type=stored_type)
    store = GCSPrivateMediaStore(bucket_name="private", client=_Client({key: blob}))

    with pytest.raises(PrivateMediaStorageError):
        store.sign_get(uid=UID_A, object_key=key, pepper_config=config)
    assert blob.signed is False


def test_gcs_upload_sets_private_no_store_and_digest_metadata():
    key = build_dream_media_object_key(
        uid=UID_A,
        pepper=b"pepper",
        pepper_version=1,
        content_type="image/png",
        dream_key="1" * 32,
        asset_key="2" * 32,
    ).object_key
    blob = _Blob(payload=b"", content_type="")
    store = GCSPrivateMediaStore(bucket_name="private", client=_Client({key: blob}))
    stored = store.upload(object_key=key, content_type="image/png", payload=PNG)

    assert stored.sha256 == hashlib.sha256(PNG).hexdigest()
    assert blob.cache_control == "private, no-store"
    assert blob.metadata == {"sha256": hashlib.sha256(PNG).hexdigest()}


def test_metadata_endpoint_contains_no_urls_and_media_endpoint_is_private(harness):
    repository, store, _clock, service = harness
    _upload(service)
    client = _client(service)

    metadata = client.get("/v1/ella/dreams")
    assert metadata.status_code == 200
    assert metadata.headers["cache-control"] == "private, no-store"
    assert metadata.headers["x-robots-tag"] == "noindex"
    assert metadata.headers["referrer-policy"] == "no-referrer"
    assert "url" not in json.dumps(metadata.json())
    assert "object_key" not in json.dumps(metadata.json())

    media = client.get("/v1/ella/dreams/dream-1/media")
    assert media.status_code == 200
    assert media.headers["cache-control"] == "private, no-store"
    assert media.headers["x-robots-tag"] == "noindex"
    assert media.json()["media"][0]["expires_in_seconds"] == 300
    inventory = repository.dreams[(UID_A, "dream-1")]["media"][0]
    assert store.objects[inventory["object_key"]]["cache_control"] == "private, no-store"
    assert "storage.googleapis.com" not in repr(repository.dreams)


def test_idor_and_missing_dream_return_same_404_without_signing(harness):
    _repository, store, _clock, service = harness
    _upload(service)
    other_user = _client(service, UID_B).get("/v1/ella/dreams/dream-1/media")
    missing = _client(service, UID_A).get("/v1/ella/dreams/missing/media")

    assert other_user.status_code == missing.status_code == 404
    assert other_user.json() == missing.json()
    assert store.signed_calls == []


def test_signed_url_ttls_expiry_and_unsigned_access(harness):
    _repository, store, clock, service = harness
    _upload(service, request_id="image-request")
    _upload(
        service,
        request_id="video-request",
        payload=FASTSTART_MP4,
        content_type="video/mp4",
    )
    response = service.get_media(UID_A, "dream-1")
    by_type = {entry["content_type"]: entry for entry in response["media"]}
    assert by_type["image/png"]["expires_in_seconds"] == 300
    assert by_type["video/mp4"]["expires_in_seconds"] == 900
    assert store.fetch_status(by_type["image/png"]["url"]) == 200
    unsigned = by_type["image/png"]["url"].split("?", 1)[0]
    assert store.fetch_status(unsigned) == 403
    clock.value += timedelta(seconds=301)
    assert store.fetch_status(by_type["image/png"]["url"]) == 403
    assert store.fetch_status(by_type["video/mp4"]["url"]) == 200
    clock.value += timedelta(seconds=600)
    assert store.fetch_status(by_type["video/mp4"]["url"]) == 403


def test_log_output_never_contains_signed_url_or_signature(harness, caplog):
    _repository, _store, _clock, service = harness
    _upload(service)
    with caplog.at_level(logging.INFO, logger="ella.services.dream_media"):
        response = service.get_media(UID_A, "dream-1")
    assert response["media"][0]["url"].startswith("https://storage.googleapis.com/")
    assert "storage.googleapis.com" not in caplog.text
    assert "X-Goog-Signature" not in caplog.text

    raw = "https://storage.googleapis.com/bucket/key?X-Goog-Credential=cred&X-Goog-Signature=secret"
    with caplog.at_level(logging.WARNING, logger="ella.services.dream_media"):
        logging.getLogger("ella.services.dream_media").warning("unexpected_url=%s", raw)
    redacted = redact_dream_media_credentials(raw)
    assert "secret" not in redacted
    assert "cred" not in redacted
    assert "?[REDACTED]" in redacted
    assert "secret" not in caplog.text
    assert "X-Goog-Signature=secret" not in caplog.text


def test_delete_removes_exact_inventory_and_tombstones(harness):
    repository, store, _clock, service = harness
    _upload(service, request_id="request-0001")
    _upload(service, request_id="request-0002", payload=JPEG, content_type="image/jpeg")
    inventory = copy.deepcopy(repository.dreams[(UID_A, "dream-1")]["media"])
    expected = {entry["object_key"] for entry in inventory}

    assert service.delete_dream(UID_A, "dream-1") is True
    assert set(store.deleted) == expected
    assert repository.dreams[(UID_A, "dream-1")]["tombstoned"] is True
    assert repository.dreams[(UID_A, "dream-1")]["media"] == []
    with pytest.raises(DreamMediaError, match="dream_media_not_found"):
        service.get_media(UID_A, "dream-1")


def test_orphan_sweep_deletes_planted_object(harness):
    _repository, store, _clock, service = harness
    owner = derive_uid_hash(UID_A, b"pepper-one")
    orphan = f"dreams/v1/p1/{owner}/{'a' * 32}/{'b' * 32}.png"
    store.objects[orphan] = {
        "payload": PNG,
        "content_type": "image/png",
        "cache_control": DREAM_MEDIA_CACHE_CONTROL,
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert service.sweep_user_orphans(UID_A) == 1
    assert orphan not in store.objects


def test_deletion_uses_inventory_across_pepper_rotation(harness):
    repository, store, clock, service = harness
    _upload(service, request_id="request-v1")
    service._configured_peppers = DreamMediaPepperConfig(
        active_version=2,
        known_versions=(1, 2),
        peppers={1: b"pepper-one", 2: b"pepper-two"},
    )
    _upload(service, request_id="request-v2", payload=JPEG, content_type="image/jpeg")
    inventory = copy.deepcopy(repository.dreams[(UID_A, "dream-1")]["media"])
    assert {entry["pepper_version"] for entry in inventory} == {1, 2}
    assert service.delete_dream(UID_A, "dream-1") is True
    assert {entry["object_key"] for entry in inventory} <= set(store.deleted)
    assert store.objects == {}
    assert clock.value == NOW


def test_source_exclusions_hide_and_block_dreams(harness):
    repository, _store, _clock, service = harness
    repository.sources[(UID_A, "source-1")] = {"discarded": False}
    _upload(service, source_memory_ids=("source-1",))
    repository.sources[(UID_A, "source-1")]["discarded"] = True
    assert service.list_dreams(UID_A) == []
    with pytest.raises(DreamMediaError, match="dream_media_not_found"):
        service.get_media(UID_A, "dream-1")
    with pytest.raises(DreamMediaError, match="dream_media_source_excluded"):
        _upload(service, dream_id="dream-2", request_id="request-0002", source_memory_ids=("source-1",))


def test_source_and_account_deletion_remove_media_before_ack(harness):
    repository, store, _clock, service = harness
    repository.sources[(UID_A, "source-1")] = {"discarded": False}
    _upload(service, source_memory_ids=("source-1",))
    first_key = repository.dreams[(UID_A, "dream-1")]["media"][0]["object_key"]
    assert service.delete_for_source(UID_A, "source-1") == 1
    assert first_key not in store.objects

    _upload(service, dream_id="dream-2", request_id="request-0002")
    second_key = repository.dreams[(UID_A, "dream-2")]["media"][0]["object_key"]
    assert service.delete_account(UID_A) == 1
    assert second_key not in store.objects


def test_account_deletion_retries_a_pending_dream_before_ack(harness):
    repository, store, _clock, service = harness
    _upload(service)
    object_key = repository.dreams[(UID_A, "dream-1")]["media"][0]["object_key"]
    store.fail_delete = True

    with pytest.raises(DreamMediaError, match="dream_media_delete_failed"):
        service.delete_account(UID_A)
    assert repository.dreams[(UID_A, "dream-1")]["deletion_pending"] is True
    assert object_key in store.objects

    store.fail_delete = False
    assert service.delete_account(UID_A) == 1
    assert object_key not in store.objects
    assert repository.dreams[(UID_A, "dream-1")]["tombstoned"] is True


def test_rotation_failure_remains_inventoried_and_retired_prefix_is_swept(harness):
    repository, store, clock, service = harness
    repository.fail_commit = True
    store.fail_delete = True
    with pytest.raises(DreamMediaError, match="dream_media_commit_failed"):
        _upload(service)
    pending = repository.dreams[(UID_A, "dream-1")]["media"][0]
    object_key = pending["object_key"]
    assert pending["state"] == "pending"
    assert object_key in store.objects
    with pytest.raises(DreamMediaError, match="dream_media_pepper_inventory_not_empty"):
        service.assert_pepper_retirable(1)

    repository.fail_commit = False
    store.fail_delete = False
    service._configured_peppers = DreamMediaPepperConfig(
        active_version=2,
        known_versions=(1, 2),
        peppers={2: b"pepper-two"},
    )
    clock.value += timedelta(hours=25)
    receipt = service.bucket_wide_sweep()
    assert {item["pepper_version"] for item in receipt["versions"]} == {1, 2}
    assert object_key not in store.objects
    assert repository.dreams[(UID_A, "dream-1")]["media"] == []
    service.assert_pepper_retirable(1)

    store.objects[object_key] = {
        "payload": PNG,
        "content_type": "image/png",
        "cache_control": DREAM_MEDIA_CACHE_CONTROL,
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    with pytest.raises(DreamMediaError, match="dream_media_pepper_prefix_not_empty"):
        service.assert_pepper_retirable(1)
    service.bucket_wide_sweep()
    assert object_key not in store.objects
    service.assert_pepper_retirable(1)


def test_internal_upload_contract_is_service_bound_and_private(harness):
    _repository, _store, _clock, service = harness
    app = FastAPI()
    app.include_router(dream_router.router)
    app.dependency_overrides[dream_router.get_dream_media_service] = lambda: service
    app.dependency_overrides[dream_router.require_dream_pipeline_authority] = lambda: EllaRequestAuthority(
        service="dream_pipeline",
        service_subject_uid=UID_A,
    )
    response = TestClient(app).post(
        "/v1/ella/internal/dreams/dream-1/media",
        data={
            "metadata": json.dumps(
                {
                    "request_id": "pipeline-request-1",
                    "title": "Dream",
                    "narrative": "Narrative",
                    "captions": ["Caption"],
                    "source_memory_ids": [],
                }
            )
        },
        files={"file": ("ignored-name.png", PNG, "image/png")},
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["asset"]["state"] == "committed"
    assert "object_key" not in response.json()["asset"]


def test_user_routes_require_firebase_auth_by_default(harness):
    _repository, _store, _clock, service = harness
    app = FastAPI()
    app.include_router(dream_router.router)
    app.dependency_overrides[dream_router.get_dream_media_service] = lambda: service
    response = TestClient(app).get("/v1/ella/dreams")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("get", "/v1/ella/dreams/missing/media", 404),
        ("get", "/v1/ella/dreams/!/media", 404),
    ],
)
def test_dream_media_failures_keep_private_response_policy(harness, method, path, expected_status):
    _repository, _store, _clock, service = harness
    response = getattr(_client(service), method)(path)
    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_pipeline_validation_failure_keeps_private_response_policy(harness):
    _repository, _store, _clock, service = harness
    app = FastAPI()
    app.include_router(dream_router.router)
    app.dependency_overrides[dream_router.get_dream_media_service] = lambda: service
    app.dependency_overrides[dream_router.require_dream_pipeline_authority] = lambda: EllaRequestAuthority(
        service="dream_pipeline",
        service_subject_uid=UID_A,
    )
    response = TestClient(app).post("/v1/ella/internal/dreams/dream-1/media")

    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
