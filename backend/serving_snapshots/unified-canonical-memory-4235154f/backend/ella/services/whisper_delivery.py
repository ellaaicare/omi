"""Opt-in durable owner/device/session delivery. No capture or provider controls."""
import copy
import json
import uuid
import hashlib
from dataclasses import dataclass
from ella.services.thin_delivery_binding import canonical_scope,require_scope,revalidate,DeliveryTargetHold

class DeliveryConflict(ValueError):pass

def target_scope(state,value):
    try:return require_scope(state,value)
    except DeliveryTargetHold as e:raise DeliveryConflict(str(e)) from None

async def target_current(value,checker,operation,body=None):
    try:return await revalidate(value,checker,operation,body)
    except DeliveryTargetHold as e:raise DeliveryConflict(str(e)) from None
class DeliveryNotFound(ValueError):pass

def identifier(value):
    if not isinstance(value,str):raise DeliveryConflict('delivery_identifier_invalid')
    try: parsed=uuid.UUID(value)
    except ValueError as exc:raise DeliveryConflict('delivery_identifier_invalid') from exc
    if str(parsed)!=value:raise DeliveryConflict('delivery_identifier_invalid')
    return value

def initial(uid,device_id,session_id,client_epoch,presentation="audio",*,consent_scope=None):
    if presentation not in {"audio","text_only"}:raise DeliveryConflict("delivery_presentation_invalid")
    state={'contract':1,'uid':uid,'device_id':identifier(device_id),'session_id':identifier(session_id),
            'client_epoch':identifier(client_epoch),'presentation':presentation,'revision':1,'closed':False,'position':0,'cursor':0,'events':{},'origins':{},'stop_request_id':None,'stop_result':None}
    if consent_scope is not None:
        try:state['consent_scope']=canonical_scope(consent_scope)
        except DeliveryTargetHold as e:raise DeliveryConflict(str(e)) from None
    return state

def scope(state,uid,device_id,session_id,revision,*,closed_ok=False):
    if (state['uid']!=uid or state['device_id']!=identifier(device_id)
            or state['session_id']!=identifier(session_id)):raise DeliveryNotFound('delivery_scope_unknown')
    if type(revision) is not int or revision<1:raise DeliveryConflict('delivery_revision_invalid')
    if state['closed']:
        if not closed_ok:raise DeliveryConflict('delivery_closed')
        if revision not in (state['revision'],state['revision']-1):raise DeliveryConflict('delivery_revision_stale')
    elif state['revision']!=revision:raise DeliveryConflict('delivery_revision_stale')

def envelope(state,event=None):
    return {'contract':1,'uid':state['uid'],'device_id':state['device_id'],'session_id':state['session_id'],
            'client_epoch':state['client_epoch'],'presentation':state['presentation'],
            'revision':state['revision'],'closed':state['closed'],'stop_request_id':state['stop_request_id'],
            'cursor':{'revision':state['revision'],'position':state['cursor']},
            'event':({**copy.deepcopy(event['payload']),'event_id':event['event_id'],'position':event['position']} if event else None)}

def apply(state,operation,uid,device_id,session_id,revision,body=None,*,consent_scope=None):
    """Pure transition run under one durable session row lock. GET never consumes."""
    target_scope(state,consent_scope)
    body=body or {}
    if identifier(body.get('client_epoch'))!=state['client_epoch']:raise DeliveryNotFound('delivery_client_epoch_unknown')
    scope(state,uid,device_id,session_id,revision,closed_ok=operation in {'poll','stop','report'})
    if operation=='poll':
        if state['closed']:return envelope(state)
        event=next((e for e in state['events'].values() if e['position']==state['cursor']+1),None)
        # A claimed but unresolved output blocks the next offer; never silently replay.
        return envelope(state,event if event and event['status']=='pending' else None)
    if operation=='stop':
        request=identifier(body['request_id'])
        if state['closed']:
            if state['stop_request_id']!=request:raise DeliveryConflict('delivery_stop_request_stale')
            return copy.deepcopy(state['stop_result'])
        if not state['closed']:
            state['closed']=True;state['revision']+=1;state['cursor']=state['position'];state['stop_request_id']=request
            for event in state['events'].values():
                if event['status'] in {'pending','claimed'}:event['status']='discarded'
            for origin in state['origins'].values():origin['cancelled']=True
        result=envelope(state)
        if body.get('resume',True) is not False:
            result['resume']=envelope(initial(uid,device_id,str(uuid.uuid4()),state['client_epoch'],state['presentation'],consent_scope=consent_scope))
        else:result['resume']=None
        state['stop_result']=copy.deepcopy(result)
        return result
    if operation=='validate_origin':
        wanted=identifier(body['origin_id'])
        origin=next((o for o in state['origins'].values() if o['origin_id']==wanted),None)
        if not origin or origin['cancelled'] or origin['revision']!=revision:raise DeliveryConflict('delivery_origin_invalid')
        return {'valid':True,'presentation':state['presentation']}
    if operation=='origin':
        request=identifier(body['request_id'])
        prior=state['origins'].get(request)
        if prior is None:
            if len(state['origins'])>=512:raise DeliveryConflict('delivery_origin_capacity')
            prior={'origin_id':str(uuid.uuid4()),'revision':revision,'cancelled':False}
            state['origins'][request]=prior
        return {'contract':1,'uid':uid,'device_id':device_id,'session_id':session_id,
                'revision':revision,'client_epoch':state['client_epoch'],'presentation':state['presentation'],'origin_id':prior['origin_id']}
    if operation=='enqueue':
        origin_id=identifier(body['origin_id'])
        origin=next((o for o in state['origins'].values() if o['origin_id']==origin_id),None)
        if not origin or origin['cancelled'] or origin['revision']!=revision:raise DeliveryConflict('delivery_origin_invalid')
        output_id=body['output_id'];payload=body['payload']
        if not isinstance(output_id,str) or not 1<=len(output_id)<=200:raise DeliveryConflict('delivery_output_invalid')
        payload=copy.deepcopy(payload)
        if state['presentation']=='text_only':
            if not isinstance(payload,dict) or not isinstance(payload.get('message'),str) or not payload['message'].strip():
                raise DeliveryConflict('delivery_text_required')
            payload['url']=None
        if (not isinstance(payload,dict) or set(payload)!={'url','priority','message','trigger_type','metadata'}
                or (state['presentation']=='audio' and (not isinstance(payload['url'],str) or not payload['url']))):raise DeliveryConflict('delivery_payload_invalid')
        try: size=len(json.dumps(payload,allow_nan=False).encode())
        except (ValueError,TypeError):raise DeliveryConflict('delivery_payload_invalid') from None
        if size>16384:raise DeliveryConflict('delivery_payload_invalid')
        old=next((e for e in state['events'].values() if e['output_id']==output_id),None)
        if old:
            if old['payload']!=payload or old['origin_id']!=origin_id:raise DeliveryConflict('delivery_output_reused')
            return {'event_id':old['event_id'],'position':old['position']}
        if len(state['events'])>=512:raise DeliveryConflict('delivery_event_capacity')
        state['position']+=1;event_id=str(uuid.uuid4())
        state['events'][event_id]={'event_id':event_id,'position':state['position'],'output_id':output_id,
            'origin_id':origin_id,'status':'pending','payload':copy.deepcopy(payload),'claim_request_id':None,'ack_request_id':None}
        if len(json.dumps(state,allow_nan=False).encode())>1048576:raise DeliveryConflict('delivery_session_capacity')
        return {'event_id':event_id,'position':state['position']}
    if operation=='report':
        event=state['events'].get(identifier(body['event_id']))
        status=body.get('status');request=identifier(body['request_id'])
        if status not in {'started','completed','canceled','discarded'}:raise DeliveryConflict('delivery_report_invalid')
        if not event or event['claim_request_id']!=identifier(body['claim_request_id']):raise DeliveryConflict('delivery_report_claim_invalid')
        if state['closed'] and status in {'started','completed'}:raise DeliveryConflict('delivery_report_stale')
        reports=event.setdefault('reports',{})
        if request in reports:
            if reports[request]!=status:raise DeliveryConflict('delivery_report_request_reused')
        elif len(reports)>=4:raise DeliveryConflict('delivery_report_capacity')
        else:reports[request]=status
        return {**envelope(state),'event_id':event['event_id'],'status':status,'request_id':request}
    if operation in {'claim','ack'}:
        event_id=identifier(body['event_id']);event=state['events'].get(event_id)
        if not event or type(body['position']) is not int or event['position']!=body['position']:
            raise DeliveryConflict('delivery_event_unknown')
        claim=identifier(body['claim_request_id'])
        if operation=='claim':
            if event['status']=='pending' and event['position']==state['cursor']+1:
                event['status']='claimed';event['claim_request_id']=claim
            elif event['status']!='claimed' or event['claim_request_id']!=claim:
                raise DeliveryConflict('delivery_claim_conflict')
            # Idempotent claim response is a receipt, never an automatic playback instruction.
            return {**envelope(state),'event_id':event_id,'position':event['position'],'claim_request_id':claim,'status':'claimed'}
        ack=identifier(body['ack_request_id'])
        if event['claim_request_id']!=claim:raise DeliveryConflict('delivery_ack_conflict')
        if event['status']=='acknowledged':
            if event['ack_request_id']!=ack:raise DeliveryConflict('delivery_ack_conflict')
        elif event['status']=='claimed' and event['position']==state['cursor']+1:
            event['status']='acknowledged';event['ack_request_id']=ack;state['cursor']=event['position']
        else:raise DeliveryConflict('delivery_ack_conflict')
        return {**envelope(state),'event_id':event_id,'ack_request_id':ack,'status':'acknowledged'}
    raise DeliveryConflict('delivery_operation_invalid')

class PostgresStore:
    """Existing pool, explicit migration required. No runtime DDL or private logging."""
    def __init__(self,pool):self.pool=pool
    async def device(self,uid,request_id,*,consent_scope=None,authority_check=None):
        request_id=identifier(request_id)
        async with self.pool.acquire() as conn:
            await target_current(consent_scope,authority_check,'device')
            row=await conn.fetchrow('''INSERT INTO guardian_delivery_devices(uid,device_id,request_id)
                VALUES($1,$2,$3) ON CONFLICT(uid,request_id) DO UPDATE SET request_id=EXCLUDED.request_id
                RETURNING device_id''',uid,str(uuid.uuid4()),request_id)
            return {'contract':1,'uid':uid,'device_id':row['device_id']}
    async def session(self,uid,device_id,request_id,client_epoch,presentation="audio",*,consent_scope=None,authority_check=None):
        client_epoch=identifier(client_epoch)
        device_id=identifier(device_id);request_id=identifier(request_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await lock_active_mode(conn,uid,consent_scope=consent_scope)
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,device_id):
                    raise DeliveryNotFound('delivery_device_unknown')
                prior=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND request_id=$3',uid,device_id,request_id)
                if prior:
                    old=_decode(prior['state']);target_scope(old,consent_scope)
                    await target_current(consent_scope,authority_check,'session')
                    if old['client_epoch']!=client_epoch or old['presentation']!=presentation:raise DeliveryConflict('delivery_session_request_reused')
                    return envelope(old)
                active=await conn.fetchrow("SELECT session_id,state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND state->>'closed'='false'",uid,device_id)
                if active:
                    old=_decode(active['state']);target_scope(old,consent_scope)
                    await target_current(consent_scope,authority_check,'session')
                    if old['client_epoch']==client_epoch and old['presentation']==presentation:raise DeliveryConflict('delivery_session_already_open')
                    apply(old,'stop',uid,device_id,old['session_id'],old['revision'],{'client_epoch':old['client_epoch'],'request_id':str(uuid.uuid4()),'resume':False},consent_scope=old.get('consent_scope'))
                    await conn.execute('UPDATE guardian_delivery_sessions SET state=$4::jsonb WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,device_id,old['session_id'],json.dumps(old))
                await target_current(consent_scope,authority_check,'session')
                sid=str(uuid.uuid4());state=initial(uid,device_id,sid,client_epoch,presentation,consent_scope=consent_scope)
                await conn.execute('INSERT INTO guardian_delivery_sessions(uid,device_id,session_id,request_id,state) VALUES($1,$2,$3,$4,$5::jsonb)',uid,device_id,sid,request_id,json.dumps(state))
                return envelope(state)
    async def bind_capture(self,uid,device_id,session_id,revision,client_epoch,capture_key,*,consent_scope=None,authority_check=None):
        """Internal authenticated handshake only; caller must freshly validate capture owner."""
        _capture_key(capture_key)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await lock_active_mode(conn,uid,consent_scope=consent_scope)
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,device_id):raise DeliveryNotFound('delivery_device_unknown')
                row=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,device_id,session_id)
                if not row:raise DeliveryNotFound('delivery_scope_unknown')
                state=_decode(row['state']);target_scope(state,consent_scope)
                await target_current(consent_scope,authority_check,'bind_capture')
                scope(state,uid,device_id,session_id,revision)
                if state['client_epoch']!=identifier(client_epoch):raise DeliveryNotFound('delivery_client_epoch_unknown')
                await conn.execute('INSERT INTO guardian_delivery_capture(uid,device_id,client_epoch,capture_key,session_id) VALUES($1,$2,$3,$4,$5) ON CONFLICT(uid,device_id) DO UPDATE SET client_epoch=EXCLUDED.client_epoch,capture_key=EXCLUDED.capture_key,session_id=EXCLUDED.session_id',uid,device_id,client_epoch,capture_key,session_id)
                return envelope(state)
    async def handoff_capture(self,uid,device_id,client_epoch,previous_key,new_key,*,consent_scope=None,authority_check=None):
        _capture_key(previous_key);_capture_key(new_key)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await lock_active_mode(conn,uid,consent_scope=consent_scope)
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,device_id):raise DeliveryNotFound('delivery_device_unknown')
                pointer=await conn.fetchrow('SELECT client_epoch,capture_key,session_id FROM guardian_delivery_capture WHERE uid=$1 AND device_id=$2',uid,device_id)
                if not pointer or pointer['client_epoch']!=client_epoch or pointer['capture_key']!=previous_key or not pointer['session_id']:raise DeliveryConflict('delivery_capture_binding_stale')
                bound=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,device_id,pointer['session_id'])
                if not bound:raise DeliveryNotFound('delivery_scope_unknown')
                target_scope(_decode(bound['state']),consent_scope)
                await target_current(consent_scope,authority_check,'handoff_capture')
                await conn.execute('UPDATE guardian_delivery_capture SET capture_key=$4 WHERE uid=$1 AND device_id=$2 AND capture_key=$3',uid,device_id,previous_key,new_key)
                return pointer['session_id']
    async def origin_for_capture(self,uid,device_id,client_epoch,capture_key,request_id,*,consent_scope=None,authority_check=None):
        """Resolve current pointer once BEFORE asynchronous work; never reroute an origin."""
        _capture_key(capture_key);identifier(request_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await lock_active_mode(conn,uid,consent_scope=consent_scope)
                if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,device_id):raise DeliveryNotFound('delivery_device_unknown')
                pointer=await conn.fetchrow('SELECT client_epoch,capture_key,session_id FROM guardian_delivery_capture WHERE uid=$1 AND device_id=$2',uid,device_id)
                if not pointer or pointer['client_epoch']!=client_epoch or pointer['capture_key']!=capture_key or not pointer['session_id']:raise DeliveryConflict('delivery_capture_binding_stale')
                prior=await conn.fetchrow('SELECT session_id FROM guardian_delivery_origin_requests WHERE uid=$1 AND device_id=$2 AND client_epoch=$3 AND capture_key=$4 AND request_id=$5',uid,device_id,client_epoch,capture_key,request_id)
                if prior and prior['session_id']!=pointer['session_id']:raise DeliveryConflict('delivery_origin_request_cancelled')
                sid=pointer['session_id'];row=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,device_id,sid)
                if not row:raise DeliveryNotFound('delivery_scope_unknown')
                state=_decode(row['state']);target_scope(state,consent_scope)
                await target_current(consent_scope,authority_check,'origin')
                result=apply(state,'origin',uid,device_id,sid,state['revision'],{'client_epoch':client_epoch,'request_id':request_id},consent_scope=consent_scope)
                await conn.execute('UPDATE guardian_delivery_sessions SET state=$4::jsonb WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,device_id,sid,json.dumps(state))
                if not prior:await conn.execute('INSERT INTO guardian_delivery_origin_requests(uid,device_id,client_epoch,capture_key,request_id,session_id) VALUES($1,$2,$3,$4,$5,$6)',uid,device_id,client_epoch,capture_key,request_id,sid)
                return result
    async def operation(self,operation,uid,device_id,session_id,revision,body=None,*,consent_scope=None,authority_check=None):
        identifier(device_id);identifier(session_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if operation in {'origin','validate_origin','enqueue','claim'} or (operation=='report' and (body or {}).get('status') in {'started','completed'}):await lock_active_mode(conn,uid,consent_scope=consent_scope)
                if operation=='stop':
                    if not await conn.fetchrow('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 AND device_id=$2 FOR UPDATE',uid,device_id):raise DeliveryNotFound('delivery_device_unknown')
                row=await conn.fetchrow('SELECT state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND session_id=$3 FOR UPDATE',uid,device_id,session_id)
                if not row:raise DeliveryNotFound('delivery_scope_unknown')
                state=_decode(row['state']);target_scope(state,consent_scope)
                await target_current(consent_scope,authority_check,operation,body)
                was_closed=state['closed'];result=apply(state,operation,uid,device_id,session_id,revision,body,consent_scope=consent_scope)
                if operation!='poll':
                    await conn.execute('UPDATE guardian_delivery_sessions SET state=$4::jsonb WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,device_id,session_id,json.dumps(state,allow_nan=False))
                if operation=='stop' and not was_closed and result.get('resume'):
                    r=result['resume'];new=initial(uid,device_id,r['session_id'],state['client_epoch'],state['presentation'],consent_scope=consent_scope)
                    request=str(uuid.uuid5(uuid.NAMESPACE_URL,'delivery-resume:'+session_id+state['stop_request_id']))
                    await conn.execute('INSERT INTO guardian_delivery_sessions(uid,device_id,session_id,request_id,state) VALUES($1,$2,$3,$4,$5::jsonb)',uid,device_id,new['session_id'],request,json.dumps(new))
                if operation=='stop' and not was_closed:
                    # Same device lock as origin admission: old admitted work keeps its old origin.
                    await conn.execute('UPDATE guardian_delivery_capture SET session_id=$4 WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,device_id,session_id,(result['resume']['session_id'] if result.get('resume') else None))
                return result

def _capture_key(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):raise DeliveryConflict('delivery_capture_binding_invalid')

def capture_identity(uid,conversation_id,generation,owner_token):
    if not isinstance(uid,str) or not uid or not isinstance(conversation_id,str) or not conversation_id or not ((type(generation) is int and generation>=1) or (isinstance(generation,str) and 0<len(generation)<=128)) or not isinstance(owner_token,str) or not owner_token:raise DeliveryConflict('delivery_capture_binding_invalid')
    return hashlib.sha256(json.dumps([uid,conversation_id,generation,owner_token],separators=(',',':')).encode()).hexdigest()

def _decode(value):return json.loads(value) if isinstance(value,str) else copy.deepcopy(value)

ACTIVE_DELIVERY_MODES=frozenset({'cyborg','chatbot','demo','active_support','emergency_only','memory_support','maximum_awareness'})
async def lock_active_mode(conn,uid,*,consent_scope=None):
    row=await conn.fetchrow('SELECT guardian_mode FROM users WHERE omi_uid=$1 FOR UPDATE',uid)
    if consent_scope is not None:
        canonical_scope(consent_scope)
        if row is None:raise DeliveryNotFound('delivery_account_unknown')
        return
    value=row['guardian_mode'] if row else None
    if not isinstance(value,str) or value.strip().lower() not in ACTIVE_DELIVERY_MODES:raise DeliveryConflict('delivery_mode_inactive')

async def close_owner_delivery(conn,uid,*,consent_target=None):
    """Called ONLY inside the existing authorized settings transaction after Off UPDATE.
    Deterministic lock order: users row -> sorted devices -> session. Keep all history.
    """
    devices=await conn.fetch('SELECT device_id FROM guardian_delivery_devices WHERE uid=$1 ORDER BY device_id FOR UPDATE',uid)
    for device in devices:
        d=device['device_id']
        rows=await conn.fetch("SELECT session_id,state FROM guardian_delivery_sessions WHERE uid=$1 AND device_id=$2 AND state->>'closed'='false' ORDER BY session_id FOR UPDATE",uid,d)
        closed_any=False
        for row in rows:
            state=_decode(row['state'])
            stored=(state.get('consent_scope') or {}).get('consent_target')
            if stored!=consent_target:continue
            closed_any=True
            apply(state,'stop',uid,d,row['session_id'],state['revision'],{'client_epoch':state['client_epoch'],'request_id':str(uuid.uuid4()),'resume':False},consent_scope=state.get('consent_scope'))
            await conn.execute('UPDATE guardian_delivery_sessions SET state=$4::jsonb WHERE uid=$1 AND device_id=$2 AND session_id=$3',uid,d,row['session_id'],json.dumps(state))
        if closed_any:await conn.execute('UPDATE guardian_delivery_capture SET session_id=NULL WHERE uid=$1 AND device_id=$2',uid,d)
