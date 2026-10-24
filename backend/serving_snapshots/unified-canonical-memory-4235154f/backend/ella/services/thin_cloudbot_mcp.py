"""Exact prebound Thin Muse pickup/save. No source discovery or Hermes resolution.

The authenticated Muse principal supplies a previously server-bound source/run/op
selection. Its owner must be verified by the existing MCP/owner auth layer; no
HTTP body UID or scope grants authority. Dedicated receipt is re-read in each CAS.
"""
from datetime import datetime, timezone
from ella.services.thin_cloudbot_consent import need
from ella.services.thin_cloudbot_capture import FIELD, source_ref, run_transaction


class ThinMcpDatabase:
    def __init__(self, db, authority, *, source_id, run_id, operation_id):
        self.db, self.authority = db, authority
        self.source_id, self.run_id, self.operation_id = source_id, run_id, operation_id

    def read_in_transaction(self, tx):
        from database.conversations import _prepare_conversation_for_read
        from ella.services import mcp_synthetic_pending as pending
        now = datetime.now(timezone.utc)
        self.authority.revalidate_in_transaction(tx, now=now)
        ref = source_ref(self.db, self.authority.uid, self.source_id)
        snap = ref.get(transaction=tx)
        need(snap.exists, 'thin_muse_source_missing')
        doc = _prepare_conversation_for_read(snap.to_dict(), self.authority.uid)
        binding = doc.get(FIELD)
        need(type(binding) is dict and binding.get('authority') == self.authority.witness
             and binding.get('consent_scope') == self.authority.scope
             and binding.get('source_id') == self.source_id, 'thin_muse_source_target_changed')
        marker = pending.validate(doc.get(pending.FIELD), self.authority.uid, self.source_id)
        need(marker['run_id'] == self.run_id and marker['operation_id'] == self.operation_id,
             'thin_muse_prebound_operation_changed')
        return ref, doc, marker

    def pickup(self):
        def read(tx):
            from ella.services import mcp_synthetic_pending as pending, mcp_summary_enrichment as summary
            from ella.services.mcp_source_report import transcript_projection
            ref, doc, marker = self.read_in_transaction(tx)
            need(pending.project(marker, self.authority.uid, self.source_id,
                                 datetime.now(timezone.utc))['available'], 'thin_muse_pending_closed')
            need(marker['expected_snapshot_sha256'] == summary._snapshot_hash(self.authority.uid, self.source_id, doc)
                 and marker['source_sha256'] == transcript_projection(doc)['sha256'],
                 'thin_muse_snapshot_changed')
            return {'conversation': doc, 'pending': marker}
        return run_transaction(self.db, read)

    def update_conversation_with_builder(self, uid, cid, builder):
        need(uid == self.authority.uid and cid == self.source_id, 'thin_muse_foreign_builder')
        def effect(tx):
            from database.conversations import _prepare_conversation_for_write
            ref, doc, marker = self.read_in_transaction(tx)
            user = self.db.collection('users').document(uid).get(transaction=tx)
            level = doc.get('data_protection_level')
            need(level in {'standard', 'enhanced'}, 'thin_muse_protection_unknown')
            update, result = builder(doc)
            # Existing builder owns report/verifier/one-use receipt/version rules.
            if update:
                tx.set(ref, _prepare_conversation_for_write({**doc, **update}, uid, level))
            return {'result': result}
        return run_transaction(self.db, effect)


def enrich_summary(db, authority, arguments, category_validator, *, source_id, run_id, operation_id):
    from ella.services import mcp_summary_enrichment as summary
    need(arguments.get('conversation_id') == source_id
         and (arguments.get('enrichment') or {}).get('operation_id') == operation_id,
         'thin_muse_request_not_prebound')
    adapter = ThinMcpDatabase(db, authority, source_id=source_id, run_id=run_id, operation_id=operation_id)
    return summary.enrich_summary(adapter, authority.uid, arguments, category_validator, _thin_authority=authority)


def execute_tool(db, authority, name, arguments, category_validator):
    """Only exact-ID source read/save; never broad legacy memory/queue tools."""
    from uuid import UUID
    from ella.services import mcp_synthetic_pending as pending, mcp_summary_enrichment as summary
    from ella.services.mcp_source_report import transcript_projection
    from ella.services.mcp_current_conversation import read_current_conversation
    need(name in {'get_current_conversation_by_id', 'get_conversation_by_id', 'update_conversation_summary'},
         'thin_mcp_tool_not_supported')
    need(type(arguments) is dict and type(arguments.get('conversation_id')) is str,
         'thin_mcp_exact_source_required')
    cid = arguments['conversation_id']
    try:
        need(str(UUID(cid)) == cid, 'thin_mcp_exact_source_required')
    except (ValueError, TypeError):
        need(False, 'thin_mcp_exact_source_required')
    # Read only this source's server pending binding; never latest/history discovery.
    authority.revalidate()
    snap = source_ref(db, authority.uid, cid).get()
    raw = snap.to_dict() if snap.exists else {}
    marker = pending.validate(raw.get(pending.FIELD), authority.uid, cid)
    adapter = ThinMcpDatabase(db, authority, source_id=cid,
        run_id=marker['run_id'], operation_id=marker['operation_id'])
    if name == 'update_conversation_summary':
        need('queue_claim' not in arguments and 'enrichment' in arguments,
             'thin_mcp_safe_enrichment_required')
        return summary.enrich_summary(adapter, authority.uid, arguments, category_validator,
                                      _thin_authority=authority)
    class Reader:
        _thin_authority = authority
        def get_conversation(self, uid, source_id):
            need(uid == authority.uid and source_id == cid, 'thin_mcp_foreign_source')
            return run_transaction(db, lambda tx: adapter.read_in_transaction(tx)[1])
    if name == 'get_current_conversation_by_id':
        return read_current_conversation(Reader(), authority.uid, arguments)
    need(set(arguments) <= {'conversation_id', 'include_enrichment_preconditions', 'enrichment_contract'}
         and arguments.get('include_enrichment_preconditions') is True
         and arguments.get('enrichment_contract') == summary.CONTRACT_V2,
         'thin_mcp_exact_read_contract_required')
    item = adapter.pickup()
    doc = item['conversation']
    return {'conversation': {key:value for key,value in doc.items() if key != FIELD},
            'enrichment_preconditions': summary.preconditions(authority.uid, cid, doc, contract=summary.CONTRACT_V2),
            'source_transcript': transcript_projection(doc), 'pending_synthetic_run': pending.project(item['pending'], authority.uid, cid, datetime.now(timezone.utc))}
