"""Finite internal prospective admission for one NEW native capture completion.

No endpoint, credential, consent, quarantine clear, backlog scan or activation on
import. The sole producer is the existing trusted first-stock transaction.
"""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

KEY = 'native_source_once'
TAG = 'native_first_source_admission'
CONTRACT = 'ella-native-first-completion-once-v5'
FIXTURE_CONTRACT = 'ella-native-fixture-playback-evidence-v3'
COLLECTION = 'ella_normal_native_source_admissions'
OWNER = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'
IDENTITY = ('job_id', 'uid', 'conversation_id', 'client_interaction_id', 'transcript_sha256')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest() if type(value) is str else None


def pin(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None


def uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def quarantine_image(tenant):
    return {k: tenant.get(k) for k in ('uid', 'quarantined_job_id', 'additional_quarantined_job_ids')}


def held_ids(tenant):
    primary = tenant.get('quarantined_job_id')
    other = tenant.get('additional_quarantined_job_ids', [])
    if type(primary) is not str or not primary or type(other) is not list or len(other) > 15:
        raise ValueError('native_once_hold_shape')
    ids = [primary] + other
    if any(type(j) is not str or not j for j in ids) or len(set(ids)) != len(ids):
        raise ValueError('native_once_hold_shape')
    return ids


def fixture_requirement_shape(value):
    """A new test epoch pins one fixture and one prospective verification ID."""
    return (type(value) is dict and set(value) == {
            'contract', 'verification_id', 'episode_id', 'fixture_audio_sha256', 'fixture_text_sha256',
            'expected_transcript_words_sha256','match_profile'}
            and value['contract'] == FIXTURE_CONTRACT
            and uuid(value['verification_id']) and uuid(value['episode_id'])
            and pin(value['fixture_audio_sha256']) and pin(value['fixture_text_sha256'])
            and pin(value['expected_transcript_words_sha256']) and fixture_profile_valid(value['match_profile']))

def fixture_profile_valid(profile):
    from database import fixture_anchored_match as matcher
    return (type(profile) is dict and set(profile) == {'contract','challenge'}
        and profile['contract'] == matcher.CONTRACT and matcher.challenge_shape(profile['challenge']))


def fixture_source_verified(transaction, db, uid, cid, doc, policy, now):
    """Verify FINAL decoded anchored facts here, inside the first-stock transaction.

    Root-owned immutable physical evidence already exists before Stop; it asserts
    only actual Ready/playback, never that unknown speech is fictional. The final transcript must satisfy the prospectively pinned closed anchored
    profile and fresh spoken challenge. The original script hash remains pinned.
    Original segments/timing/order are separately hashed without modification.
    No client flag, new endpoint, store write, retry or post-completion promotion.
    """
    from database import native_fixture_evidence as evidence
    try:
        requirement = policy[KEY]['fixture_requirement']
        if (not fixture_requirement_shape(requirement) or sha(uid) != OWNER
                or not source_native(doc, uid, cid) or not aware(now)):
            return None
        observed = evidence.read_evidence(policy['epoch'], requirement['verification_id'])
        proof = observed['body']
        keys = {'contract', 'verification_id', 'episode_id', 'fixture_audio_sha256', 'fixture_text_sha256',
                'match_profile', 'ready_conversation_id','ready_source_created_at','lineage_sha256','lineage_depth',
                'owner_ref_sha256', 'admission_epoch', 'conversation_id', 'capture_protocol_version',
                'capture_generation', 'capture_owner_sha256', 'source_created_at',
                'ready_observation_sha256', 'backend_join_sha256', 'ready_evidence_kind', 'ready_observed_at', 'playback_started_at', 'playback_finished_at',
                'published_at', 'expires_at', 'capture_lease_expires_at', 'hard_stop_utc', 'physical_evidence_sha256'}
        if (type(proof) is not dict or set(proof) != keys
                or any(proof.get(k) != v for k, v in requirement.items() if k != 'expected_transcript_words_sha256')
                or proof['owner_ref_sha256'] != OWNER or proof['admission_epoch'] != policy['epoch']
                or proof['conversation_id'] != cid or type(proof['capture_protocol_version']) is not int
                or proof['capture_protocol_version'] != 2 or proof['capture_generation'] != doc['capture_generation']
                or proof['capture_owner_sha256'] != sha(doc['capture_owner_token'])
                or not all(pin(proof[k]) for k in ('ready_observation_sha256', 'backend_join_sha256', 'physical_evidence_sha256'))
                or proof['ready_evidence_kind'] != 'main-owned-session-deviceRecord-transcriptServiceReady-v1'
                or not pin(proof['lineage_sha256']) or type(proof['lineage_depth']) is not int
                or not 1 <= proof['lineage_depth'] <= 8
                or not pin(observed['identity_sha256']) or proof['expires_at'] != str(policy['expires_at'])
                or not policy['source_completion_boundary'] < evidence.instant(proof['source_created_at'])
                    <= now
                or not policy['source_completion_boundary'] < evidence.instant(proof['ready_source_created_at'])
                    <= evidence.instant(proof['ready_observed_at']) <= evidence.instant(proof['playback_started_at'])
                    < evidence.instant(proof['playback_finished_at']) <= evidence.instant(proof['published_at'])
                    <= now < policy['expires_at']
                or type(observed.get('publication_time_ns')) is not int
                or not evidence.utc_ns(evidence.instant(proof['published_at'])) <= observed['publication_time_ns']
                    < min(evidence.utc_ns(evidence.instant(proof['expires_at'])),
                          evidence.utc_ns(evidence.instant(proof['capture_lease_expires_at'])),
                          evidence.utc_ns(evidence.instant(proof['hard_stop_utc'])))):
            return None
        from models.hermes_cloud_enrichment_contract import transcript_source
        from database.fixture_capture_lineage import verify as verify_lineage
        from database import fixture_anchored_match as matcher
        evidence.challenge_registered(db,transaction,policy)
        lineage = verify_lineage(db,transaction,uid,policy,cid,doc,proof['ready_conversation_id'],evidence.instant(proof['ready_observed_at']),now)
        if any(proof.get(k) != v for k,v in lineage.items()) or not nonempty_transcript(doc):
            return None
        match = matcher.assess(' '.join(row['text'] for row in doc['transcript_segments']),requirement['match_profile']['challenge'])
        if match.get('accepted') is not True:
            return None
        verification = {'contract': 'ella-final-native-fixture-verification-v1',
                        'physical_evidence_sha256': evidence.digest(proof),
                        'physical_identity_sha256': observed['identity_sha256'],
                        'transcript_sha256': transcript_source(doc)[1],
                        'expected_transcript_words_sha256': requirement['expected_transcript_words_sha256'],
                        'anchored_match_sha256': evidence.digest(match), 'match_contract': matcher.CONTRACT,
                        'comparison_contract': evidence.COMPARISON}
        return {'verification_sha256': evidence.digest(verification),
                'evidence_identity_sha256': observed['identity_sha256'],
                'source_created_at': proof['source_created_at']}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


def scope_shape(policy):
    try:
        s = policy[KEY]
        return (type(s) is dict and set(s) == {'contract', 'owner_ref_sha256', 'quarantine_sha256', 'held_job_preimages', 'fixture_requirement'}
                and s['contract'] == CONTRACT and s['owner_ref_sha256'] == OWNER
                and fixture_requirement_shape(s['fixture_requirement'])
                and policy.get('owner_scope_sha256') == [OWNER]
                and 'one_source_qualification' not in policy
                and aware(policy['source_completion_boundary']) and aware(policy['expires_at'])
                and policy['expires_at'] - policy['source_completion_boundary'] == timedelta(seconds=3600)
                and uuid(policy['epoch']) and pin(s['quarantine_sha256'])
                and type(s['held_job_preimages']) is dict and 1 <= len(s['held_job_preimages']) <= 16
                and all(type(j) is str and j and pin(h) for j, h in s['held_job_preimages'].items())
                and all(type(policy['quotas'].get(k)) is int and policy['quotas'][k] == 1
                        for k in ('max_concurrency', 'max_claims_per_hour', 'max_tenant_claims_per_hour', 'max_attempts')))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def snapshot_holds(transaction, db, uid, tenant):
    """Bounded exact references, metadata hashed in place, no old source read."""
    if sha(uid) != OWNER or tenant.get('uid') != uid:
        raise ValueError('native_once_owner')
    from database import normal_enrichment_activation as activation
    result = {}
    for jid in held_ids(tenant):
        snap = db.collection(activation.JOBS).document(jid).get(transaction=transaction, timeout=5, retry=None)
        row = snap.to_dict() if snap.exists else {}
        if (not snap.exists or row.get('uid') != uid or row.get('job_id') != jid
                or row.get('status') != 'quarantined' or row.get('dispatch_state') != 'unknown_quarantined'
                or type(row.get('attempt_count')) is not int or row['attempt_count'] != 1
                or row.get('lease_token') is not None or row.get('lease_expires_at') is not None):
            raise ValueError('native_once_old_hold_changed')
        result[jid] = digest(row)
    return result


def holds_match(transaction, db, uid, policy, tenant):
    if not scope_shape(policy) or sha(uid) != OWNER or uid not in policy.get('quarantined_tenants', []):
        return False
    try:
        return (digest(quarantine_image(tenant)) == policy[KEY]['quarantine_sha256']
                and snapshot_holds(transaction, db, uid, tenant) == policy[KEY]['held_job_preimages'])
    except ValueError:
        return False


def source_native(doc, uid, cid):
    """Native v2 provenance is server-stamped, never caller headers/fixtures."""
    external = doc.get('external_data')
    return (type(doc) is dict and doc.get('id') == cid and doc.get('uid', uid) == uid
            and doc.get('source') == 'omi' and doc.get('discarded') is not True
            and type(doc.get('capture_protocol_version')) is int and doc['capture_protocol_version'] == 2
            and uuid(doc.get('capture_generation'))
            and type(doc.get('capture_owner_token')) is str and bool(doc['capture_owner_token'])
            and doc.get('thin_cloudbot_source_binding') is None
            and not (isinstance(external, dict) and 'qualification_transcript' in external))


def nonempty_transcript(doc):
    """Actual decoded speech, not a stock-summary/empty-hash success label."""
    if type(doc) is not dict or type(doc.get('transcript_segments')) is not list:
        return False
    return any(type(segment) is dict and type(segment.get('text')) is str
               and bool(segment['text'].strip()) for segment in doc['transcript_segments'])


def admission(transaction, db, payload, policy, tenant, now, transition):
    from database.normal_future_source_disposition import TerminalTransition
    if (type(transition) is not TerminalTransition or not holds_match(transaction, db, payload.get('uid'), policy, tenant)
            or tenant.get('active_job_id') is not None or not aware(now)
            or not policy['source_completion_boundary'] <= now < policy['expires_at']
            or transition.uid != payload.get('uid') or transition.conversation_id != payload.get('conversation_id')
            or transition.first_completed_at != now or not pin(transition.binding_sha256)
            or not all(aware(t) for t in (transition.source_created_at, transition.prior_updated_at))
            or not policy['source_completion_boundary'] < transition.source_created_at <= transition.prior_updated_at <= now
            or any(type(payload.get(k)) is not str or not payload[k] for k in IDENTITY)):
        return None
    ref = db.collection(COLLECTION).document(policy['epoch'])
    if ref.get(transaction=transaction, timeout=5, retry=None).exists:
        return None
    source_ref = db.collection('users').document(payload['uid']).collection('conversations').document(payload['conversation_id'])
    snap = source_ref.get(transaction=transaction, timeout=5, retry=None)
    prior = snap.to_dict() if snap.exists else {}
    if (not snap.exists or snap.create_time != transition.source_created_at or snap.update_time != transition.prior_updated_at
            or not source_native(prior, payload['uid'], payload['conversation_id'])
            or prior.get('status') != 'processing' or prior.get('active_summary_version_id')
            or prior.get('normal_source_completed_at') or prior.get('capture_state') != 'finalizing'
            or type(prior.get('capture_finalization_claim_token')) is not str or not prior['capture_finalization_claim_token']
            or not aware(prior.get('capture_finalization_lease_expires_at'))
            or prior['capture_finalization_lease_expires_at'] <= now):
        return None
    from database.conversations import _prepare_conversation_for_read
    from models.hermes_cloud_enrichment_contract import transcript_source
    readable = _prepare_conversation_for_read(prior, payload['uid'])
    if not nonempty_transcript(readable) or transcript_source(readable)[1] != payload['transcript_sha256']:
        return None
    verified = fixture_source_verified(transaction, db, payload['uid'], payload['conversation_id'], readable, policy, now)
    if not verified or verified['source_created_at'] != str(transition.source_created_at):
        return None
    tag = {'contract': CONTRACT, 'epoch': policy['epoch'], 'source_created_at': transition.source_created_at,
           'first_completed_at': now, 'binding_sha256': transition.binding_sha256,
           'capture_generation': prior['capture_generation'], 'capture_owner_sha256': sha(prior['capture_owner_token']),
           'fixture_verification_sha256': verified['verification_sha256'],
           'fixture_evidence_identity_sha256': verified['evidence_identity_sha256']}
    record = {k: payload[k] for k in IDENTITY}
    record.update(contract=CONTRACT, state='prepared', tag=tag, expires_at=policy['expires_at'])
    return tag, ref, record


def eligible(transaction, db, current, policy, tenant, now, *, consumed=False, admission=False):
    """One exact new source; source binding and all old holds rechecked at effect."""
    if admission:
        return None  # Only trusted producer admission() can mint the singleton.
    from database.normal_future_source_disposition import binding
    from database.conversations import _prepare_conversation_for_read
    try:
        uid, cid = current['uid'], current['conversation_id']
        tag = current[TAG]
        if (not holds_match(transaction, db, uid, policy, tenant) or type(tag) is not dict
                or set(tag) != {'contract','epoch','source_created_at','first_completed_at','binding_sha256','capture_generation','capture_owner_sha256','fixture_verification_sha256','fixture_evidence_identity_sha256'}
                or tag['contract'] != CONTRACT or tag['epoch'] != policy['epoch']
                or not aware(now) or not policy['source_completion_boundary'] <= now < policy['expires_at']
                or current.get('admission_epoch') != policy['epoch']
                or not all(aware(tag[k]) for k in ('source_created_at','first_completed_at'))
                or not policy['source_completion_boundary'] < tag['source_created_at'] <= tag['first_completed_at'] <= now
                or current.get('source_completed_at') != tag['first_completed_at']
                or tenant.get('active_job_id') != (current.get('job_id') if consumed else None)
                or type(current.get('attempt_count')) is not int or current['attempt_count'] != (1 if consumed else 0)):
            return None
        ref = db.collection(COLLECTION).document(policy['epoch'])
        snap = ref.get(transaction=transaction, timeout=5, retry=None)
        record = snap.to_dict() if snap.exists else {}
        if (record.get('state') != ('consumed' if consumed else 'prepared') or record.get('contract') != CONTRACT
                or record.get('tag') != tag or record.get('expires_at') != policy['expires_at']
                or any(type(current.get(k)) is not str or not current[k] or record.get(k) != current[k] for k in IDENTITY)
                or (consumed and (not current.get('lease_token') or record.get('lease_token') != current['lease_token']))):
            return None
        src = db.collection('users').document(uid).collection('conversations').document(cid).get(transaction=transaction, timeout=5, retry=None)
        doc = _prepare_conversation_for_read(src.to_dict(), uid) if src.exists else {}
        b = binding(doc, uid, cid)
        if (not src.exists or src.create_time != tag['source_created_at'] or not source_native(doc, uid, cid)
                or doc.get('capture_generation') != tag['capture_generation']
                or sha(doc.get('capture_owner_token')) != tag['capture_owner_sha256']
                or doc.get('capture_state') not in ('finalizing','drained')
                or doc.get('normal_source_completed_at') != tag['first_completed_at']
                or not nonempty_transcript(doc) or not b or digest(b) != tag['binding_sha256']
                or b.get('transcript_sha256') != current['transcript_sha256']
                or b.get('client_interaction_id') != current['client_interaction_id']):
            return None
        verified = fixture_source_verified(transaction, db, uid, cid, doc, policy, now)
        if (not verified or verified['source_created_at'] != str(tag['source_created_at'])
                or verified['verification_sha256'] != tag['fixture_verification_sha256']
                or verified['evidence_identity_sha256'] != tag['fixture_evidence_identity_sha256']):
            return None
        return ref
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
