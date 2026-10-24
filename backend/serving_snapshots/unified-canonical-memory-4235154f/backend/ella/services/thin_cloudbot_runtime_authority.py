"""Dedicated-target runtime proof. No Main consent or model fallback.
Call only with verified first-party identity, never claimed UID/auth_time JSON.
Existing device/session/revision/capture/claim/Stop validators remain mandatory.
"""
from ella.services.thin_cloudbot_consent import need
NATIVE_FIELDS={'receipt_id','decision','profile_binding_id','policy_version','processor_set_hash','scope_version','scope_hash','server_decided_at'}
def require_native_authority(service,uid,verified_auth_time,*,epoch_token,receipt):
    view=service.status(uid,verified_auth_time)
    need(view['authorized'],'thin_current_consent_required')
    need(type(epoch_token) is str and epoch_token==view['account_epoch_token'],'thin_account_epoch_mismatch')
    expected={k:view['receipt'][k] for k in NATIVE_FIELDS}
    need(type(receipt) is dict and set(receipt)==NATIVE_FIELDS and receipt==expected,'thin_native_receipt_mismatch')
    return {'consent_target':view['consent_target'],'owner_sha256':view['subject_owner_sha256'],
        'account_epoch_hash':view['account_epoch_hash'],'receipt_id':view['receipt']['receipt_id'],
        'receipt_sha256':view['current_state']['receipt_sha256'],'profile_binding_id':view['profile_binding_id']}
def revalidate_native_in_transaction(service,tx,uid,verified_auth_time,witness,*,now):
    return service.repository.assert_witness_in_transaction(tx,uid,verified_auth_time,witness,now)
