"""Private read projection; no generation, enrichment, or writeback capability."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

VERSION = 'ella.muse.private-sources.v1'
MAX_PAGE = 50
MAX_TEXT_BYTES = 131072
CURSOR_SECONDS = 900


class ReaderError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _integer(value: Any) -> bool:
    return type(value) is int and 0 <= value < 2**63


def _key(secret: str) -> bytes:
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise ReaderError('reader_unavailable', 503)
    return secret.encode()


def issue_cursor(uid: str, authority: str, after: int, watermark: int,
                 secret: str, now: float) -> str:
    value = {'v': 1, 'owner': digest(uid), 'authority': authority,
             'after': after, 'watermark': watermark, 'expires': int(now) + CURSOR_SECONDS}
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    signature = hmac.new(_key(secret), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def read_cursor(token: str, uid: str, authority: str, secret: str, now: float) -> dict:
    key = _key(secret)
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        payload, signature = token.split('.')
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if set(value) != {'v', 'owner', 'authority', 'after', 'watermark', 'expires'}:
            raise ValueError
        if type(value['v']) is not int or value['v'] != 1:
            raise ValueError
        if value['owner'] != digest(uid) or value['authority'] != authority:
            raise ValueError
        if not all(_integer(value[k]) for k in ('after', 'watermark', 'expires')):
            raise ValueError
        if value['after'] > value['watermark']:
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        raise ReaderError('cursor_invalid', 409) from None
    if now >= value['expires']:
        raise ReaderError('cursor_expired', 410)
    return value


def project(row: dict, uid: str, *, body: bool = False) -> dict:
    # Filtering alone is insufficient: validate each returned row's owner too.
    if row.get('uid') != uid or row.get('canonical_identity') != uid:
        raise ReaderError('source_unavailable', 503)
    if not _integer(row.get('sequence')) or row['sequence'] == 0:
        raise ReaderError('source_unavailable', 503)
    if row.get('operation') not in ('upsert', 'withdraw'):
        raise ReaderError('source_unavailable', 503)
    data = row.get('payload')
    if (not isinstance(data, dict)
            or set(data) != {'channel', 'projection', 'started_at', 'body_hash', 'source_ref'}
            or data.get('channel') not in ('omi_transcript','muse_fictional_test')
            or data.get('projection') not in ('raw_event_text', 'segment_text_join_v1')):
        raise ReaderError('source_unavailable', 503)
    if (not isinstance(row.get('source_id'), str) or len(row['source_id']) != 64
            or any(c not in '0123456789abcdef' for c in row['source_id'])):
        raise ReaderError('source_unavailable', 503)
    reference = data.get('source_ref') or {}
    if not isinstance(reference, dict) or set(reference) - {'conversation_id', 'transcript_sha256'}:
        raise ReaderError('source_unavailable', 503)
    cid = reference.get('conversation_id')
    if cid is not None and (not isinstance(cid, str) or not 0 < len(cid) <= 256):
        raise ReaderError('source_unavailable', 503)
    upstream = reference.get('transcript_sha256')
    if upstream is not None and (not isinstance(upstream, str) or len(upstream) > 80):
        raise ReaderError('source_unavailable', 503)
    if any(not isinstance(x, str) or not 0 < len(x) <= 80
           for x in (data.get('started_at'), row.get('available_at'))):
        raise ReaderError('source_unavailable', 503)
    previous = row.get('previous_sequence')
    if previous is not None and (not _integer(previous) or not 0 < previous < row['sequence']):
        raise ReaderError('source_unavailable', 503)
    # The durable change projection contains no transcript body. Exact GET joins
    # the current owner-private canonical row and verifies its recorded hash.
    text = row.get('current_text', '') if row['operation'] == 'upsert' else ''
    if not isinstance(text, str):
        raise ReaderError('source_unavailable', 503)
    if len(text.encode()) > MAX_TEXT_BYTES:
        raise ReaderError('source_too_large', 413)
    body_hash = data.get('body_hash') if row['operation'] == 'upsert' else hashlib.sha256(b'').hexdigest()
    if (not isinstance(body_hash, str) or len(body_hash) != 64
            or any(c not in '0123456789abcdef' for c in body_hash)):
        raise ReaderError('source_unavailable', 503)
    if body and hashlib.sha256(text.encode()).hexdigest() != body_hash:
        raise ReaderError('source_changed', 409)
    # This is a projection envelope digest, not the upstream transcript-grounding hash.
    revision = digest({'source_id': row['source_id'], 'operation': row['operation'],
                       'payload': data if row['operation'] == 'upsert' else None})
    logical_id = digest([uid, cid]) if isinstance(cid, str) and cid else row['source_id']
    result = {'source_id': row['source_id'], 'logical_source_id': logical_id,
              'sequence': row['sequence'], 'revision': revision,
              'event_type': row['operation'], 'source_created_at': data.get('started_at'),
              'source_available_at': row.get('available_at'), 'source_kind': data['channel'],
              'body_hash': body_hash,
              'supersedes_sequence': row.get('previous_sequence'),
              'projection': data.get('projection'),
              'canonical_summary_version': reference.get('active_summary_version_id'),
              'upstream_transcript_sha256': reference.get('transcript_sha256')}
    if body:
        result['text'] = text
    return result


@dataclass
class Dependencies:
    guard: Any
    store: Any
    secret: str
    clock: Any = time.time


class PrivateReader:
    def __init__(self, dependencies: Dependencies):
        self.d = dependencies

    async def _authority(self, uid: str) -> str:
        if not isinstance(uid, str) or not uid:
            raise ReaderError('authentication_required', 401)
        value = await self.d.guard(uid)
        if not isinstance(value, str) or len(value) != 64:
            raise ReaderError('authority_unavailable', 503)
        return value

    async def sources(self, uid: str, *, cursor: str | None = None, limit: int = 20) -> dict:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ReaderError('limit_invalid', 422)
        async with asyncio.timeout(10):
            authority = await self._authority(uid)
            now = self.d.clock()
            position = read_cursor(cursor, uid, authority, self.d.secret, now) if cursor else None
            # First request creates a NOW baseline, never imports historical personal sources.
            watermark, rows = await self.d.store.window(uid, position, limit + 1)
            if not _integer(watermark) or not isinstance(rows, list):
                raise ReaderError('feed_unavailable', 503)
            baseline = position is None
            after = watermark if baseline else position['after']
            if baseline and rows:
                raise ReaderError('feed_unavailable', 503)
            if position and watermark < position['watermark']:
                raise ReaderError('feed_reset', 409)
            if position and position['after'] < position['watermark'] and watermark != position['watermark']:
                raise ReaderError('snapshot_changed', 409)
            events = [project(dict(row), uid) for row in rows]
            numbers = [e['sequence'] for e in events]
            if numbers != sorted(set(numbers)) or any(n <= after or n > watermark for n in numbers):
                raise ReaderError('feed_unavailable', 503)
            has_more = len(events) > limit
            events = events[:limit]
            if events:
                after = events[-1]['sequence']
            if not has_more:
                after = watermark
            if await self._authority(uid) != authority:
                raise ReaderError('authority_changed', 409)
            if position and self.d.clock() >= position['expires']:
                raise ReaderError('cursor_expired', 410)
            return {'contract': VERSION, 'owner_binding': digest([uid, authority]),
                    'events': events, 'baseline': baseline, 'has_more': has_more,
                    'snapshot_watermark': watermark,
                    'next_cursor': issue_cursor(uid, authority, after, watermark,
                                                self.d.secret, self.d.clock()),
                    'observed_at_unix': self.d.clock(), 'content_included': False}

    async def source(self, uid: str, source_id: str, revision: str) -> dict:
        if any(not isinstance(x, str) or len(x) != 64 or any(c not in '0123456789abcdef' for c in x)
               for x in (source_id, revision)):
            raise ReaderError('source_key_invalid', 422)
        async with asyncio.timeout(10):
            authority = await self._authority(uid)
            row = await self.d.store.latest(uid, source_id)
            if row is None:
                raise ReaderError('source_not_found', 404)
            result = project(dict(row), uid, body=True)
            if result['event_type'] == 'withdraw':
                raise ReaderError('source_withdrawn', 410)
            if result['revision'] != revision:
                raise ReaderError('source_superseded', 409)
            if await self._authority(uid) != authority:
                raise ReaderError('authority_changed', 409)
            fresh = await self.d.store.latest(uid, source_id)
            if fresh is None or project(dict(fresh), uid, body=True) != result:
                raise ReaderError('source_changed', 409)
            if await self._authority(uid) != authority:
                raise ReaderError('authority_changed', 409)
            return {'contract': VERSION, 'owner_binding': digest([uid, authority]),
                    **result, 'citation': {'source_id': source_id, 'revision': revision,
                    'body_hash': result['body_hash']}, 'observed_at_unix': self.d.clock()}

    async def necklace_status(self, uid: str) -> dict:
        async with asyncio.timeout(10):
            authority = await self._authority(uid)
            status = await self.d.store.capture_status(uid)
            if await self._authority(uid) != authority:
                raise ReaderError('authority_changed', 409)
            allowed = {'server_capture_state', 'server_capture_lease_current',
                       'capture_pointer_present', 'socket_owner_present', 'server_observed_at'}
            if not isinstance(status, dict) or set(status) != allowed:
                raise ReaderError('status_unavailable', 503)
            if status['server_capture_state'] not in ('unknown', 'active', 'drained', 'finalizing', 'terminal'):
                raise ReaderError('status_unavailable', 503)
            if any(type(status[k]) is not bool for k in
                   ('server_capture_lease_current', 'capture_pointer_present', 'socket_owner_present')):
                raise ReaderError('status_unavailable', 503)
            return {'contract': VERSION, 'owner_binding': digest([uid, authority]),
                    **status, 'device_status': 'unknown', 'last_upload_at': None,
                    'native_or_provider_drain_proven': False,
                    'observed_at_unix': self.d.clock()}
