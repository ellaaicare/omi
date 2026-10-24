"""Server composition adapter, no HTTP surface, model selector or Hermes lookup.
Calls a prebound trusted backend transaction callback only after exact Thin
owner/epoch/receipt proof is rechecked inside THAT same Firestore transaction.
Not wired to dormant QA operator/capture/output routes by this source packet.
"""
from dataclasses import dataclass
from ella.services.thin_cloudbot_consent import ThinConsentHold,sha
@dataclass(frozen=True)
class MuseAdmissionAdapter:
    service:object
    expected_owner_sha256:str
    def prepare_witness(self,uid,verified_auth_time):
        if sha(uid)!=self.expected_owner_sha256:raise ThinConsentHold('thin_operator_owner_mismatch')
        return self.service.require_current(uid,verified_auth_time)
    def apply_in_transaction(self,tx,uid,verified_auth_time,witness,*,now,prebound_effect):
        if sha(uid)!=self.expected_owner_sha256:raise ThinConsentHold('thin_operator_owner_mismatch')
        self.service.repository.assert_witness_in_transaction(tx,uid,verified_auth_time,witness,now)
        # Callback is static server composition, never request/model/code input.
        return prebound_effect(tx)
