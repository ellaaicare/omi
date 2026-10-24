"""SOURCE candidate; no main.py registration or live policy/grant mutation yet."""
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from utils.ella.exact_firebase_auth import FirebaseTokenIdentity,get_firebase_token_identity
from ella.services.thin_cloudbot_consent_contract import policy
from ella.services.thin_cloudbot_consent import ThinCloudbotConsentService,FirestoreThinConsentRepository,ThinConsentHold
router=APIRouter()
class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision:Literal['granted','declined','revoked']
    policy_version:str
    processor_set_hash:str
    scope_version:str
    scope_hash:str
    request_id:str=Field(min_length=8,max_length=128)
    app_version:str=Field(min_length=1,max_length=80)
    build_number:str=Field(min_length=1,max_length=40)
    locale:str=Field(min_length=2,max_length=40)
    account_epoch_token:str=Field(min_length=64,max_length=64)
    expected_current_receipt_id:str=Field(max_length=80)
def service():
    from database._client import db
    return ThinCloudbotConsentService(FirestoreThinConsentRepository(db))
def run(fn):
    try:return fn()
    except ThinConsentHold as e:
        code=e.code if e.code.startswith('thin_') else 'thin_consent_unavailable'
        raise HTTPException(status_code=409,detail={'code':code}) from e
    except Exception as e:raise HTTPException(status_code=503,detail={'code':'thin_consent_unavailable'}) from e
@router.get('/v1/users/thin-cloudbot-consent/policy')
def get_policy(identity:FirebaseTokenIdentity=Depends(get_firebase_token_identity)):
    return policy()
@router.get('/v1/users/thin-cloudbot-consent')
def get_status(identity:FirebaseTokenIdentity=Depends(get_firebase_token_identity)):
    return run(lambda:service().status(identity.uid,identity.auth_time))
@router.get('/v1/users/thin-cloudbot-consent/receipts/{receipt_id}')
def get_receipt(receipt_id:str,identity:FirebaseTokenIdentity=Depends(get_firebase_token_identity)):
    value=run(lambda:service().receipt(identity.uid,identity.auth_time,receipt_id))
    if value is None:raise HTTPException(status_code=404,detail={'code':'thin_consent_receipt_not_found'})
    return {'receipt':value}
@router.post('/v1/users/thin-cloudbot-consent')
def decide(body:Decision,identity:FirebaseTokenIdentity=Depends(get_firebase_token_identity)):
    return run(lambda:service().submit(identity.uid,identity.auth_time,body.model_dump()))

@router.post('/v1/users/thin-cloudbot-consent/decisions/reconcile')
def reconcile(body:Decision,identity:FirebaseTokenIdentity=Depends(get_firebase_token_identity)):
    return run(lambda:service().reconcile(identity.uid,identity.auth_time,body.model_dump()))
