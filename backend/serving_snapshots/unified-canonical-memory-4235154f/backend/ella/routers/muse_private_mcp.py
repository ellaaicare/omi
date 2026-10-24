"""Candidate stateless MCP read transport; no static-owner auth or write tools."""
import json
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import JSONResponse
from ella.routers.muse_private_reader import call_reader,production_reader
from ella.services.muse_session_owner import production_session_owner
from ella.services.muse_private_reader import ReaderError
router=APIRouter(prefix='/v1/ella/muse')
TOOLS=[{'name':'transcripts_list','description':'Read new fictional source metadata since an owner-bound cursor.',
 'inputSchema':{'type':'object','properties':{'cursor':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':50}},'additionalProperties':False}},
 {'name':'transcript_get','description':'Read an exact current fictional source revision.',
 'inputSchema':{'type':'object','properties':{'source_id':{'type':'string'},'revision':{'type':'string'}},'required':['source_id','revision'],'additionalProperties':False}}]
@router.post('/mcp')
async def mcp_read(request:Request):
    auth=request.headers.get('authorization','')
    if not auth.startswith('Bearer ') or not auth[7:].strip():raise HTTPException(401,detail={'code':'connector_auth_required'})
    async def operation():
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>65536:raise ReaderError('request_bound',413)
            raw.extend(chunk)
        try:body=json.loads(raw)
        except (ValueError,UnicodeError):raise ReaderError('invalid_request',400) from None
        if not isinstance(body,dict) or body.get('jsonrpc')!='2.0' or set(body)-{'jsonrpc','id','method','params'}:raise ReaderError('invalid_request',400)
        reqid=body.get('id');method=body.get('method');params=body.get('params') or {}
        if not isinstance(params,dict):raise ReaderError('invalid_request',400)
        token=auth[7:].strip();owner=production_session_owner()
        uid,fence=await owner.resolve(token,'transcripts_list')
        if method=='notifications/initialized' and 'id' not in body:return None,None
        if type(reqid) not in (str,int) or isinstance(reqid,str) and len(reqid)>128:raise ReaderError('invalid_request',400)
        if method in ('initialize','tools/list'):
            reader=await production_reader('muse_fictional_test')
            await reader.d.guard(uid)
            after_uid,after=await owner.resolve(token,'transcripts_list')
            if uid!=after_uid or after!=fence:raise ReaderError('connector_binding_changed',409)
            if method=='initialize':
                if params.get('protocolVersion')!='2025-03-26':raise ReaderError('protocol_not_supported',400)
                return reqid,{'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'ella-private-fictional-reader','version':'1'}}
            return reqid,{'tools':TOOLS}
        if method!='tools/call' or set(params)!={'name','arguments'}:raise ReaderError('method_not_allowed',400)
        result=await owner.invoke(token,params['name'],params['arguments'],production_reader)
        return reqid,{'content':[{'type':'text','text':json.dumps(result,separators=(',',':'),ensure_ascii=False)}],'isError':False}
    reqid,result=await call_reader(operation)
    if reqid is None:return JSONResponse(None,status_code=202)
    return JSONResponse({'jsonrpc':'2.0','id':reqid,'result':result},headers={'Cache-Control':'private, no-store'})
