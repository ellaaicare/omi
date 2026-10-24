"""Fixed-subject, private-file-authorized, one-use in-service HTTP read probe.

No route/tool is registered. Missing scope disables it. Never copies credentials
or configures environment. Runtime source/deployment/release are separate gates.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

CONTROL = Path('/var/lib/ella/canonical-auth-readiness-v1')
SUBJECT_SHA256 = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'
HELPER_SHA256 = '3bc2be1ea872145712c05d32100974143ad82f2dbc3fba6f648b705708100724'
ROUTER_SHA256 = 'e5f4e2e90373d7bd7500a2fa888b30a14f19d198f77e04ae750d1db8a4425ce2'
URL = 'http://127.0.0.1:8000/v1/ella/timeline'
SCOPE_KEYS = {'version','purpose','attempt_id','uid','created_unix','not_before_unix','expires_unix','module_sha256'}
GO_KEYS = {'version','attempt_id','scope_sha256','pid','verified_unix','account_consent_match'}
_task = None

class Hold(ValueError):
    pass

def need(value, code):
    if not value:
        raise Hold(code)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def strict(raw):
    def pairs(items):
        d = {}
        for key,value in items:
            need(key not in d,'json_shape')
            d[key]=value
        return d
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(Hold('json_shape')))

def numeric(x):
    return type(x) in (int,float) and math.isfinite(x)

def validate_scope(scope, now, module_sha256, subject_sha256=SUBJECT_SHA256):
    need(type(scope) is dict and set(scope)==SCOPE_KEYS,'scope_shape')
    need(type(scope['version']) is int and scope['version']==1 and scope['purpose']=='one_canonical_auth_readiness_get','scope_authority')
    need(type(scope['attempt_id']) is str and str(uuid.UUID(scope['attempt_id']))==scope['attempt_id'],'scope_shape')
    uid=scope['uid'];need(type(uid) is str and 0<len(uid)<=128 and uid==uid.strip() and sha(uid.encode())==subject_sha256,'subject_mismatch')
    need(scope['module_sha256']==module_sha256,'source_drift')
    need(all(numeric(scope[k]) for k in ('created_unix','not_before_unix','expires_unix')),'scope_clock')
    need(scope['created_unix']<=now<scope['expires_unix'] and scope['created_unix']<=scope['not_before_unix']<scope['expires_unix']-10 and scope['expires_unix']<=scope['created_unix']+180,'scope_expired')
    return uid

def validate_go(go, scope, raw_scope, now, pid):
    need(type(go) is dict and set(go)==GO_KEYS,'go_shape')
    need(type(go['version']) is int and go['version']==1 and go['attempt_id']==scope['attempt_id'] and go['scope_sha256']==sha(raw_scope),'go_binding')
    need(type(go['pid']) is int and go['pid']==pid and go['account_consent_match'] is True,'go_binding')
    need(numeric(go['verified_unix']) and 0<=now-go['verified_unix']<=30 and scope['not_before_unix']<=now<scope['expires_unix']-10,'go_expired')

class LocalIO:
    clock=staticmethod(time.time)
    mono=staticmethod(time.monotonic)
    pid=staticmethod(os.getpid)
    subject_sha256=SUBJECT_SHA256
    def _directory(self):
        for p in reversed((CONTROL,*CONTROL.parents)):
            info=p.lstat()
            need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not stat.S_IMODE(info.st_mode)&0o022,'private_path')
        need(stat.S_IMODE(CONTROL.stat().st_mode)==0o700,'private_path')
    def read(self, name):
        self._directory()
        fd=os.open(CONTROL/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd);need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=16384,'private_file')
            raw=os.read(fd,16385);after=os.fstat(fd)
            need(len(raw)==info.st_size and (info.st_ino,info.st_size,info.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns),'private_file_changed')
            return raw
        finally:os.close(fd)
    def save(self, name, value):
        self._directory()
        fd=os.open(CONTROL/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode());stream.flush();os.fsync(stream.fileno())
        fd=os.open(CONTROL,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    def consume(self):
        self.save('INVOCATION.safe.json',{'version':1,'purpose':'one_canonical_auth_readiness_get','consumed_unix':self.clock(),'no_auto_retry':True})
    def source_pins(self):
        base=Path(__file__).resolve().parents[2]
        need(sha((base/'utils/ella/canonical_auth.py').read_bytes())==HELPER_SHA256 and sha((base/'ella/routers/canonical_events.py').read_bytes())==ROUTER_SHA256,'source_drift')
        return sha(Path(__file__).read_bytes())
    def headers(self, uid):
        from utils.ella.canonical_auth import canonical_event_service_headers
        return canonical_event_service_headers(uid)
    async def get(self, params, headers):
        import httpx
        async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
            async with client.stream('GET',URL,params=params,headers=headers) as response:
                raw=b''
                async for part in response.aiter_bytes():
                    raw+=part;need(len(raw)<=65536,'response_shape')
                return response.status_code,raw

async def run_probe(io=None):
    io=io or LocalIO();result={'version':1,'status':'HOLD','http_status':None,'owner_match':None,'rows_count':None,'expires_unix':None,'invocation_consumed':False,'no_auto_retry':True,'observed_unix':None}
    end=io.mono()+180;scope=None;raw_scope=None;raw_go=None;headers=None
    async def bounded(call,cap):
        need(end-io.mono()>0,'deadline')
        value=await asyncio.wait_for(call(),min(cap,end-io.mono()))
        need(end-io.mono()>0 and (scope is None or io.clock()<scope['expires_unix']),'deadline')
        return value
    async def file(call,cap=2):return await bounded(lambda:asyncio.to_thread(call),cap)
    try:
        # Missing scope is disabled; no invocation marker, credential or HTTP read.
        try:raw_scope=await file(lambda:io.read('SCOPE.json'))
        except FileNotFoundError:
            result['status']='DISABLED';return result
        await file(io.consume);result['invocation_consumed']=True
        scope=strict(raw_scope);pin=await file(io.source_pins)
        uid=validate_scope(scope,io.clock(),pin,io.subject_sha256)
        result['expires_unix']=scope['expires_unix'];end=min(end,io.mono()+scope['expires_unix']-io.clock())
        while True:
            try:raw_go=await file(lambda:io.read('GO.json'));break
            except FileNotFoundError:
                need(io.clock()<scope['expires_unix']-10,'go_expired')
                await bounded(lambda:asyncio.sleep(.1),1)
        validate_go(strict(raw_go),scope,raw_scope,io.clock(),io.pid())
        end=min(end,io.mono()+10)
        need(await file(lambda:io.read('SCOPE.json'))==raw_scope and await file(lambda:io.read('GO.json'))==raw_go,'release_changed')
        need(await file(io.source_pins)==pin,'source_drift')
        validate_go(strict(raw_go),scope,raw_scope,io.clock(),io.pid())
        # Only the existing in-process helper resolves existing configured auth.
        try:headers=io.headers(uid)
        except RuntimeError:
            result['status']='AUTH_CONTEXT_UNAVAILABLE_HOLD';return result
        need(io.clock()<scope['expires_unix']-5 and end-io.mono()>5,'deadline')
        import datetime
        since=io.clock()+3600;before=since+60
        iso=lambda x:datetime.datetime.fromtimestamp(x,datetime.timezone.utc).isoformat()
        params={'uid':uid,'channels':'muse_fictional_test','limit':2,'since':iso(since),'before':iso(before)}
        code,raw=await bounded(lambda:io.get(params,headers),5)
        result['http_status']=code;need(type(code) is int and code==200,'http_status')
        need(type(raw) is bytes and len(raw)<=65536,'response_shape');body=strict(raw)
        need(type(body) is dict and body.get('ok') is True and type(body.get('events')) is list and len(body['events'])<=2,'response_shape')
        result['owner_match']=body.get('uid')==uid
        for ev in body['events']:
            if not (type(ev) is dict and ev.get('uid')==uid and ev.get('canonical_identity')==uid and ev.get('channel')=='muse_fictional_test'):
                result['owner_match']=False;raise Hold('owner_mismatch')
            stamp=datetime.datetime.fromisoformat(ev['started_at'].replace('Z','+00:00'))
            need(stamp.tzinfo is not None and since<=stamp.timestamp()<before,'response_window')
        need(result['owner_match'] is True,'owner_mismatch');result['rows_count']=len(body['events'])
        need(await file(lambda:io.read('SCOPE.json'))==raw_scope and await file(lambda:io.read('GO.json'))==raw_go,'release_changed')
        need(await file(io.source_pins)==pin,'source_drift');validate_go(strict(raw_go),scope,raw_scope,io.clock(),io.pid())
        result['status']='AUTH_READINESS_HTTP_OWNER_COUNT_QUALIFIED'
    except asyncio.CancelledError:
        result['status']='CANCELLED_HOLD'
        raise
    except BaseException:
        result['status']='HOLD'
    finally:
        if headers is not None:headers.clear()
        # Observation timestamp is the last business fence, before at most2s
        # receipt cleanup. It is historical HTTP proof, never a new permit.
        result['observed_unix']=io.clock()
        if result['status']!='DISABLED':
            try:await asyncio.wait_for(asyncio.to_thread(io.save,'TERMINAL.safe.json',result),2)
            except BaseException:result['status']='TERMINAL_UNKNOWN_HOLD'
    return result

async def start_probe():
    global _task
    if _task is None:
        _task=asyncio.create_task(run_probe(),name='ella-canonical-auth-readiness-one-shot')

async def stop_probe():
    global _task
    if _task is not None:
        if not _task.done():_task.cancel()
        try:await asyncio.wait_for(_task,1)
        except BaseException:pass
        # Never reset task or durable invocation for restart/rearm.
