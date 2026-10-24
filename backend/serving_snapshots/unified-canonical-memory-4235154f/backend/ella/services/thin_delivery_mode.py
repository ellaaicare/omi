"""Thin saved output preference, separate from Main Guardian inference modes.
Only normal explicit On/Off handlers write it; consent grant never enables it.
"""
from ella.services.thin_cloudbot_consent import COLLECTION,need
from ella.services.thin_cloudbot_request_authority import validate_scope

def read_mode(service,uid,scope):
    scope=validate_scope(scope)
    snap=service.repository.db.collection(COLLECTION).document(uid).get()
    row=(snap.to_dict() or {}).get('delivery_mode') if snap.exists else None
    if row is None:return False
    need(type(row) is dict and set(row)=={'scope','on'} and type(row['on']) is bool,'thin_delivery_mode_invalid')
    return row['scope']==scope and row['on'] is True

def set_mode(service,uid,scope,on,*,authority=None):
    scope=validate_scope(scope);need(type(on) is bool,'thin_delivery_mode_invalid')
    def write(tx):
        if on:
            need(authority is not None and authority.uid==uid and authority.scope==scope,'thin_current_consent_required')
            authority.revalidate_in_transaction(tx,now=service.clock())
        ref=service.repository.db.collection(COLLECTION).document(uid);snap=ref.get(transaction=tx);doc=snap.to_dict() or {}
        old=doc.get('delivery_mode')
        if not on and old is not None:
            need(type(old) is dict and old.get('scope')==scope,'thin_delivery_mode_lifetime_changed')
        # Repeated Off with no Thin store need not create state or a grant.
        if not on and not snap.exists:return False
        tx.set(ref,{**doc,'delivery_mode':{'scope':scope,'on':on}})
        return on
    return service.repository._run(write)

def require_enabled(service,uid,scope):
    need(read_mode(service,uid,scope),'thin_delivery_mode_inactive')
