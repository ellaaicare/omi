"""Internal delivery of already-generated cloudbot output. No TTS/model/fallback.
Not an exposed MCP tool or HTTP route; existing separately authenticated owner
composition supplies RequestAuthority and exact device/session/epoch tuple.
"""
from starlette.concurrency import run_in_threadpool
from ella.services.thin_cloudbot_consent import need
from ella.services.whisper_delivery import PostgresStore
class ThinOutputPublisher:
    def __init__(self,pool,authority):
        need(authority is not None and authority.scope['consent_target']=='thin-cloudbot','thin_output_authority_required')
        self.store=PostgresStore(pool);self.authority=authority
    async def _check(self):
        await run_in_threadpool(self.authority.revalidate)
        from ella.services.thin_delivery_mode import require_enabled
        await run_in_threadpool(require_enabled,self.authority._service,self.authority.uid,self.authority.scope)
        return self.authority.scope
    async def operation(self,operation,*,device_id,session_id,revision,client_epoch,fields):
        need(operation in {'origin','validate_origin','enqueue'},'thin_output_operation_invalid')
        need(type(fields) is dict and 'client_epoch' not in fields,'thin_output_fields_invalid')
        # Fresh recheck before lookup and under the durable row lock before effect.
        await self._check()
        return await self.store.operation(operation,self.authority.uid,device_id,session_id,revision,
            {**fields,'client_epoch':client_epoch},consent_scope=self.authority.scope,authority_check=self._check)
