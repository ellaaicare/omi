"""Internal admitted scanner work. No API can issue work or choose delivery scope.

Explicit migration/install required; ordinary routes are unchanged. Unknown START
is retained without retry. A successful receipt means queued, never phone playback.
"""
import asyncio, copy, hashlib, json, time, uuid
from contextlib import asynccontextmanager
from ella.services.whisper_delivery import identifier, scope, apply, _decode, lock_active_mode, DeliveryConflict, DeliveryNotFound

WORK_SCOPE={'device_id','session_id','client_epoch','revision','origin_id','presentation'}
BINDING={'uid','agent_id','profile_name','account_user_id','runtime_identity_sha256'}

def held(code):raise DeliveryConflict(code)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def closed_body(value,fields):
    if not isinstance(value,dict) or set(value)!=fields:held('tool_body_invalid')
    identifier(value['tool_work_id']);identifier(value['request_id'])
    return value

def validate_binding(value,uid):
    if not isinstance(value,dict) or set(value)!=BINDING or value['uid']!=uid:held('tool_runtime_invalid')
    identifier(value['account_user_id'])
    for key in ('uid','agent_id','profile_name'):
        if not isinstance(value[key],str) or not value[key] or len(value[key])>200 or any(ord(c)<33 or ord(c)>126 for c in value[key]):held('tool_runtime_invalid')
    s=value['runtime_identity_sha256']
    if not isinstance(s,str) or len(s)!=64 or any(c not in '0123456789abcdef' for c in s):held('tool_runtime_invalid')
    return copy.deepcopy(value)

def authority_result(work,binding):
    return {'contract':1,'tool_work_id':work['tool_work_id'],**{k:binding[k] for k in ('uid','agent_id','profile_name','runtime_identity_sha256')},**work['scope']}

def validate_stored_scope(state,work,pointer):
    d=work['scope'];uid=work['uid']
    if not isinstance(d,dict) or set(d)!=WORK_SCOPE:held('tool_scope_invalid')
    scope(state,uid,d['device_id'],d['session_id'],d['revision'])
    if (state['client_epoch']!=d['client_epoch'] or state['presentation']!=d['presentation']
            or not pointer or pointer['session_id']!=d['session_id'] or pointer['client_epoch']!=d['client_epoch']
            or pointer['capture_key']!=work['capture_key']):held('tool_capture_changed')
    apply(state,'validate_origin',uid,d['device_id'],d['session_id'],d['revision'],{'client_epoch':d['client_epoch'],'origin_id':d['origin_id']})

async def lock_tool_mode(conn,uid):
    # Retained generic work has no backend-proven emergency trigger/severity.
    # Never treat a model/caller message or severity claim as emergency authority.
    await lock_active_mode(conn,uid)
    row=await conn.fetchrow('SELECT guardian_mode FROM users WHERE omi_uid=$1 FOR UPDATE',uid)
    if not row or row['guardian_mode'].strip().lower()=='emergency_only':
        held('tool_unclassified_emergency_only')

class ToolWorkStore:
    def __init__(self,pool):self.pool=pool
    @asynccontextmanager
    async def locked(self,uid,work_id):
        identifier(work_id)
        async with self.pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '5000ms'")
                await conn.execute("SET LOCAL lock_timeout = '1000ms'")
                await lock_tool_mode(conn,uid)
                locator=await conn.fetchrow('SELECT device_id,session_id FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2',uid,work_id)
                if not locator:raise DeliveryNotFound('tool_work_unknown')
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,locator['device_id']):held('tool_device_unknown')
                row=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,locator['device_id'],locator['session_id'])
                work=await conn.fetchrow('SELECT * FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2 FOR UPDATE',uid,work_id)
                if not row or not work:held('tool_work_unknown')
                work=dict(work);work['scope']=_decode(work['scope']);work['capture_proof']=_decode(work['capture_proof']);work['binding']=_decode(work['binding']);work['receipt']=_decode(work['receipt']) if work['receipt'] is not None else None
                if work['device_id']!=locator['device_id'] or work['session_id']!=locator['session_id']:held('tool_locator_changed')
                pointer=await conn.fetchrow('SELECT client_epoch,capture_key,session_id FROM guardian_delivery_capture WHERE uid=$1 AND device_id=$2',uid,work['device_id'])
                state=_decode(row['state']);validate_stored_scope(state,work,pointer)
                if not await conn.fetchval('SELECT expires_at > clock_timestamp() FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2',uid,work_id):held('tool_work_expired')
                yield conn,work,state
    async def issue(self,*,uid,origin,capture_key,request_id,binding,capture_proof):
        """Internal producer only, after fresh capture-owner validation. Never from HTTP."""
        identifier(request_id);validate_binding(binding,uid)
        if not isinstance(capture_proof,dict) or set(capture_proof)!={'conversation_id','generation','owner_token_sha256'}:held('tool_capture_proof_invalid')
        if not isinstance(origin,dict) or origin.get('uid')!=uid:held('tool_origin_invalid')
        d={k:origin[k] for k in WORK_SCOPE};identifier(d['origin_id'])
        async with self.pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '5000ms'")
                await conn.execute("SET LOCAL lock_timeout = '1000ms'")
                await lock_tool_mode(conn,uid)
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,d['device_id']):held('tool_device_unknown')
                row=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,d['device_id'],d['session_id'])
                pointer=await conn.fetchrow('SELECT client_epoch,capture_key,session_id FROM guardian_delivery_capture WHERE uid=$1 AND device_id=$2',uid,d['device_id'])
                if not row:held('tool_scope_unknown')
                work={'uid':uid,'scope':d,'capture_key':capture_key}
                validate_stored_scope(_decode(row['state']),work,pointer)
                old=await conn.fetchrow('SELECT tool_work_id,scope,binding,capture_key,capture_proof FROM guardian_delivery_tool_work WHERE uid=$1 AND producer_request_id=$2',uid,request_id)
                if old:
                    if _decode(old['scope'])!=d or _decode(old['binding'])!=binding or old['capture_key']!=capture_key or _decode(old['capture_proof'])!=capture_proof:held('tool_issue_request_reused')
                    return str(old['tool_work_id']) # no expiry extension or re-arm
                work_id=str(uuid.uuid4())
                await conn.execute("INSERT INTO guardian_delivery_tool_work(uid,tool_work_id,producer_request_id,device_id,session_id,scope,binding,capture_key,capture_proof,status,expires_at) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9::jsonb,'issued',clock_timestamp()+interval '90 seconds')",uid,work_id,request_id,d['device_id'],d['session_id'],json.dumps(d),json.dumps(binding),capture_key,json.dumps(capture_proof))
                return work_id
    async def close(self,uid,work_id,request_id):
        """Owned cleanup only. Off/expiry/revocation does not forbid closing work.

        Serializes with final enqueue; does not cancel or drain a remote provider.
        """
        identifier(work_id);identifier(request_id)
        async with self.pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '5000ms'")
                await conn.execute("SET LOCAL lock_timeout = '1000ms'")
                if not await conn.fetchrow('SELECT id FROM users WHERE omi_uid=$1 FOR UPDATE',uid):raise DeliveryNotFound('tool_owner_unknown')
                locator=await conn.fetchrow('SELECT device_id,session_id FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2',uid,work_id)
                if not locator:raise DeliveryNotFound('tool_work_unknown')
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,locator['device_id']):held('tool_device_unknown')
                if not await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,locator['device_id'],locator['session_id']):held('tool_scope_unknown')
                work=await conn.fetchrow('SELECT * FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2 FOR UPDATE',uid,work_id)
                if not work or work['device_id']!=locator['device_id'] or work['session_id']!=locator['session_id']:held('tool_locator_changed')
                if work['status']=='closed':
                    if work['close_request_id']!=request_id:held('tool_close_request_reused')
                    return _decode(work['close_result'])
                if work['status']=='completed':
                    receipt=_decode(work['receipt'])
                    if not isinstance(receipt,dict) or set(receipt)!={'accepted','event_id'} or receipt['accepted'] is not True:held('tool_receipt_invalid')
                    identifier(receipt['event_id'])
                    result={'closed':True,'accepted':True,'event_id':receipt['event_id'],'outcome':'completed'}
                elif work['status'] in {'issued','started'}:
                    result={'closed':True,'accepted':False,'event_id':None,'outcome':'unused' if work['status']=='issued' else 'unknown'}
                else:held('tool_status_invalid')
                await conn.execute("UPDATE guardian_delivery_tool_work SET status='closed',close_request_id=$3,close_result=$4::jsonb WHERE uid=$1 AND tool_work_id=$2",uid,work_id,request_id,json.dumps(result))
                return result
    async def authority(self,uid,work_id,binding,capture_guard):
        async with self.locked(uid,work_id) as (_,work,_state):
            if work['binding']!=binding:held('tool_runtime_changed')
            await capture_guard(uid,work['capture_proof'])
            if work['status'] not in {'issued','completed'}:held('tool_delivery_unknown')
            return authority_result(work,binding)
    async def authority_started(self,uid,work_id,binding,capture_guard):
        async with self.locked(uid,work_id) as (_,work,_state):
            await capture_guard(uid,work['capture_proof'])
            if work['binding']!=binding or work['status']!='started':held('tool_runtime_changed')
            return authority_result(work,binding)
    async def start(self,uid,work_id,request_id,message_sha,binding,capture_guard):
        async with self.locked(uid,work_id) as (conn,work,_):
            if work['binding']!=binding:held('tool_runtime_changed')
            await capture_guard(uid,work['capture_proof'])
            if work['status']=='completed':
                if work['delivery_request_id']!=request_id or work['message_sha256']!=message_sha:held('tool_request_reused')
                return work,work['receipt']
            if work['status']!='issued':held('tool_delivery_unknown')
            await conn.execute("UPDATE guardian_delivery_tool_work SET status='started',delivery_request_id=$3,message_sha256=$4 WHERE uid=$1 AND tool_work_id=$2",uid,work_id,request_id,message_sha)
            return work,None
    async def finish(self,uid,work_id,request_id,message_sha,binding,payload,authority_guard,capture_guard):
        async with self.locked(uid,work_id) as (conn,work,state):
            if (work['binding']!=binding or work['status']!='started' or work['delivery_request_id']!=request_id or work['message_sha256']!=message_sha):held('tool_completion_changed')
            if await authority_guard()!=binding:held('tool_runtime_changed')
            await capture_guard(uid,work['capture_proof'])
            if not await conn.fetchval('SELECT expires_at > clock_timestamp() FROM guardian_delivery_tool_work WHERE uid=$1 AND tool_work_id=$2',uid,work_id):held('tool_work_expired')
            d=work['scope'];result=apply(state,'enqueue',uid,d['device_id'],d['session_id'],d['revision'],{'client_epoch':d['client_epoch'],'origin_id':d['origin_id'],'output_id':'retained-tool:'+work_id,'payload':payload})
            receipt={'accepted':True,'event_id':result['event_id']}
            await conn.execute('UPDATE guardian_delivery_sessions SET state=$4::jsonb WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,d['device_id'],d['session_id'],json.dumps(state,allow_nan=False))
            await conn.execute("UPDATE guardian_delivery_tool_work SET status='completed',receipt=$3::jsonb WHERE uid=$1 AND tool_work_id=$2",uid,work_id,json.dumps(receipt))
            return receipt

class ToolDeliveryService:
    def __init__(self,store,*,runtime_binding,assert_consent,assert_capture,synthesize):
        self.store=store;self.runtime_binding=runtime_binding;self.assert_consent=assert_consent;self.synthesize=synthesize;self.assert_capture=assert_capture
    async def fresh(self,uid):
        await self.assert_consent(uid)
        return validate_binding(await self.runtime_binding(uid),uid)
    async def authority(self,uid,body):
        closed_body(body,{'tool_work_id','request_id'})
        async with asyncio.timeout(15):
            binding=await self.fresh(uid);result=await self.store.authority(uid,body['tool_work_id'],binding,self.assert_capture)
            if await self.fresh(uid)!=binding:held('tool_runtime_changed')
            return result
    async def close(self,uid,body):
        closed_body(body,{'tool_work_id','request_id'})
        async with asyncio.timeout(10):
            return await self.store.close(uid,body['tool_work_id'],body['request_id'])
    async def deliver(self,uid,body):
        closed_body(body,{'tool_work_id','request_id','message'})
        message=body['message']
        if not isinstance(message,str) or not message.strip() or len(message)>4000 or len(message.encode())>16000 or '\x00' in message:held('tool_message_invalid')
        sha=digest(message)
        # Commit START before any synthesis; cancellation/unknown cannot re-arm.
        async with asyncio.timeout(60):
            binding=await self.fresh(uid)
            work,replay=await self.store.start(uid,body['tool_work_id'],body['request_id'],sha,binding,self.assert_capture)
            if await self.fresh(uid)!=binding:held('tool_runtime_changed')
            if replay is not None:return replay
            # Revalidate original stored scope immediately before provider egress.
            await self.store.authority_started(uid,body['tool_work_id'],binding,self.assert_capture)
            url=None
            if work['scope']['presentation']=='audio':
                url=await self.synthesize(uid,message,work)
                if not isinstance(url,str) or not url:held('tool_synthesis_invalid')
            elif work['scope']['presentation']!='text_only':held('tool_presentation_invalid')
            if await self.fresh(uid)!=binding:held('tool_runtime_changed')
            payload={'url':url,'priority':'normal','message':message,'trigger_type':'retained_tool','metadata':{'tool_work_id':body['tool_work_id']}}
            return await self.store.finish(uid,body['tool_work_id'],body['request_id'],sha,binding,payload,lambda:self.fresh(uid),self.assert_capture)
