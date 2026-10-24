"""OFFLINE candidate: pure exact-item pending admission. No I/O/tool/grant change.
Not integrated into deployed CAS. Controller alone stages these fields.
"""
import copy
import hashlib
import uuid
from datetime import datetime,timedelta
FIELD='mcp_synthetic_pending_v1'
CONTRACT='ella-exact-synthetic-pending-v1'
KEYS={'contract','run_id','owner_ref_sha256','source_id','source_sha256','expected_snapshot_sha256','expected_active_summary_version_id','operation_id','ready_at','expires_at','state','saved_version_id','saved_report_sha256'}
class BindingError(ValueError):pass
def need(v,c):
 if not v:raise BindingError(c)
def canonical_uuid(v):
 try:return type(v) is str and str(uuid.UUID(v))==v
 except (ValueError,TypeError,AttributeError):return False
def hexhash(v):return type(v) is str and len(v)==64 and all(c in '0123456789abcdef' for c in v)
def validate(binding,uid,cid):
 need(type(binding) is dict and set(binding)==KEYS,'pending_shape')
 need(binding['contract']==CONTRACT and canonical_uuid(binding['run_id']) and canonical_uuid(binding['operation_id']),'pending_identity')
 need(binding['owner_ref_sha256']==hashlib.sha256(uid.encode()).hexdigest() and binding['source_id']==cid,'pending_owner_source')
 need(hexhash(binding['source_sha256']) and hexhash(binding['expected_snapshot_sha256']) and canonical_uuid(binding['expected_active_summary_version_id']),'pending_source_pins')
 a,b=binding['ready_at'],binding['expires_at']
 need(isinstance(a,datetime) and a.tzinfo is not None and isinstance(b,datetime) and b.tzinfo is not None and timedelta(0)<b-a<=timedelta(minutes=30),'pending_window')
 need(binding['state'] in ('pending','completed','retired'),'pending_state')
 if binding['state']=='completed':need(canonical_uuid(binding['saved_version_id']) and hexhash(binding['saved_report_sha256']),'pending_result')
 else:need(binding['saved_version_id'] is None and binding['saved_report_sha256'] is None,'pending_result')
 return copy.deepcopy(binding)
def project(binding,uid,cid,now):
 b=validate(binding,uid,cid);b['available']=b['state']=='pending' and b['ready_at']<=now<b['expires_at'];return b

def admit_save(binding,uid,cid,arguments,now,*,live_snapshot,live_version,source_sha256):
 b=validate(binding,uid,cid);e=arguments.get('enrichment',{})
 need(e.get('contract')=='omi-mcp-summary-enrichment-v2' and e.get('operation_id')==b['operation_id'],'pending_operation')
 need(arguments.get('conversation_id')==cid and e.get('expected_snapshot_sha256')==b['expected_snapshot_sha256'] and e.get('expected_active_summary_version_id')==b['expected_active_summary_version_id'],'pending_request')
 need(source_sha256==b['source_sha256'],'pending_source_changed')
 if b['state']=='completed':
  # Existing ledger must independently confirm exact request + current result.
  # This helper never grants a write/replay by itself.
  need(live_version==b['saved_version_id'],'pending_superseded');return 'ledger_confirmation_only'
 need(b['state']=='pending' and b['ready_at']<=now<b['expires_at'],'pending_closed')
 need(live_snapshot==b['expected_snapshot_sha256'] and live_version==b['expected_active_summary_version_id'],'pending_source_changed')
 return 'new_commit_allowed'
def completed(binding,result):
 need(result.get('success') is True and result.get('canonical_write') is False and result.get('operation_id')==binding['operation_id'] and canonical_uuid(result.get('summary_version_id')) and hexhash(result.get('source_report_sha256')),'pending_commit_result')
 return {**copy.deepcopy(binding),'state':'completed','saved_version_id':result['summary_version_id'],'saved_report_sha256':result['source_report_sha256']}
