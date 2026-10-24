"""Scoped OAuth pilot. Default locked; no grants until an owner approval ticket.

Public clients, S256 PKCE, body-only Firebase credential, no refresh flow.
All persistence and signing go through injected production owner adapters.
"""
import base64,hashlib,hmac,re,secrets,time
from urllib.parse import urlparse,urlencode
from ella.services.muse_private_reader import ReaderError,digest

RESOURCE='https://api.ella-ai-care.com/v1/ella/muse/mcp'
NAMESPACE='ella.muse.private-reader.v1'
SCOPES={'tools:read','timeline:read'}
TOOLS=['transcripts_list','transcript_get']

def need(condition,code,status=400):
    if not condition:raise ReaderError(code,status)

def string(value,maximum=256):
    return type(value) is str and 0<len(value)<=maximum

def permitted(permit,now):
    need(type(permit) is dict and permit.get('contract')=='ella.muse.oauth.pilot.v1'
         and permit.get('approved') is True,'pilot_not_released',503)
    for key in ('attempt_id','grant_id','owner_uid','profile_binding_id','assistant_ref'):
        need(string(permit.get(key)),'pilot_not_released',503)
    need(type(permit.get('not_before')) is int and type(permit.get('expires_at')) is int
         and permit['not_before']<=now<permit['expires_at']<=permit['not_before']+900,
         'pilot_expired',403)
    need(type(permit.get('allowed_redirect_uris')) is list and len(permit['allowed_redirect_uris'])==1
         and string(permit['allowed_redirect_uris'][0],2048),'approved_callback_required',503)
    return digest({k:permit[k] for k in ('contract','approved','attempt_id','grant_id','owner_uid',
                                      'profile_binding_id','assistant_ref','not_before','expires_at','allowed_redirect_uris')})

class OAuthPilot:
    def __init__(self,load_permit,verify_owner,ensure_grant,issue_once,revoke,clock=time.time):
        self.load_permit,self.verify_owner,self.ensure_grant=load_permit,verify_owner,ensure_grant
        self.issue_once,self.revoke,self.clock=issue_once,revoke,clock
        self.clients={};self.codes={}
    async def permit(self):
        p=await self.load_permit();return p,permitted(p,self.clock())
    async def register(self,data):
        p,fence=await self.permit()
        need(type(data) is dict,'invalid_registration')
        uris=data.get('redirect_uris')
        need(type(uris) is list and len(uris)==1 and string(uris[0],2048),'invalid_redirect')
        need(uris==p['allowed_redirect_uris'],'approved_callback_mismatch',403)
        uri=urlparse(uris[0]);need(uri.scheme=='https' and uri.hostname and not uri.username
            and not uri.password and not uri.fragment and uri.port in (None,443),'invalid_redirect')
        need(data.get('token_endpoint_auth_method','none')=='none','public_client_required')
        need(data.get('grant_types',['authorization_code'])==['authorization_code']
             and data.get('response_types',['code'])==['code'],'authorization_code_only')
        self.clients={k:v for k,v in self.clients.items() if v['expires_at']>self.clock()}
        need(len(self.clients)<16,'registration_capacity',503)
        client_id='ella-muse-'+secrets.token_hex(16)
        record={'client_id':client_id,'redirect_uri':uris[0],'permit_fence':fence,'expires_at':p['expires_at']}
        self.clients[client_id]=record
        return {'client_id':client_id,'client_id_issued_at':int(self.clock()),'redirect_uris':uris,
                'grant_types':['authorization_code'],'response_types':['code'],'token_endpoint_auth_method':'none'}
    async def request(self,data):
        p,fence=await self.permit();need(type(data) is dict,'invalid_request')
        need(set(data)<= {'response_type','client_id','redirect_uri','state','scope','code_challenge',
             'code_challenge_method','resource','firebase_id_token'},'invalid_request')
        need(data.get('response_type')=='code','authorization_code_only')
        client=self.clients.get(data.get('client_id')) if string(data.get('client_id')) else None
        need(client is not None and client['permit_fence']==fence
             and self.clock()<client['expires_at'],'client_not_registered',403)
        need(data.get('redirect_uri')==client['redirect_uri'],'redirect_mismatch')
        need(string(data.get('state')),'state_required')
        need(data.get('resource',RESOURCE)==RESOURCE,'resource_mismatch')
        need(type(data.get('scope')) is str and set(data['scope'].split())==SCOPES,'scope_mismatch')
        challenge=data.get('code_challenge')
        need(data.get('code_challenge_method')=='S256' and type(challenge) is str
             and re.fullmatch('[A-Za-z0-9_-]{43}',challenge) is not None,'pkce_s256_required')
        return p,fence,client
    async def authorize(self,data):
        p,fence,client=await self.request(data)
        need(string(data.get('firebase_id_token'),20000),'firebase_login_required',401)
        identity=await self.verify_owner(data['firebase_id_token'],p)
        # Original credentials are not retained in code records or echoed.
        p2,fence2=await self.permit();need(fence2==fence,'pilot_changed',409)
        grant=await self.ensure_grant(p2,identity,client)
        _,after=await self.permit();need(after==fence,'pilot_changed',409)
        self.codes={k:v for k,v in self.codes.items() if v['expires_at']>self.clock()}
        need(len(self.codes)<16,'authorization_capacity',503)
        code=secrets.token_urlsafe(32)
        self.codes[code]={'client_id':client['client_id'],'redirect_uri':client['redirect_uri'],
            'challenge':data['code_challenge'],'permit_fence':fence,'grant_id':p['grant_id'],
            'expires_at':min(p['expires_at'],int(self.clock())+60),'identity':identity}
        query=urlencode({'code':code,'state':data['state']})
        return {'redirect':client['redirect_uri']+('&' if '?' in client['redirect_uri'] else '?')+query}
    async def token(self,data):
        need(type(data) is dict and set(data)<= {'grant_type','code','client_id','redirect_uri','code_verifier'},'invalid_token_request')
        need(data.get('grant_type')=='authorization_code','refresh_not_supported')
        need(string(data.get('code')),'invalid_code')
        code=self.codes.pop(data['code'],None) # permanently consumed before awaits
        need(code is not None and self.clock()<code['expires_at'],'invalid_code')
        p,fence=await self.permit();need(fence==code['permit_fence'],'pilot_changed',409)
        need(data.get('client_id')==code['client_id'] and data.get('redirect_uri')==code['redirect_uri'],
             'code_client_redirect_mismatch')
        verifier=data.get('code_verifier')
        need(type(verifier) is str and re.fullmatch('[A-Za-z0-9._~-]{43,128}',verifier) is not None,'invalid_pkce')
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
        need(hmac.compare_digest(challenge,code['challenge']),'invalid_pkce')
        client=self.clients.get(code['client_id'])
        need(client is not None and client['permit_fence']==fence,'client_retired',403)
        # Rechecks current consent/profile + original grant deadline, durable
        # one-shot session issue marker before signing. Never a token retry.
        result=await self.issue_once(p,code['identity'],client)
        _,after=await self.permit();need(after==fence,'pilot_changed',409)
        return result

# Production adapters are locked unless the backend owner publishes ONE exact
# approved private ticket. The ticket/grant/vault actions require separate release.
def production_oauth_pilot():
    import asyncio,json,os,stat,uuid
    from pathlib import Path
    from datetime import datetime,timezone
    from ella.services.mcp_identity import (ExternalConnectorIdentity,MCPProfileGrant,
        MCPIdentityResolution,grant_to_firestore_data,issue_mcp_session_token,
        validate_mcp_session_token)
    from ella.services.ai_consent import get_ai_consent_service
    from database._client import db
    from google.cloud import firestore
    collection=db.collection(os.getenv('ELLA_MCP_IDENTITY_GRANTS_COLLECTION','mcp_identity_grants'))
    mutation_holds=set()
    async def mutation(kind,p,operation):
        key=(p['attempt_id'],p['grant_id'])
        need(key not in mutation_holds,'mutation_outcome_unknown_no_retry',409)
        try:
            return await asyncio.wait_for(asyncio.to_thread(operation),4)
        except ReaderError:
            raise
        except asyncio.CancelledError:
            mutation_holds.add(key)
            raise
        except Exception:
            # A timeout/reap does not prove the Firestore worker or commit ended.
            # Keep the exact attempt held; owner readback and ticket retirement
            # are required before any process restart, never an automatic retry.
            mutation_holds.add(key)
            raise ReaderError('mutation_outcome_unknown_no_retry',409) from None
    async def load_permit():
        path=Path('/run/ella-muse-reader-pilot.json')
        def read():
            info=path.lstat();need(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                and info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,
                'pilot_ticket_identity',503)
            raw=path.read_bytes();need(len(raw)<=8192,'pilot_ticket_identity',503)
            return json.loads(raw)
        try:return await asyncio.wait_for(asyncio.to_thread(read),2)
        except ReaderError:raise
        except Exception:raise ReaderError('pilot_not_released',503) from None
    async def consent(p):
        status=await asyncio.wait_for(asyncio.to_thread(get_ai_consent_service().status,p['owner_uid']),4)
        need(status.get('subject_uid')==p['owner_uid'] and status.get('authorized') is True
             and (status.get('consent') or {}).get('profile_binding_id')==p['profile_binding_id'],
             'current_consent_required',403)
    async def verify_owner(token,p):
        from firebase_admin import auth
        from ella.routers.mcp_onboarding import _identity_from_firebase_claims
        try:decoded=await asyncio.wait_for(asyncio.to_thread(auth.verify_id_token,token,check_revoked=True),4)
        except Exception:raise ReaderError('firebase_login_required',401) from None
        uid=decoded.get('uid') or decoded.get('sub')
        need(uid==p['owner_uid'],'pilot_owner_mismatch',403)
        await consent(p)
        identity=_identity_from_firebase_claims(decoded)
        need(identity.authenticated and identity.provider=='google','google_owner_required',403)
        return ExternalConnectorIdentity(provider=identity.provider,subject=identity.subject,authenticated=True)
    async def ensure_grant(p,identity,client):
        await consent(p)
        metadata={'connector_namespace':NAMESPACE,'assistant_ref':p['assistant_ref'],
            'profile_binding_id':p['profile_binding_id'],'expires_at_unix':p['expires_at'],
            'oauth_client_id':client['client_id'],'oauth_redirect_uri':client['redirect_uri'],
            'attempt_id':p['attempt_id'],'session_issued':False}
        data=grant_to_firestore_data(identity=identity,profile_uid=p['owner_uid'],role='self',
            profile_label='Ella Test fictional sources',scopes=sorted(SCOPES),allowed_tools=TOOLS,
            grant_id=p['grant_id'],created_by='root-approved-muse-fictional-pilot')
        data['metadata']=metadata;data['created_at']=data['updated_at']
        ref=collection.document(p['grant_id'])
        @firestore.transactional
        def create_once(transaction):
            permitted(p,time.time())
            existing=ref.get(transaction=transaction,timeout=3,retry=None)
            permitted(p,time.time())
            if existing.exists:
                g=existing.to_dict() or {};md=g.get('metadata') or {}
                need(g.get('status')=='active' and g.get('role')=='self' and g.get('profile_uid')==p['owner_uid']
                    and g.get('provider_subject')==identity.provider_subject
                    and all(md.get(k)==v for k,v in metadata.items() if k!='session_issued'),
                    'grant_preimage_conflict',409)
                need(md.get('session_issued') is False,'session_already_consumed',409)
                return g
            permitted(p,time.time())
            transaction.create(ref,data);return data
        # No Firestore automatic transaction replay at this authority boundary.
        return await mutation('grant_create',p,lambda:create_once(db.transaction(max_attempts=1)))
    async def issue_once(p,identity,client):
        await consent(p)
        ref=collection.document(p['grant_id'])
        @firestore.transactional
        def consume(transaction):
            permitted(p,time.time())
            snap=ref.get(transaction=transaction,timeout=3,retry=None);g=snap.to_dict() or {};md=g.get('metadata') or {}
            permitted(p,time.time())
            need(g.get('status')=='active' and g.get('role')=='self' and g.get('profile_uid')==p['owner_uid']
                and g.get('provider_subject')==identity.provider_subject
                and md.get('assistant_ref')==p['assistant_ref'] and md.get('profile_binding_id')==p['profile_binding_id']
                and md.get('connector_namespace')==NAMESPACE and md.get('expires_at_unix')==p['expires_at']
                and md.get('oauth_client_id')==client['client_id'] and md.get('oauth_redirect_uri')==client['redirect_uri']
                and md.get('session_issued') is False and g.get('scopes')==sorted(SCOPES)
                and g.get('allowed_tools')==sorted(TOOLS),'grant_not_issuable',403)
            permitted(p,time.time())
            transaction.update(ref,{'metadata.session_issued':True,'metadata.session_issued_at':datetime.now(timezone.utc)})
            return g
        grant=await mutation('session_issue',p,lambda:consume(db.transaction(max_attempts=1)))
        permitted(p,time.time())
        resolution=MCPIdentityResolution(state='authenticated_mapped',trace_id=str(uuid.uuid4()),
            identity=identity,selected_grant=MCPProfileGrant.from_mapping(grant))
        token,claims=issue_mcp_session_token(resolution,ttl_seconds=max(1,p['expires_at']-int(time.time())))
        return {'access_token':token,'token_type':'Bearer','expires_in':max(0,claims['exp']-int(time.time())),
                'scope':' '.join(sorted(SCOPES))} # no profile, identity or grant body
    async def revoke(token,client_id=None):
        try:claims=validate_mcp_session_token(token,audience='ella-muse-private-reader')
        except ValueError:return # invalid/expired tokens disclose nothing
        need(claims.get('connector_namespace')==NAMESPACE,'wrong_resource',403)
        ref=collection.document(claims['grant_id'])
        @firestore.transactional
        def close(transaction):
            snap=ref.get(transaction=transaction,timeout=3,retry=None);g=snap.to_dict() or {};md=g.get('metadata') or {}
            need(g.get('profile_uid')==claims.get('profile_uid') and md.get('assistant_ref')==claims.get('assistant_ref')
                and md.get('connector_namespace')==NAMESPACE and md.get('profile_binding_id')==claims.get('profile_binding_id'),
                'revoke_binding_mismatch',403)
            need(client_id is None or client_id==md.get('oauth_client_id'),'revoke_client_mismatch',403)
            transaction.update(ref,{'status':'inactive','metadata.revoked_at':datetime.now(timezone.utc)})
        await asyncio.wait_for(asyncio.to_thread(close,db.transaction(max_attempts=1)),4)
    return OAuthPilot(load_permit,verify_owner,ensure_grant,issue_once,revoke)
