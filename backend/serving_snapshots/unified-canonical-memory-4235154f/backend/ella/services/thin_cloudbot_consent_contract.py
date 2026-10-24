"""SOURCE-ONLY proposed Thin consent projection. No I/O, auth, grants or route.
An adapter must supply verified Firebase/account-generation context and exact DB
current-state + immutable receipt. This module cannot authenticate supplied JSON.
Main v10/v11 records and helpers are deliberately not imported or mutated.
"""
import hashlib
import hmac
import json
from datetime import datetime

TARGET='thin-cloudbot'
VERSION='thin-cloudbot-consent-v1'
SCOPE_VERSION='thin-muse-owned-inference-v1'
# Exact proposed service recipients, scoped purposes; not a current-use claim.
DISCLOSURES=(
 {'id':'firebase','legal_recipient':'Google Firebase','function':'Authentication and service infrastructure','data':'Account and service metadata','third_party':True,'condition':'Required service infrastructure'},
 {'id':'ella-backend','legal_recipient':'Ella backend','function':'Owner-scoped storage, transcription routing, generic-summary processing and media delivery','data':'Authorized microphone/source audio, source transcript, saved summaries and delivery metadata','third_party':False,'condition':'No personal Hermes/Honcho/agent or self-hosted TTS inference'},
 {'id':'deepgram','legal_recipient':'Deepgram','function':'Speech transcription','data':'Live or stored microphone audio','third_party':True,'condition':'Only when selected by the supported transcription route'},
 {'id':'soniox','legal_recipient':'Soniox','function':'Speech transcription','data':'Live or stored microphone audio','third_party':True,'condition':'Only when selected by the supported transcription route'},
 {'id':'speechmatics','legal_recipient':'Speechmatics','function':'Speech transcription','data':'Live or stored microphone audio','third_party':True,'condition':'Only when selected by the supported transcription route'},
 {'id':'openrouter','legal_recipient':'OpenRouter','function':'Generic-summary model routing','data':'Source transcript for generic summary','third_party':True,'condition':'Configured generic-summary route'},
 {'id':'google-gemini','legal_recipient':'Google Gemini','function':'Generic summary','data':'Source transcript for generic summary','third_party':True,'condition':'Configured generic-summary primary through OpenRouter'},
 {'id':'groq','legal_recipient':'Groq','function':'Generic-summary model processing','data':'Source transcript for generic summary','third_party':True,'condition':'Only when supported configured generic-summary fallback is selected; never personal Hermes fallback'},
 {'id':'muse','legal_recipient':'Muse (user-connected cloud bot)','function':'Personal enhancement, memory and cloud-bot interactions','data':'Authorized source/saved context and user interactions sent to the connected cloud bot','third_party':True,'condition':'Muse first; other cloud bots or voice capabilities require separately supported actual host scope'},
)
PROCESSORS=tuple((d['id'],d['function']) for d in DISCLOSURES)
CANONICAL_PROCESSORS=json.dumps(DISCLOSURES,sort_keys=True,separators=(',',':'),ensure_ascii=True)
CANONICAL_SCOPE=('product=ella-thin|profile_binding=ella-thin-owner-consent-v1|personal_inference_owner=user-connected-cloudbot|initial_host=muse|'
                 'generic_summary=omi-backend-configured-model|personal_enhancement=cloudbot-owned|whisper_generation=cloudbot-owned|chat=cloudbot-owned|'
                 'voice=cloudbot-owned-where-supported|memory=cloudbot-owned|'
                 'backend=transcription-routing-generic-summary-and-authenticated-storage-delivery|hermes_required=false|'
                 'personal_server_model_fallback=false|external_selfhosted_intelligence=jev-scanner-only|'
                 'jev_enabled=false|raw_audio_to_jev=false|host_connection_not_implied=true')

def digest(v):
    return 'sha256:'+hashlib.sha256(v.encode('utf8')).hexdigest()

POLICY={'consent_target':TARGET,'policy_version':VERSION,
        'minimum_required_version':VERSION,'processor_set_hash':digest(CANONICAL_PROCESSORS),
        'canonical_processor_set':CANONICAL_PROCESSORS,'scope_version':SCOPE_VERSION,
        'scope_hash':digest(CANONICAL_SCOPE),'canonical_scope':CANONICAL_SCOPE,
        'processor_ids':[k for k,_ in PROCESSORS],
        'processors':json.loads(json.dumps(DISCLOSURES))}
FIELDS={'consent_target','subject_owner_sha256','account_epoch_hash','receipt_id',
        'request_id','decision','policy_version','processor_set_hash','scope_version',
        'scope_hash','processor_ids','server_decided_at','profile_binding_id','app_version','build_number','locale'}
STATE_FIELDS={'consent_target','subject_owner_sha256','account_epoch_hash','receipt_id',
              'decision','receipt_sha256','profile_binding_id'}
class ContractHold(ValueError):pass

def need(value,code):
    if not value:raise ContractHold(code)

def hexhash(v):
    return type(v) is str and len(v)==64 and all(x in '0123456789abcdef' for x in v)

def aware(value):
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return d if d.tzinfo is not None else None
    except (ValueError,TypeError,AttributeError):return None

def receipt_digest(receipt):
    return hashlib.sha256(json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def policy():
    return json.loads(json.dumps(POLICY))

def validate_policy(value):
    need(type(value) is dict and value==POLICY,'thin_policy_not_exact')
    return policy()

def validate_current_grant(*,state,receipt,authenticated_owner_sha256,
                           current_account_epoch_hash,account_active,now):
    """No lifetime/age convenience gate. Valid until revoke/epoch/policy retirement.
    Owner and epoch are trusted adapter inputs, NOT request fields. Adapter must
    revalidate after awaits and atomically fence every source/admission effect.
    """
    need(hexhash(authenticated_owner_sha256) and hexhash(current_account_epoch_hash)
         and account_active is True and isinstance(now,datetime) and now.tzinfo is not None,
         'thin_authenticated_context_required')
    need(type(state) is dict and set(state)==STATE_FIELDS and type(receipt) is dict
         and set(receipt)==FIELDS,'thin_receipt_shape')
    need(state['consent_target']==receipt['consent_target']==TARGET,'thin_target_mismatch')
    need(state['subject_owner_sha256']==receipt['subject_owner_sha256']==authenticated_owner_sha256,
         'thin_owner_mismatch')
    need(state['account_epoch_hash']==receipt['account_epoch_hash']==current_account_epoch_hash,
         'thin_account_epoch_mismatch')
    need(type(receipt['profile_binding_id']) is str and state['profile_binding_id']==receipt['profile_binding_id'] and bool(receipt['profile_binding_id']),'thin_profile_binding_mismatch')
    need(state['decision']==receipt['decision']=='granted','thin_not_granted')
    need(type(receipt['receipt_id']) is str and 0<len(receipt['receipt_id'])<=128
         and state['receipt_id']==receipt['receipt_id'],'thin_receipt_identity')
    need(type(receipt['request_id']) is str and 8<=len(receipt['request_id'])<=128,
         'thin_request_identity')
    for key in ('policy_version','processor_set_hash','scope_version','scope_hash','processor_ids'):
        need(receipt[key]==POLICY[key],'thin_policy_not_exact')
    for key,low,high in [('app_version',1,80),('build_number',1,40),('locale',2,40)]:
        value=receipt[key];need(type(value) is str and low<=len(value)<=high and all(32<ord(c)<127 for c in value),'thin_receipt_metadata_invalid')
    at=aware(receipt['server_decided_at']);need(at is not None and at<=now,'thin_server_timestamp_invalid')
    need(hexhash(state['receipt_sha256']) and hmac.compare_digest(state['receipt_sha256'],receipt_digest(receipt)),
         'thin_current_receipt_mismatch')
    return {'consent_target':TARGET,'authorized':True,'receipt_id':receipt['receipt_id'],
            'policy_version':VERSION,'connection_verified':False,'pending_ready':False,
            'enrichment_success':False,'hermes_required':False,'jev_authorized':False}
