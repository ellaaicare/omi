"""Hidden durable target/receipt/epoch binding; no privilege from app headers.
Legacy Main sessions have no scope; cross-target adoption/operation is refused.
"""
import re
from copy import deepcopy
class DeliveryTargetHold(ValueError):pass

def canonical_scope(value):
    if value is None:return None
    if (type(value) is not dict or set(value)!={'consent_target','receipt_id','account_epoch_hash'}
        or value['consent_target']!='thin-cloudbot'
        or type(value['receipt_id']) is not str or not re.fullmatch(r'tccr_[0-9a-f]{32}',value['receipt_id'])
        or type(value['account_epoch_hash']) is not str or not re.fullmatch(r'[0-9a-f]{64}',value['account_epoch_hash'])):
        raise DeliveryTargetHold('delivery_consent_scope_invalid')
    return deepcopy(value)

def require_scope(state,supplied):
    current=canonical_scope(state.get('consent_scope'));wanted=canonical_scope(supplied)
    if current!=wanted:raise DeliveryTargetHold('delivery_consent_scope_mismatch')
    return wanted

async def revalidate(scope,checker,operation,body=None):
    scope=canonical_scope(scope)
    if scope is None:return
    body=body or {}
    cleanup=(operation=='ack' or operation=='stop' and body.get('resume',True) is False
             or operation=='report' and body.get('status') in {'canceled','discarded'})
    if cleanup:return
    if checker is None:raise DeliveryTargetHold('delivery_current_consent_required')
    actual=await checker()
    if canonical_scope(actual)!=scope:raise DeliveryTargetHold('delivery_consent_scope_changed')
