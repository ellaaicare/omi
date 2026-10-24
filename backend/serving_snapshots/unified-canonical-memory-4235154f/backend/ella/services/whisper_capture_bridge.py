"""Internal opt-in dispatcher seam; no public caller may supply an owner verdict.
Bind at authenticated capture handshake; admit immediately before NEW async work.
The captured origin must be propagated unchanged to the existing enqueue endpoint.
"""
from .whisper_delivery import DeliveryConflict,capture_identity

class CaptureDeliveryBridge:
    def __init__(self,store,owner_check,consent_check,mode_check):
        self.store=store;self.owner_check=owner_check;self.consent_check=consent_check;self.mode_check=mode_check
    async def _fresh(self,uid,cid,generation,token):
        if not await self.owner_check(uid,cid,generation,token):raise DeliveryConflict('delivery_capture_owner_stale')
        # Consent follows the potentially blocking owner lookup.
        await self.consent_check(uid)
        if not await self.mode_check(uid):raise DeliveryConflict('delivery_mode_inactive')
    async def bind(self,*,uid,device_id,session_id,revision,client_epoch,conversation_id,generation,token):
        await self._fresh(uid,conversation_id,generation,token)
        result=await self.store.bind_capture(uid,device_id,session_id,revision,client_epoch,capture_identity(uid,conversation_id,generation,token))
        await self._fresh(uid,conversation_id,generation,token)
        return result
    async def handoff(self,*,uid,device_id,client_epoch,previous_key,conversation_id,generation,token):
        await self._fresh(uid,conversation_id,generation,token)
        result=await self.store.handoff_capture(uid,device_id,client_epoch,previous_key,capture_identity(uid,conversation_id,generation,token))
        await self._fresh(uid,conversation_id,generation,token)
        return result
    async def admit(self,*,uid,device_id,client_epoch,conversation_id,generation,token,request_id):
        await self._fresh(uid,conversation_id,generation,token)
        origin=await self.store.origin_for_capture(uid,device_id,client_epoch,capture_identity(uid,conversation_id,generation,token),request_id)
        await self._fresh(uid,conversation_id,generation,token)
        return origin

def enqueue_scope(origin):
    # Do not resolve a fresh pointer at enqueue: that would resurrect canceled work.
    return {k:origin[k] for k in ('device_id','session_id','client_epoch','revision','origin_id')}
