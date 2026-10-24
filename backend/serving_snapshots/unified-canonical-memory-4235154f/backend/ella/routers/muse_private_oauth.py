"""Candidate OAuth browser/transport. No active pilot ticket by default."""
import asyncio,json,html
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse,HTMLResponse
from ella.services.muse_private_reader import ReaderError
from ella.routers.muse_private_reader import call_reader
from ella.services.muse_private_oauth import production_oauth_pilot,RESOURCE
from ella.services.muse_rejected_dcr_diagnostic import observe_rejected_registration
router=APIRouter(prefix='/v1/ella/muse/oauth')
_service=None
def service():
    global _service
    if _service is None:_service=production_oauth_pilot()
    return _service
async def body(request,json_only=False):
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>32768:raise ReaderError('request_bound',413)
        raw.extend(chunk)
    try:
        if 'application/json' in request.headers.get('content-type',''):
            value=json.loads(raw)
            if type(value) is not dict:raise ValueError
            return value
        if json_only:raise ValueError
        from urllib.parse import parse_qs
        parsed=parse_qs(raw.decode(),strict_parsing=True)
        if any(len(v)!=1 for v in parsed.values()):raise ValueError
        return {k:v[0] for k,v in parsed.items()}
    except (ValueError,UnicodeError):raise ReaderError('invalid_request',400) from None
@router.post('/register')
async def register(request:Request):
    async def operation():
        data=await body(request,True)
        try:return await service().register(data)
        except ReaderError as error:
            if error.status==503 and error.code=='pilot_not_released':observe_rejected_registration(data)
            raise
    return JSONResponse(await call_reader(operation),status_code=201,headers={'Cache-Control':'no-store'})
@router.get('/authorize')
async def authorize_page(request:Request):
    async def operation():
        params=dict(request.query_params)
        if len(params)!=len(request.query_params.multi_items()) or 'firebase_id_token' in params:raise ReaderError('body_login_required',400)
        await service().request(params)
        from ella.routers.mcp_onboarding import _firebase_web_config
        config=_firebase_web_config()
        if not all(config.values()):raise ReaderError('firebase_web_setup_missing',503)
        # Escape '<' before embedding JSON in script; no token or owner values.
        data=json.dumps({'request':params,'firebase':config},separators=(',',':')).replace('<','\\u003c')
        return data
    data=await call_reader(operation)
    page='''<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Connect Ella Test</title>
<script src="https://www.gstatic.com/firebasejs/9.6.1/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/9.6.1/firebase-auth-compat.js"></script></head><body>
<h1>Connect Ella Test</h1><p>Read only new fictional test sources for this assistant, for at most 15 minutes. No personal history, capture, memory writes, or refresh access.</p>
<button id="connect">Sign in with Google and approve this fictional connection</button><p id="status"></p>
<script>const setup=__DATA__;firebase.initializeApp(setup.firebase);
document.getElementById('connect').onclick=async()=>{const button=document.getElementById('connect');button.disabled=true;
try{const signed=await firebase.auth().signInWithPopup(new firebase.auth.GoogleAuthProvider());
const idToken=await signed.user.getIdToken(true);
const response=await fetch('/v1/ella/muse/oauth/authorize',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({...setup.request,firebase_id_token:idToken}),cache:'no-store',credentials:'same-origin'});
if(!response.ok)throw Error('not-admitted');const result=await response.json();window.location.assign(result.redirect);
}catch(_){document.getElementById('status').textContent='Connection not completed. Close this page; no automatic retry.';}}
</script></body></html>'''.replace('__DATA__',data)
    return HTMLResponse(page,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'})
@router.post('/authorize')
async def authorize(request:Request):
    async def operation():
        if request.headers.get('origin')!='https://api.ella-ai-care.com':raise ReaderError('same_origin_required',403)
        return await service().authorize(await body(request,True))
    return JSONResponse(await call_reader(operation),headers={'Cache-Control':'no-store'})
@router.post('/token')
async def token(request:Request):
    async def operation():return await service().token(await body(request))
    return JSONResponse(await call_reader(operation),headers={'Cache-Control':'no-store'})
@router.post('/revoke')
async def revoke(request:Request):
    async def operation():
        data=await body(request)
        if set(data)-{'token','token_type_hint','client_id'} or type(data.get('token')) is not str:raise ReaderError('invalid_revoke',400)
        await service().revoke(data['token'],data.get('client_id'))
        return {}
    return JSONResponse(await call_reader(operation),headers={'Cache-Control':'no-store'})
