"""Server-signed, replay-safe manifests binding a `/v2/sync-local-files` upload to a conversation,
plus segment-level idempotency for the same endpoint.

Ported from BasedHardware/omi (upstream commit f16699aea7fe9ba089baceb628922f2882c51153),
`backend/utils/sync/capture_manifest.py` and `backend/utils/sync/content_id.py`, adapted to this
fork's simpler, synchronous sync pipeline:

- No client-device binding. Upstream binds a manifest to a verified `client_device_id` resolved
  from request headers (`resolve_client_device`); this fork's sync routes do not yet resolve a
  verified device identity, so the manifest is scoped to (uid, conversation_id, file claims) only.
  A stolen/replayed manifest token is still only usable by the same uid it was issued for (the
  token is HMAC-signed and `uid` is part of the signed payload), and it can only ever attach audio
  to a conversation that `conversations_db.get_conversation(uid, conversation_id)` resolves for
  that same uid.
- No Firestore-backed sync ledger / job queue (upstream's `claim_sync_content` /
  `database.sync_jobs`). This fork processes uploads synchronously like `/v1/sync-local-files`, so
  idempotent replay is handled per-VAD-segment (see `compute_sync_segment_id` below, and
  `database.sync_segments` for the durable claim/idempotency record keyed on it) rather than
  per-job.

`compute_sync_segment_id` is the only segment-idempotency piece still in this module; the claim,
lease, and result-cache it used to back (previously Redis `SET NX EX` keys) now live in
`database.sync_segments` as durable Firestore records -- see that module's docstring for why.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from database.redis_db import r as redis_client

MANIFEST_TTL_SECONDS = 15 * 60
MANIFEST_CLAIM_TTL_SECONDS = 6 * 60 * 60

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


def _secret() -> bytes:
    value = os.getenv('SYNC_CONTENT_ID_SECRET') or os.getenv('ENCRYPTION_SECRET')
    if not value:
        raise RuntimeError('SYNC_CONTENT_ID_SECRET or ENCRYPTION_SECRET is required for capture manifests')
    return value.encode()


def validate_file_claims(raw_claims: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for raw in raw_claims:
        name = Path(str(raw.get('name', ''))).name
        digest = str(raw.get('sha256', '')).lower()
        if not name or name != str(raw.get('name', '')) or not _SHA256_RE.fullmatch(digest):
            raise ValueError('invalid capture manifest file claim')
        claims.append({'name': name, 'sha256': digest})
    if not claims:
        raise ValueError('capture manifest requires at least one file')
    return sorted(claims, key=lambda item: (item['name'], item['sha256']))


def issue_capture_manifest(
    uid: str,
    conversation_id: str,
    file_claims: Iterable[dict[str, Any]],
    *,
    now: Optional[int] = None,
) -> str:
    """Mint a short-lived, HMAC-signed, stateless token binding [file_claims] to (uid, conversation_id)."""
    issued_at = int(time.time()) if now is None else now
    payload = {
        'v': 1,
        'uid': uid,
        'conversation': conversation_id,
        'files': validate_file_claims(file_claims),
        'iat': issued_at,
        'exp': issued_at + MANIFEST_TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=')
    signature = hmac.new(_secret(), encoded, hashlib.sha256).hexdigest().encode()
    return f'{encoded.decode()}.{signature.decode()}'


def claim_conversation_manifest(uid: str, conversation_id: str, file_claims: Iterable[dict[str, Any]]) -> bool:
    """Allow one immutable fresh-capture file set per (uid, conversation). Idempotent: replaying the
    exact same claim for the same conversation returns True again instead of failing."""
    claims = validate_file_claims(file_claims)
    fingerprint = hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    key = f'sync_capture_manifest:{uid}:{conversation_id}'
    if redis_client.set(key, fingerprint, nx=True, ex=MANIFEST_CLAIM_TTL_SECONDS):
        return True
    existing = redis_client.get(key)
    if isinstance(existing, bytes):
        existing = existing.decode()
    return existing == fingerprint


def verify_capture_manifest(
    token: Optional[str],
    uid: str,
    conversation_id: Optional[str],
    filenames: Iterable[str],
    *,
    now: Optional[int] = None,
) -> Optional[list[dict[str, str]]]:
    """Validate a manifest token against the uploading uid/conversation/filenames. Returns the
    validated file claims, or None if the token is missing, malformed, expired, or does not match
    (never raises — an invalid/absent manifest just means "no fresh-capture proof", not a hard
    failure, matching how the vendored client treats a failed manifest issuance)."""
    if not token or not conversation_id:
        return None
    try:
        encoded_text, signature = token.split('.', 1)
        encoded = encoded_text.encode()
        expected = hmac.new(_secret(), encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padding = '=' * (-len(encoded_text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_text + padding))
        effective_now = int(time.time()) if now is None else now
        if (
            payload.get('v') != 1
            or payload.get('uid') != uid
            or payload.get('conversation') != conversation_id
            or int(payload.get('iat', 0)) > effective_now + 60
            or int(payload.get('exp', 0)) < effective_now
        ):
            return None
        claims = validate_file_claims(payload.get('files') or [])
        expected_names = sorted(Path(filename).name for filename in filenames)
        if [claim['name'] for claim in claims] != expected_names:
            return None
        return claims
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def manifest_claims_match_paths(claims: list[dict[str, str]], paths: Iterable[str]) -> bool:
    actual: list[dict[str, str]] = []
    for path in paths:
        digest = hashlib.sha256()
        with open(path, 'rb') as audio_file:
            while chunk := audio_file.read(1024 * 1024):
                digest.update(chunk)
        actual.append({'name': Path(path).name, 'sha256': digest.hexdigest()})
    return sorted(actual, key=lambda item: (item['name'], item['sha256'])) == claims


def compute_sync_segment_id(uid: str, path: str) -> str:
    """Stable identity for one VAD-segmented audio chunk, keyed on its content bytes (not its
    temp-directory path), so the same audio replayed after a crash/relaunch maps to the same id."""
    digest = hashlib.sha256()
    with open(path, 'rb') as audio_file:
        while chunk := audio_file.read(1024 * 1024):
            digest.update(chunk)
    logical_name = Path(path).name
    payload = f'{uid}\n{logical_name}\n{digest.hexdigest()}'
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
