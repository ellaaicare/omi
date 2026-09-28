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
  idempotent replay is handled per-VAD-segment (see `compute_sync_segment_id` /
  `get_cached_sync_segment_result` / `cache_sync_segment_result` below) rather than per-job.
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
# How long a successfully-processed segment's outcome is remembered so a WAL replay (same audio
# bytes re-uploaded after a dropped response / app relaunch) is a no-op instead of a duplicate
# conversation write. Generous enough to cover realistic client retry windows.
SEGMENT_RESULT_TTL_SECONDS = 7 * 24 * 60 * 60

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


def get_cached_sync_segment_result(segment_id: str) -> Optional[dict[str, str]]:
    """Returns {'kind': 'new_memories'|'updated_memories', 'conversation_id': str} if this exact
    segment was already durably processed, else None."""
    raw = redis_client.get(f'sync_v2_segment:{segment_id}')
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
        if data.get('kind') in ('new_memories', 'updated_memories') and data.get('conversation_id'):
            return {'kind': data['kind'], 'conversation_id': data['conversation_id']}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def cache_sync_segment_result(segment_id: str, kind: str, conversation_id: str) -> None:
    redis_client.set(
        f'sync_v2_segment:{segment_id}',
        json.dumps({'kind': kind, 'conversation_id': conversation_id}),
        ex=SEGMENT_RESULT_TTL_SECONDS,
    )


# How long one execution may hold exclusive processing rights over a single VAD segment. Bounds
# how long a crashed/hung claimant can block a legitimate retry of the same segment (SYNC-V2-002):
# generous relative to one segment's STT + LLM + persistence latency, short relative to a human
# noticing a stuck upload.
SEGMENT_CLAIM_TTL_SECONDS = 15 * 60


def claim_sync_segment(segment_id: str, claimant: str) -> bool:
    """Atomically claim exclusive processing rights for one VAD segment via Redis `SET NX EX`.

    Returns True if [claimant] now holds the claim (safe to run STT/LLM/persistence for this
    segment id); False if another claimant currently holds an unexpired lease. Combined with
    `get_cached_sync_segment_result`, this makes concurrent retries of the same segment content
    (e.g. two in-flight uploads racing after a dropped response) run STT/LLM/persistence exactly
    once instead of both proceeding past a non-atomic get/process/set check.
    """
    return bool(
        redis_client.set(f'sync_v2_segment_claim:{segment_id}', claimant, nx=True, ex=SEGMENT_CLAIM_TTL_SECONDS)
    )


def release_sync_segment_claim(segment_id: str, claimant: str) -> None:
    """Release a claim early (e.g. after a failed attempt) so a retry doesn't have to wait out the
    full lease TTL. No-ops if [claimant] no longer holds it — already expired and possibly
    reclaimed by someone else — so this never deletes a lease it doesn't own."""
    key = f'sync_v2_segment_claim:{segment_id}'
    current = redis_client.get(key)
    if isinstance(current, bytes):
        current = current.decode()
    if current == claimant:
        redis_client.delete(key)


# How long a per-conversation update lock may be held (SYNC-V2-003) and how long a segment will
# wait to acquire one. One segment update (merge + Firestore write) is fast; this only needs to
# outlast realistic contention between a handful of segments targeting the same conversation.
CONVERSATION_UPDATE_LOCK_TTL_SECONDS = 60
CONVERSATION_UPDATE_LOCK_WAIT_SECONDS = 30
CONVERSATION_UPDATE_LOCK_POLL_INTERVAL_SECONDS = 0.1


def acquire_conversation_update_lock(uid: str, conversation_id: str, claimant: str) -> bool:
    """Blocking-with-timeout Redis mutex serializing read-modify-write updates to one
    conversation's segments, so parallel VAD segments explicitly targeting the same conversation
    can't both read a stale snapshot and drop each other's write. Returns False on timeout."""
    key = f'sync_v2_conversation_lock:{uid}:{conversation_id}'
    deadline = time.monotonic() + CONVERSATION_UPDATE_LOCK_WAIT_SECONDS
    while True:
        if redis_client.set(key, claimant, nx=True, ex=CONVERSATION_UPDATE_LOCK_TTL_SECONDS):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(CONVERSATION_UPDATE_LOCK_POLL_INTERVAL_SECONDS)


def release_conversation_update_lock(uid: str, conversation_id: str, claimant: str) -> None:
    key = f'sync_v2_conversation_lock:{uid}:{conversation_id}'
    current = redis_client.get(key)
    if isinstance(current, bytes):
        current = current.decode()
    if current == claimant:
        redis_client.delete(key)
