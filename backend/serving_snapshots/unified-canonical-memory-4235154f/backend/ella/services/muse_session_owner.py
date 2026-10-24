"""Candidate read-only per-session Muse binding; no token issuance or grants.

Requires an explicitly assistant-bound durable grant. Existing configured-owner,
static tokens and unbound legacy grants are never admitted by this adapter.
"""
import asyncio,time
from ella.services.muse_private_reader import ReaderError,digest

def closed_string_list(value):
    return (type(value) is list and all(type(item) is str for item in value)
            and len(set(value))==len(value))

NAMESPACE='ella.muse.private-reader.v1'
SCOPES={'tools:read','timeline:read'}
TOOLS={'transcripts_list','transcript_get'}

class SessionOwner:
    def __init__(self,validate,load_grant,clock=time.time):
        self.validate,self.load_grant,self.clock=validate,load_grant,clock

    async def resolve(self,token,tool):
        if tool not in TOOLS:raise ReaderError('tool_not_allowed',403)
        claims=await self.validate(token)
        if not isinstance(claims,dict):raise ReaderError('connector_binding_required',403)
        for key in ('profile_uid','grant_id','sub','assistant_ref'):
            if not isinstance(claims.get(key),str) or not claims[key]:raise ReaderError('connector_binding_required',403)
        if (claims.get('iss')!='ella-mcp' or claims.get('aud')!='ella-muse-private-reader'
            or claims.get('resource')!='https://api.ella-ai-care.com/v1/ella/muse/mcp'
            or claims.get('connector_namespace')!=NAMESPACE or claims.get('role')!='self'
            or type(claims.get('iat')) is not int or type(claims.get('exp')) is not int
            or not claims['iat']<=self.clock()<claims['exp']<=claims['iat']+900):
            raise ReaderError('connector_binding_required',403)
        if (not closed_string_list(claims.get('scopes')) or set(claims['scopes'])!=SCOPES
            or not closed_string_list(claims.get('allowed_tools'))
            or not set(claims['allowed_tools'])<=TOOLS or tool not in claims['allowed_tools']):
            raise ReaderError('tool_not_allowed',403)
        grant=await self.load_grant(claims['grant_id'])
        metadata=grant.get('metadata') if isinstance(grant,dict) else None
        if (not isinstance(metadata,dict) or grant.get('status')!='active'
            or grant.get('profile_uid')!=claims['profile_uid'] or grant.get('role')!='self'
            or grant.get('provider_subject')!=claims['sub']
            or metadata.get('connector_namespace')!=NAMESPACE
            or metadata.get('assistant_ref')!=claims['assistant_ref']
            or type(metadata.get('expires_at_unix')) is not int
            or not self.clock()<metadata['expires_at_unix']
            or claims['exp']>metadata['expires_at_unix']
            or not isinstance(metadata.get('profile_binding_id'),str)
            or not metadata['profile_binding_id']
            or claims.get('profile_binding_id')!=metadata['profile_binding_id']
            or not closed_string_list(grant.get('scopes'))
            or not closed_string_list(grant.get('allowed_tools'))
            or set(grant['scopes'])!=SCOPES
            or set(grant.get('allowed_tools') or [])!=set(claims['allowed_tools'])):
            raise ReaderError('connector_binding_retired',403)
        # Firestore administrative timestamps/arbitrary metadata are not JSON
        # token claims and are not authority. Fence only validated security fields.
        fence_claims={key:claims.get(key) for key in ('iss','aud','iat','exp','sub',
            'profile_uid','grant_id','assistant_ref','connector_namespace','role','profile_binding_id')}
        fence_claims['scopes']=sorted(claims['scopes'])
        fence_claims['allowed_tools']=sorted(claims['allowed_tools'])
        fence_grant={key:grant.get(key) for key in ('status','profile_uid','role','provider_subject')}
        fence_grant['scopes']=sorted(grant['scopes'])
        fence_grant['allowed_tools']=sorted(grant['allowed_tools'])
        fence_grant['metadata']={key:metadata[key] for key in
            ('connector_namespace','assistant_ref','expires_at_unix','profile_binding_id')}
        return claims['profile_uid'],digest({'claims':fence_claims,'grant':fence_grant})

    async def invoke(self,token,tool,args,reader_factory):
        if not isinstance(args,dict):raise ReaderError('invalid_arguments',400)
        async with asyncio.timeout(10):
            uid,fence=await self.resolve(token,tool)
            reader=await reader_factory('muse_fictional_test')
            if tool=='transcripts_list':
                if set(args)-{'cursor','limit'}:raise ReaderError('invalid_arguments',400)
                result=await reader.sources(uid,**args)
            else:
                if set(args)!={'source_id','revision'}:raise ReaderError('invalid_arguments',400)
                result=await reader.source(uid,**args)
            after_uid,after=await self.resolve(token,tool)
            if uid!=after_uid or after!=fence:raise ReaderError('connector_binding_changed',409)
            return result


def production_session_owner():
    from ella.services.mcp_identity import validate_mcp_session_token
    async def validate(token):
        try:return validate_mcp_session_token(token,audience='ella-muse-private-reader')
        except Exception:raise ReaderError('connector_auth_required',401) from None
    async def load_grant(grant_id):
        from database._client import db
        import os
        ref=db.collection(os.getenv('ELLA_MCP_IDENTITY_GRANTS_COLLECTION','mcp_identity_grants')).document(grant_id)
        grant=await asyncio.wait_for(asyncio.to_thread(lambda:ref.get(timeout=3,retry=None).to_dict()),4)
        if not isinstance(grant,dict):return None
        from ella.services.ai_consent import get_ai_consent_service
        status=await asyncio.wait_for(asyncio.to_thread(get_ai_consent_service().status,grant.get('profile_uid')),4)
        if (status.get('subject_uid')!=grant.get('profile_uid') or status.get('authorized') is not True
            or (status.get('consent') or {}).get('profile_binding_id')!=(grant.get('metadata') or {}).get('profile_binding_id')):
            raise ReaderError('connector_consent_changed',403)
        return grant
    return SessionOwner(validate,load_grant)
