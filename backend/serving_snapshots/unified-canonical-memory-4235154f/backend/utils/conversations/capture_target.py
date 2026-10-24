"""Server-stored capture target namespace; real account UID is never substituted."""
import hashlib,json
FIELD='thin_cloudbot_source_binding'
def scope_for_document(document):
    binding=(document or {}).get(FIELD)
    if binding is None:return None
    if not isinstance(binding,dict):raise ValueError('thin_capture_target_invalid')
    from ella.services.thin_cloudbot_request_authority import validate_scope
    return validate_scope(binding.get('consent_scope'))
def namespace(scope):
    if scope is None:return None
    from ella.services.thin_cloudbot_request_authority import validate_scope
    return 'thin-cloudbot:'+hashlib.sha256(json.dumps(validate_scope(scope),sort_keys=True,separators=(',',':')).encode()).hexdigest()
def namespace_for_conversation(uid,cid):
    from database._client import db
    snap=db.collection('users').document(uid).collection('conversations').document(cid).get()
    return namespace(scope_for_document(snap.to_dict() if snap.exists else None))
class CaptureRedis:
    """Only capture-key methods select target namespace; no fake UID/proxy auth."""
    METHODS={'get_in_progress_conversation_id','get_in_progress_conversation_owner',
     'set_in_progress_conversation_id','claim_in_progress_conversation_id',
     'replace_stale_in_progress_conversation_id','refresh_in_progress_conversation_id',
     'rotate_in_progress_conversation_id','acquire_capture_commit_lease','release_capture_commit_lease',
     'release_unowned_in_progress_conversation_id','release_owned_in_progress_conversation_id',
     'acquire_in_progress_processing_fence'}
    def __init__(self,module,scope):self.module=module;self.namespace=namespace(scope)
    def __getattr__(self,name):
        fn=getattr(self.module,name)
        if name not in self.METHODS:return fn
        return lambda *a,**kw:fn(*a,**kw,capture_namespace=self.namespace)


def authority_document(scope):
    value=namespace(scope)
    return 'current' if value is None else value.replace(':','-')

def assert_target_relation(authority_ref, authority, document):
    """Recheck the source-derived target inside the SAME authority transaction."""
    scope=scope_for_document(document)
    if authority_ref.id != authority_document(scope):
        raise ValueError('capture_authority_target_changed')
    if authority and authority.get('consent_scope') != scope:
        raise ValueError('capture_authority_target_mismatch')
    return scope

def require_document_grant(tx,uid,document,now):
    scope=scope_for_document(document)
    if scope is None:return
    binding=document[FIELD]
    from ella.services.thin_cloudbot_request_authority import default_service
    from ella.services.thin_cloudbot_consent import need
    need(type(binding.get('verified_auth_time')) is int and binding['verified_auth_time']>0,
         'thin_capture_verified_epoch_missing')
    need(binding.get('source_id')==document.get('id'), 'thin_capture_source_binding_changed')
    service=default_service()
    witness=service.repository.assert_witness_in_transaction(tx,uid,binding['verified_auth_time'],binding.get('authority'),now)
    need(all(witness.get(k)==v for k,v in scope.items()), 'thin_capture_scope_changed')
