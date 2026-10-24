"""Normal app-auth setup metadata plus nonsecret public guidance, no grant API."""
import os
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from dependencies import get_current_user_id
from ella.services.mcp_public_setup import SetupError, SetupIntents, public_page


def enabled(): return os.environ.get('ELLA_MCP_SETUP_GUIDANCE_ENABLED')=='true'


def service():
    from database._client import db
    from google.cloud.firestore_v1 import transactional
    return SetupIntents(db,transactional,enabled=enabled())


class SetupRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode: Literal['fresh_ella','add_existing_muse']
    request_id: UUID


def _call(fn):
    try:return fn()
    except SetupError as error:raise HTTPException(error.status,detail={'code':error.code}) from error


def private_response(response):
    response.headers['Cache-Control']='no-store'
    response.headers['Pragma']='no-cache'


router=APIRouter(tags=['Ella public setup'])


@router.get('/ella/setup/{choice}',response_class=HTMLResponse)
def guidance(choice:str,request:Request):
    if request.query_params:raise HTTPException(400,detail={'code':'setup_public_query_not_supported'})
    mode={'new':'fresh_ella','add':'add_existing_muse'}.get(choice)
    page=_call(lambda:public_page(mode,enabled=enabled()))
    return HTMLResponse(page,headers={'Cache-Control':'public, max-age=300','Referrer-Policy':'no-referrer',
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})


@router.post('/v1/ella/mcp/setup-intents')
def create_setup(body:SetupRequest,response:Response,uid:str=Depends(get_current_user_id)):
    private_response(response)
    return _call(lambda:service().create(uid,body.mode,str(body.request_id)))


@router.get('/v1/ella/mcp/setup-intents/{setup_id}')
def setup_status(setup_id:UUID,response:Response,uid:str=Depends(get_current_user_id)):
    private_response(response)
    return _call(lambda:service().status(uid,str(setup_id)))


@router.delete('/v1/ella/mcp/setup-intents/{setup_id}')
def cancel_setup(setup_id:UUID,response:Response,uid:str=Depends(get_current_user_id)):
    private_response(response)
    return _call(lambda:service().cancel(uid,str(setup_id)))
