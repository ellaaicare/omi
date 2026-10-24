"""Opt-in canonical history keysets. No stores, data writes, or logging.

Cursor authentication uses a domain-separated key derived from the existing
server-only Guardian key; deployment must separately approve that reuse.
This is live keyset traversal, not a snapshot of a concurrently changing archive.
"""
from __future__ import annotations
import base64, hashlib, hmac, json, re, time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone

VERSION = 1
ORDER = 'canonical-keyset-v1-c8'
SOURCE = 'canonical_timeline'
TTL_SECONDS = 3600
MAX_TOKEN_BYTES = 8192
I64 = 9223372036854775807
I32 = 2147483647

class HistoryPageError(ValueError):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)

def encoded(v):
    return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()

def digest(v): return hashlib.sha256(encoded(v)).hexdigest()

def integer(v, maximum):
    if type(v) is int: return v if 0 <= v <= maximum else None
    if isinstance(v,str) and re.fullmatch(r'0|[1-9][0-9]*',v) and len(v)<=len(str(maximum)):
        n=int(v);return n if n<=maximum else None
    return None

def first_string(*values):
    return next((v for v in values if isinstance(v,str) and v), '')

def key(event):
    stamp=event['started_at']
    if isinstance(stamp,str): stamp=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if not isinstance(stamp,datetime) or stamp.tzinfo is None: raise HistoryPageError('history_order_unavailable',503)
    meta=event.get('metadata') if isinstance(event.get('metadata'),dict) else {}
    ref=event.get('source_ref') if isinstance(event.get('source_ref'),dict) else {}
    eid=event['event_id'];ordinal=integer(meta.get('turn_ordinal'),I64)
    turn=first_string(meta.get('turn_id'),ref.get('client_message_id'),ref.get('turn_id'))
    if not turn:
        turn=eid[:-5] if eid.endswith(':user') else eid[:-10] if eid.endswith(':assistant') else eid
    seq=integer(meta.get('event_sequence'),I32)
    if seq is None: seq=0 if event.get('role')=='user' else 1 if event.get('role')=='assistant' else 2
    return (stamp.astimezone(timezone.utc),str(event.get('session_id') or ''),0 if ordinal is not None else 1,ordinal or 0,turn,seq,eid,str(event.get('source_identity') or ''))

def position(event):
    k=key(event)
    return [k[0].isoformat(timespec='microseconds'),*k[1:]]

def validate_position(v):
    if not isinstance(v,list) or len(v)!=8: raise HistoryPageError('history_cursor_invalid')
    if any(not isinstance(v[i],str) or len(v[i].encode())>2048 for i in (0,1,4,6,7)): raise HistoryPageError('history_cursor_invalid')
    if type(v[2]) is not int or v[2] not in (0,1) or type(v[3]) is not int or not 0<=v[3]<=I64 or type(v[5]) is not int or not 0<=v[5]<=I32: raise HistoryPageError('history_cursor_invalid')
    if v[2]==1 and v[3]!=0: raise HistoryPageError('history_cursor_invalid')
    try: stamp=datetime.fromisoformat(v[0])
    except ValueError: raise HistoryPageError('history_cursor_invalid') from None
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds()!=0 or stamp.isoformat(timespec='microseconds')!=v[0]: raise HistoryPageError('history_cursor_invalid')
    return (stamp,*v[1:])

def authority_projection(authority,uid):
    # Explicit secret-free production shape; never default=str or arbitrary vars().
    if (not is_dataclass(authority) or isinstance(authority,type)
        or type(authority).__name__!='CloudRuntimeAuthorityIdentity'
        or {f.name for f in fields(authority)}!={'uid','target_mode','digest'}
        or not getattr(type(authority),'__dataclass_params__').frozen):
        raise HistoryPageError('history_authority_unavailable',503)
    if authority.uid!=uid or not isinstance(authority.uid,str) or not isinstance(authority.target_mode,str) or not 1<=len(authority.target_mode)<=128 or not isinstance(authority.digest,str) or not re.fullmatch('[0-9a-f]{64}',authority.digest):
        raise HistoryPageError('history_authority_unavailable',503)
    return {'uid':authority.uid,'target_mode':authority.target_mode,'digest':authority.digest}

def scope(uid,authority,channels,limit):
    if not isinstance(uid,str) or not uid or type(limit) is not int or not 1<=limit<=200: raise HistoryPageError('history_page_invalid')
    return {'owner':hashlib.sha256(uid.encode()).hexdigest(),'query':digest({'source':SOURCE,'order':ORDER,'channels':sorted(set(channels)),'limit':limit,'mode':'hermes-cloud-chat'}),'authority':digest(authority_projection(authority,uid))}

def signing_key(secret):
    if not isinstance(secret,str) or len(secret.encode())<32: raise HistoryPageError('history_cursor_unavailable',503)
    return hmac.new(secret.encode(),b'ella.chat.history.cursor.v1',hashlib.sha256).digest()

def b64(raw): return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')

def unb64(text):
    if not isinstance(text,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',text): raise HistoryPageError('history_cursor_invalid')
    try: raw=base64.b64decode(text+'='*((-len(text))%4),altchars=b'-_',validate=True)
    except (ValueError,TypeError): raise HistoryPageError('history_cursor_invalid') from None
    if b64(raw)!=text: raise HistoryPageError('history_cursor_invalid')
    return raw

def seal(pos,expected,secret,now=None):
    validate_position(pos);now=int(time.time() if now is None else now)
    value={'version':VERSION,'order':ORDER,'source':SOURCE,**expected,'position':pos,'issued':now,'expires':now+TTL_SECONDS}
    raw=encoded(value);token=b64(raw)+'.'+b64(hmac.new(signing_key(secret),raw,hashlib.sha256).digest())
    if len(token)>MAX_TOKEN_BYTES: raise HistoryPageError('history_cursor_unavailable',503)
    return token

def open_cursor(token,expected,secret,now=None):
    if not isinstance(token,str) or not 1<=len(token)<=MAX_TOKEN_BYTES or token.count('.')!=1: raise HistoryPageError('history_cursor_invalid')
    a,b=token.split('.');raw,sig=unb64(a),unb64(b)
    if len(sig)!=32 or not hmac.compare_digest(sig,hmac.new(signing_key(secret),raw,hashlib.sha256).digest()): raise HistoryPageError('history_cursor_invalid')
    def pairs(items):
        d={}
        for k,v in items:
            if k in d: raise HistoryPageError('history_cursor_invalid')
            d[k]=v
        return d
    try: value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(HistoryPageError('history_cursor_invalid')))
    except (ValueError,UnicodeError): raise HistoryPageError('history_cursor_invalid') from None
    if not isinstance(value,dict) or set(value)!={'version','order','source','owner','query','authority','position','issued','expires'}: raise HistoryPageError('history_cursor_invalid')
    if type(value['version']) is not int or value['version']!=VERSION or value['order']!=ORDER or value['source']!=SOURCE: raise HistoryPageError('history_cursor_invalid')
    if value['owner']!=expected['owner']: raise HistoryPageError('history_cursor_owner_mismatch',403)
    if any(value[k]!=expected[k] for k in ('query','authority')): raise HistoryPageError('history_cursor_scope_changed',409)
    now=int(time.time() if now is None else now)
    if type(value['issued']) is not int or type(value['expires']) is not int or value['expires']-value['issued']!=TTL_SECONDS: raise HistoryPageError('history_cursor_invalid')
    if value['issued']>now or value['expires']<=now: raise HistoryPageError('history_cursor_expired',409)
    return validate_position(value['position'])
