"""Prospective normal-product native capture provenance: OMI and Fieldy only.

Fixture OMI-only helpers remain unchanged. This module never accepts a request
body as terminal authority and performs no activation, replay or import I/O.
"""
from database import normal_native_source_once as native
from database.normal_future_source_disposition import (
    TerminalTransition, SEMANTIC, digest, aware,
    terminal_transition as omi_terminal_transition,
)

SUPPORTED_SOURCES = frozenset({'omi', 'fieldy'})


def source_native(doc, uid, cid):
    """Same native-v2 server tuple, plus bounded normal-product source family.

    These predicates run on the existing UID-owned stored snapshot, not caller
    parameters. The first-completion transaction and callback CAS remain required.
    """
    if type(doc) is not dict:
        return False
    external = doc.get('external_data')
    return (doc.get('id') == cid and doc.get('uid', uid) == uid
        and type(doc.get('source')) is str and doc.get('source') in SUPPORTED_SOURCES
        and (doc.get('source') != 'fieldy' or doc.get('uid') == uid)
        and doc.get('discarded') is not True
        and type(doc.get('capture_protocol_version')) is int
        and doc['capture_protocol_version'] == 2
        and native.uuid(doc.get('capture_generation'))
        and type(doc.get('capture_owner_token')) is str and bool(doc['capture_owner_token'])
        and doc.get('thin_cloudbot_source_binding') is None
        and not (isinstance(external, dict) and 'qualification_transcript' in external))


def binding(doc, uid, cid):
    from models.hermes_cloud_enrichment_contract import build_enrichment_identity
    from ella.services.retained_completion_enrichment import source_pin
    if (not source_native(doc, uid, cid) or doc.get('status') != 'completed'
        or doc.get('discarded') or not doc.get('active_summary_version_id')
        or not native.nonempty_transcript(doc) or any(doc.get(k) for k in SEMANTIC)):
        return None
    identity = build_enrichment_identity(uid=uid, conversation_id=cid, conversation=doc)
    pin = source_pin(doc, uid, cid)
    return {'source_family': doc['source'], 'source_sha256': pin[0], 'source_version': pin[1],
        'stock_summary_version_sha256': digest(pin[2]),
        'transcript_sha256': identity.transcript_sha256,
        'client_interaction_id': identity.client_interaction_id}


def terminal_transition(snapshot, uid, prior, completed, now):
    # Family continuity is checked before either first-completion branch.
    if (type(completed) is not dict or type(prior) is not dict
        or type(prior.get('source')) is not str
        or prior.get('source') != completed.get('source')):
        return None
    if completed.get('source') == 'omi':
        original = omi_terminal_transition(snapshot, uid, prior, completed, now)
        if original is None:
            return None
        b = binding(completed, uid, completed.get('id'))
        if b is None:
            return None
        return TerminalTransition(original.uid, original.conversation_id,
            original.source_created_at, original.prior_updated_at,
            original.first_completed_at, digest(b), b)
    from ella.services.normal_account_routing import admitted_subject
    if type(completed) is not dict or type(prior) is not dict:
        return None
    cid = completed.get('id')
    b = binding(completed, uid, cid)
    created, updated = snapshot.create_time, snapshot.update_time
    if (completed.get('source') != 'fieldy' or not source_native(prior, uid, cid)
        or not snapshot.exists or snapshot.id != cid or not admitted_subject(uid)
        or prior.get('status') == 'completed' or prior.get('active_summary_version_id')
        or prior.get('normal_source_completed_at') or not b
        or not all(aware(t) for t in (created, updated, now))
        or not created <= updated <= now or completed.get('normal_source_completed_at') != now
        or prior.get('capture_generation') != completed.get('capture_generation')
        or prior.get('capture_owner_token') != completed.get('capture_owner_token')):
        return None
    return TerminalTransition(uid, cid, created, updated, now, digest(b), b)


def require_initial_source(uid, cid, doc):
    """Select normal lifecycle provenance without changing fixture validation."""
    from database import normal_future_source_disposition as future
    job = future._worker_job.get()
    if job is None or 'normal_account_lifecycle_admission' not in job:
        return future.require_initial_source(uid, cid, doc)
    from ella.services.runtime_errors import ProvisioningError
    tag = job.get('normal_account_lifecycle_admission')
    b = binding(doc, uid, cid)
    if (type(tag) is not dict or uid != job.get('uid')
        or cid != job.get('conversation_id') or not b
        or digest(b) != tag.get('binding_sha256')
        or doc.get('normal_source_completed_at') != tag.get('first_completed_at')):
        raise ProvisioningError('retained_completion_rejected', retryable=False)
