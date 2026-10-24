"""Existing Firebase + current AI consent, exact prebound test owner/source only."""
from fastapi import APIRouter,Depends,Request,HTTPException
from starlette.concurrency import run_in_threadpool
from ella.services.ai_consent import require_current_ai_consent
from utils.ella.exact_firebase_auth import get_exact_firebase_uid
from ella.services.thin_cloudbot_request_authority import parse_scope
from ella.services.thin_cloudbot_consent import ThinConsentHold
from database._client import db
from database.qualification_transcript_producer import TranscriptProducer,ProducerHold

router=APIRouter()

def require_legacy_test_owner(request:Request,authenticated_uid:str=Depends(get_exact_firebase_uid)):
    # This QA producer is still a separately prebound Main contract. A Thin
    # header must not silently fall through to a Main grant/Hermes consumer.
    try:
        selected=parse_scope(request.headers)
    except ThinConsentHold as error:
        raise HTTPException(status_code=409,detail={'code':error.code}) from None
    if selected is not None:
        raise HTTPException(status_code=409,detail={'code':'thin_qa_producer_not_supported'})
    return require_current_ai_consent(authenticated_uid)

async def bounded_body(request,limit):
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>limit:raise HTTPException(status_code=413,detail={'code':'test_transcript_body_bound'})
        body.extend(chunk)
    return bytes(body)

@router.post('/v1/conversations/test-transcripts/prepare',tags=['conversations'])
async def prepare_test_transcript(request:Request,uid:str=Depends(require_legacy_test_owner)):
    raw=await bounded_body(request,65536)
    try:return await run_in_threadpool(TranscriptProducer(db).prepare,uid,raw)
    except ProducerHold as e:raise HTTPException(status_code=409,detail={'code':str(e)}) from e
    except ValueError as e:raise HTTPException(status_code=409,detail={'code':'test_transcript_scope_or_binding_hold'}) from e

@router.post('/v1/conversations/test-transcripts/{request_id}/complete',tags=['conversations'])
async def complete_test_transcript(request_id:str,request:Request,uid:str=Depends(require_legacy_test_owner)):
    # Completion has no body: no caller-supplied source, UID, provenance or result.
    if await bounded_body(request,0):raise HTTPException(status_code=422,detail={'code':'test_transcript_completion_body_forbidden'})
    try:return await run_in_threadpool(TranscriptProducer(db).complete,uid,request_id)
    except ProducerHold as e:raise HTTPException(status_code=409,detail={'code':str(e)}) from e
    except ValueError as e:raise HTTPException(status_code=409,detail={'code':'test_transcript_scope_or_binding_hold'}) from e
