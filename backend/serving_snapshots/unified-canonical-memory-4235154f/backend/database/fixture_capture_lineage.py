"""Prospective private test lineage; never capture or consent authority.

Called inside the existing successful authority-install transaction, before any
writes. No new producer, discovery, endpoint, credential or replacement grant.
"""
from datetime import datetime

FIELD = 'ella_test_fixture_capture_lineage'
CONTRACT = 'ella-test-fixture-capture-lineage-v1'
MAX_HOPS = 8
FIELDS = {'contract','epoch','root_cid','conversation_id','generation','owner_sha256',
          'parent_cid','parent_sha256','source_created_at','installed_at'}

def node_valid(node, *, policy, uid, cid, generation, owner_sha256, created_at):
    from database import normal_native_source_once as native
    from database import native_fixture_evidence as evidence
    return bool(type(node) is dict and set(node) == FIELDS and node['contract'] == CONTRACT
        and native.sha(uid) == native.OWNER and node['epoch'] == policy['epoch']
        and node['conversation_id'] == cid and evidence.uuid(cid) and evidence.uuid(node['root_cid'])
        and node['generation'] == generation and evidence.uuid(generation)
        and node['owner_sha256'] == owner_sha256 and evidence.pin(owner_sha256)
        and node['source_created_at'] == str(created_at)
        and native.aware(created_at) and policy['source_completion_boundary'] < created_at
        and created_at <= evidence.instant(node['installed_at']) < policy['expires_at']
        and ((node['parent_cid'] is None and node['parent_sha256'] is None and node['root_cid'] == cid)
             or (evidence.uuid(node['parent_cid']) and node['parent_cid'] != cid
                 and evidence.pin(node['parent_sha256']) and node['root_cid'] != cid)))

def plan_install(transaction, authority_ref, conversation_ref, conversation_snapshot,
                 conversation, conversation_id, generation, owner_token, now,
                 predecessor_ref, predecessor):
    """Return metadata only after caller's ORIGINAL install checks have passed.

    All reads precede caller's first buffered write. No active v5 test scope -> no
    new metadata. Missing/invalid ancestry -> no stamp, never a capture override.
    """
    from database import normal_native_source_once as native
    from database import normal_enrichment_activation as activation
    from database import native_fixture_evidence as evidence
    uid = conversation_ref.parent.parent.id
    if native.sha(uid) != native.OWNER or authority_ref.id != 'current':
        return None
    client = conversation_ref._client
    snap = activation.reference(client).get(transaction=transaction,timeout=5,retry=None)
    policy = snap.to_dict() if snap.exists else {}
    if (not native.scope_shape(policy) or not activation.valid_policy(policy,now)
            or policy.get(native.KEY,{}).get('contract') != native.CONTRACT
            or not native.aware(now) or not policy['source_completion_boundary'] <= now < policy['expires_at']
            or not conversation_snapshot.exists or conversation.get(FIELD) is not None):
        return None
    created_at = conversation_snapshot.create_time
    if not native.aware(created_at) or not policy['source_completion_boundary'] < created_at <= now:
        return None
    node = {'contract':CONTRACT,'epoch':policy['epoch'],'root_cid':conversation_id,
        'conversation_id':conversation_id,'generation':generation,'owner_sha256':native.sha(owner_token),
        'parent_cid':None,'parent_sha256':None,'source_created_at':str(created_at),'installed_at':str(now)}
    if predecessor_ref is not None:
        if type(predecessor) is not dict or not native.source_native(predecessor,uid,predecessor_ref.id):
            return None
        parent_snapshot = predecessor_ref.get(transaction=transaction,timeout=5,retry=None)
        parent = (parent_snapshot.to_dict() or {}).get(FIELD)
        if (predecessor.get('capture_generation') != generation
                or predecessor.get('capture_owner_token') != owner_token
                or not node_valid(parent,policy=policy,uid=uid,cid=predecessor_ref.id,
                    generation=generation,owner_sha256=native.sha(owner_token),created_at=parent_snapshot.create_time)
                or evidence.instant(parent['installed_at']) > created_at):
            return None
        node.update(root_cid=parent['root_cid'],parent_cid=predecessor_ref.id,parent_sha256=evidence.digest(parent))
    return node if node_valid(node,policy=policy,uid=uid,cid=conversation_id,generation=generation,
        owner_sha256=native.sha(owner_token),created_at=created_at) else None

def ancestry_source(doc, uid, cid, *, leaf):
    """Lifecycle eligibility of the selected source is NOT ancestry identity.

    Strict nondiscarded leaf is unchanged. Only a terminal, completed, strictly
    decoded-empty discarded ancestor without a saved/completed summary may be
    used as an identity link. This function never changes or returns source data.
    """
    from database import normal_native_source_once as native
    if native.source_native(doc,uid,cid):
        return True
    if (leaf or type(doc) is not dict or doc.get('discarded') is not True
            or doc.get('status') != 'completed' or doc.get('capture_state') != 'terminal'
            or doc.get('active_summary_version_id') is not None
            or doc.get('normal_source_completed_at') is not None):
        return False
    # Reuse ALL existing provenance/owner/target clauses; exclude only the
    # lifecycle-discard eligibility bit for this historical ancestry check.
    identity = dict(doc)
    identity.pop('discarded',None)
    if not native.source_native(identity,uid,cid):
        return False
    from database.conversations import _prepare_conversation_for_transcript_hash
    readable = _prepare_conversation_for_transcript_hash(doc,uid)
    return (type(readable) is dict and type(readable.get('transcript_segments')) is list
            and readable['transcript_segments'] == [])


def verify(db, transaction, uid, policy, leaf_cid, leaf_doc, ready_cid, ready_at, now):
    """Exact ancestry, not a same-generation guess. Reads bind every durable hop."""
    from database import normal_native_source_once as native
    from database import native_fixture_evidence as evidence
    need = evidence.need
    generation = leaf_doc['capture_generation']
    owner_sha256 = native.sha(leaf_doc['capture_owner_token'])
    nodes, seen, cid, child = [], set(), leaf_cid, None
    ready_created = None
    for _ in range(MAX_HOPS):
        need(evidence.uuid(cid) and cid not in seen,'fixture_lineage_cycle_or_id')
        seen.add(cid)
        snap = db.collection('users').document(uid).collection('conversations').document(cid).get(
            transaction=transaction,timeout=5,retry=None)
        doc = snap.to_dict() if snap.exists else {}
        node = doc.get(FIELD)
        need(native.aware(now) and snap.exists and ancestry_source(doc,uid,cid,leaf=(cid == leaf_cid))
            and doc['capture_generation'] == generation and native.sha(doc['capture_owner_token']) == owner_sha256
            and node_valid(node,policy=policy,uid=uid,cid=cid,generation=generation,
                owner_sha256=owner_sha256,created_at=snap.create_time),'fixture_lineage_missing_or_foreign')
        need(evidence.instant(node['installed_at']) <= now,'fixture_lineage_future_install')
        need(child is None or (child['parent_cid'] == cid and child['parent_sha256'] == evidence.digest(node)
            and child['root_cid'] == node['root_cid']
            and evidence.instant(node['installed_at']) <= evidence.instant(child['source_created_at'])),
            'fixture_lineage_replaced_or_unordered')
        if cid == ready_cid:
            need(evidence.instant(node['installed_at']) <= ready_at,'fixture_ready_before_install')
            ready_created = str(snap.create_time)
        nodes.append(node)
        if node['parent_cid'] is None:
            need(ready_created is not None,'fixture_ready_not_ancestor')
            return {'lineage_sha256':evidence.digest(list(reversed(nodes))),
                'ready_source_created_at':ready_created,'ready_conversation_id':ready_cid,'lineage_depth':len(nodes)}
        child, cid = node, node['parent_cid']
    raise evidence.FixtureHold('fixture_lineage_depth_exceeded')
