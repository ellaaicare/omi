"""Owner-bound canonical memory reads shared by chat and voice.

No writes, model calls or legacy-history fallback. Empty reads are available;
failed reads are unavailable. Text is never logged. Runtime checks are sampled
before/after the read, not an atomic transaction across authority stores.
"""
from __future__ import annotations
import asyncio
import json
import hashlib
from dataclasses import dataclass

MAX_BYTES = 1048576

class CanonicalMemoryUnavailable(RuntimeError):
    code = 'canonical_memory_unavailable'

@dataclass
class Dependencies:
    resolve: object
    identity: object
    consent: object
    recent: object
    detail: object
    page: object = None

async def _production_dependencies():
    from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_authority_identity
    from ella.services.ai_consent import assert_current_ai_consent
    from ella.routers.canonical_events import PostgresCanonicalEventStore, _get_pool, _row_to_event
    store = PostgresCanonicalEventStore()
    async def recent(uid, limit, since, channels, before):
        return await store.timeline(uid=uid, limit=limit, since=since, channels=channels, before=before)
    async def detail(uid, cid):
        pool = await _get_pool()
        rows = await pool.fetch('''SELECT uid,canonical_identity,event_id,source_identity,session_id,channel,provider,role,text,started_at,ended_at,privacy_scope,scan_policy,source_ref,metadata,raw_event,inserted_at FROM canonical_events
            WHERE uid=$1 AND canonical_identity=$1 AND channel=ANY($3::text[])
            AND source_ref->>'conversation_id'=$2
            ORDER BY started_at DESC, event_id DESC LIMIT 101''', uid, cid, ['omi','omi_transcript'])
        return [_row_to_event(row) for row in rows]
    async def page(uid, limit, channels, position):
        return await store.history_page(uid=uid, limit=limit, channels=channels, position=position)
    return Dependencies(resolve_isolated_runtime, runtime_authority_identity,
        assert_current_ai_consent, recent, detail, page)

async def read_memory(uid, *, target_mode, limit=50, since=None, channels=None, before=None,
                      conversation_id=None, expected_summary_version=None,
                      dependencies=None, max_bytes=MAX_BYTES):
    """UID must already come from the existing authenticated route/principal.

    An optional summary-version precondition prevents silently returning a newer
    summary. Raw transcript source hashes/revisions remain separate evidence.
    """
    if not isinstance(uid,str) or not uid or not 1<=limit<=500 or not 1<=max_bytes<=MAX_BYTES:
        raise CanonicalMemoryUnavailable()
    d=dependencies
    async def guard():
        if await asyncio.to_thread(d.consent,uid)!=uid: raise CanonicalMemoryUnavailable()
        runtime=await d.resolve(uid,target_mode=target_mode)
        if runtime is None or runtime.uid!=uid or runtime.status!='active': raise CanonicalMemoryUnavailable()
        return d.identity(runtime)
    try:
        async with asyncio.timeout(10):
            d = d or await _production_dependencies()
            authority_before=await guard()
            rows=await (d.detail(uid,conversation_id) if conversation_id else d.recent(uid,limit,since,channels,before))
            if not isinstance(rows,list) or any(not isinstance(r,dict) or r.get('uid')!=uid
                    or r.get('canonical_identity')!=uid for r in rows): raise CanonicalMemoryUnavailable()
            if conversation_id and (len(rows)>100 or any(r.get('source_ref',{}).get('conversation_id')!=conversation_id for r in rows)):
                raise CanonicalMemoryUnavailable()
            if expected_summary_version is not None:
                summaries=[r for r in rows if r.get('channel')=='omi']
                if len(summaries)!=1 or summaries[0].get('source_ref',{}).get('active_summary_version_id')!=expected_summary_version:
                    raise CanonicalMemoryUnavailable()
            # Recent-list consumers use the canonical projection. raw_event is the
            # duplicated ingestion envelope, retained unchanged in durable storage
            # and exact-detail reads; it is not needed to render Chat history.
            raw_event_omitted = not conversation_id and any('raw_event' in r for r in rows)
            if not conversation_id:
                rows = [{k: v for k, v in r.items() if k != 'raw_event'} for r in rows]
            if len(json.dumps(rows,default=str,ensure_ascii=False).encode())>max_bytes: raise CanonicalMemoryUnavailable()
            if await guard()!=authority_before: raise CanonicalMemoryUnavailable()
            return {'status':'available','events':rows,'truncated':False,
                    'possibly_more':bool(not conversation_id and len(rows)==limit),
                    'raw_event_omitted':raw_event_omitted}
    except CanonicalMemoryUnavailable: raise
    except Exception: raise CanonicalMemoryUnavailable() from None


def render_memory(value, *, max_chars=9000):
    lines=[]; clipped=False; fingerprints=[]
    # timeline() returns a chronological recent window, while exact detail
    # is newest-first. Normalize only rendering; preserve reader/history order.
    from datetime import datetime
    def recency(event):
        stamp = event.get('started_at')
        try:
            instant = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError('naive_capture_time')
            return instant.timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise CanonicalMemoryUnavailable() from None
    for e in sorted(value['events'], key=recency, reverse=True):
        ref=e.get('source_ref') or {}; original=e.get('text') or ''
        record={'event_id':e.get('event_id'),'source_identity':e.get('source_identity'),
            'conversation_id':ref.get('conversation_id'),'summary_version':ref.get('active_summary_version_id'),
            'source_sha256':ref.get('source_sha256'),'channel':e.get('channel'),
            'capture_time':str(e.get('started_at')),'attribution':'source speaker labels; user role does not prove owner speech',
            'text':original[:2000],'excerpt_truncated':len(original)>2000}
        line=json.dumps(record,ensure_ascii=False)
        if len(line)+sum(len(x)+1 for x in lines)>max_chars:
            clipped=True
            continue
        lines.append(line);clipped=clipped or record['excerpt_truncated']
        cid, version = record['conversation_id'], record['summary_version']
        if isinstance(cid, str) and isinstance(version, str) and cid and version:
            fingerprints.append(hashlib.sha256(json.dumps(
                {'conversation_id':cid,'summary_version_id':version},
                sort_keys=True,separators=(',',':')).encode()).hexdigest())
    text='\n'.join(lines)
    return {'text':text,'truncated':clipped,
            'detail_required':clipped,'status':value['status'],
            'selected_event_count':len(lines),
            'source_version_fingerprints':fingerprints,
            'rendered_text_sha256':hashlib.sha256(text.encode()).hexdigest()}


def search_memory(value, query, limit=10, full_access=True):
    words=[w.lower() for w in query.split() if len(w)>2]
    results=[]
    for e in value['events']:
        text=e.get('text') or ''
        score=sum(w in text.lower() for w in words)
        if words and not score:continue
        ref=e.get('source_ref') or {}
        results.append({'source':'canonical_memory','title':e.get('channel') or 'Canonical memory',
            'content':text if full_access else '', 'timestamp':str(e.get('started_at')),
            'score':score,'metadata':{'provenance':'canonical_memory','event_id':e.get('event_id'),
                'source_identity':e.get('source_identity'),'conversation_id':ref.get('conversation_id'),
                'active_summary_version_id':ref.get('active_summary_version_id'),
                'source_sha256':ref.get('source_sha256'),'channel':e.get('channel'),
                'attribution':'user-role-is-not-speaker-identity','truncated':False}})
    return sorted(results,key=lambda r:r['score'],reverse=True)[:limit]


async def read_history_page(uid, *, limit, channels, cursor=None, secret=None, dependencies=None, now=None):
    """Canonical-only live keyset. Every page repeats owner/consent/runtime fences."""
    from utils.ella.history_pagination import (HistoryPageError, signing_key, scope,
        open_cursor, seal, position, key, ORDER, SOURCE)
    signing_key(secret)
    if type(limit) is not int or not 1<=limit<=200 or not isinstance(channels,list) or not channels or any(not isinstance(c,str) or not c for c in channels):
        raise HistoryPageError('history_page_invalid')
    d=dependencies
    async def guard():
        if await asyncio.to_thread(d.consent,uid)!=uid: raise CanonicalMemoryUnavailable()
        runtime=await d.resolve(uid,target_mode='hermes-cloud-chat')
        if runtime is None or runtime.uid!=uid or runtime.status!='active': raise CanonicalMemoryUnavailable()
        return d.identity(runtime)
    try:
        async with asyncio.timeout(10):
            d=d or await _production_dependencies()
            before=await guard();expected=scope(uid,before,channels,limit)
            last=open_cursor(cursor,expected,secret,now=now) if cursor is not None else None
            rows=await d.page(uid,limit,channels,last)
            if not isinstance(rows,list) or len(rows)>limit+1 or any(not isinstance(r,dict) or r.get('uid')!=uid or r.get('canonical_identity')!=uid for r in rows): raise CanonicalMemoryUnavailable()
            keys=[key(row) for row in rows]
            if any(keys[i]>=keys[i-1] for i in range(1,len(keys))) or (last is not None and any(k>=last for k in keys)): raise CanonicalMemoryUnavailable()
            selected=[{k:v for k,v in row.items() if k!='raw_event'} for row in rows[:limit]]
            if len(json.dumps(selected,default=str,ensure_ascii=False).encode())>MAX_BYTES: raise CanonicalMemoryUnavailable()
            if await guard()!=before: raise HistoryPageError('history_cursor_scope_changed',409)
            more=len(rows)>limit
            return {'events':selected,'hasMore':more,'nextCursor':seal(position(selected[-1]),expected,secret,now=now) if more else None,
                'paginationVersion':1,'pageOrder':ORDER,'source':SOURCE,'fallback':False,
                'canonical_memory_status':'available' if selected else 'available_empty'}
    except (HistoryPageError,CanonicalMemoryUnavailable): raise
    except Exception: raise CanonicalMemoryUnavailable() from None
