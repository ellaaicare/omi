"""Consistent existing capture read, compare private token only in RAM by digest.
No raw capture owner token is stored in the tool-work ledger or returned to host.
"""
import hashlib,secrets
from datetime import datetime,timezone

def matches_capture(cp,authority,conversation,proof,now):
    token=authority.get('owner_token')
    if not isinstance(token,str) or not token:return False
    expected=proof.get('owner_token_sha256')
    if not isinstance(expected,str) or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),expected):return False
    cid=proof['conversation_id'];generation=proof['generation']
    return bool(authority.get('state')=='active' and conversation.get('capture_state')=='active'
        and cp._authority_tuple_matches(authority,cid,generation,token)
        and cp._conversation_tuple_matches(conversation,cid,generation,token)
        and cp._status_value(conversation.get('status'))=='in_progress'
        and str(conversation.get('capture_owner_id') or '')==token
        and not cp._lease_expired(authority,now)
        and not cp._lease_expired(conversation,now,'capture_lease_expires_at'))

def current_hashed_capture(uid,proof):
    from utils.conversations import capture_protocol as cp
    @cp.transactional
    def read(transaction):
        authority=cp._authority_ref(uid).get(transaction=transaction)
        conversation=cp._conversation_ref(uid,proof['conversation_id']).get(transaction=transaction)
        if not authority.exists or not conversation.exists:return False
        return matches_capture(cp,authority.to_dict() or {},conversation.to_dict() or {},proof,datetime.now(timezone.utc))
    return read(cp.db.transaction())
