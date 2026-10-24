"""Private root-owned test evidence. No endpoint, activation, model or import I/O.

Physical observations are supplied only through the separately reviewed retained
operator, never client flags/headers. The native stock transaction independently
checks the final full transcript against the prospectively pinned whole-text hash.
"""
import hashlib
import json
import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from uuid import UUID

ROOT = '/var/lib/ella-native-fixture-evidence'
CONTRACT = 'ella-native-fixture-playback-evidence-v3'
COMPARISON = 'ella-maple-workshop-anchored-asr-v1'
MAX_BYTES = 32768
class FixtureHold(ValueError):
    pass

def need(ok, code):
    if not ok:
        raise FixtureHold(code)

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()

def pin(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

def uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False

def instant(value):
    need(type(value) is str and len(value) <= 40, 'fixture_time_invalid')
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        raise FixtureHold('fixture_time_invalid') from None
    need(out.tzinfo is not None and out.utcoffset() is not None, 'fixture_time_invalid')
    return out

def utc_ns(value):
    need(isinstance(value, datetime) and value.tzinfo is not None, 'fixture_time_invalid')
    delta = value.astimezone(timezone.utc) - datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000000 + delta.microseconds * 1000

def words_sha256(text):
    """Comparison only: no source/segment/timing rewrite. Full word sequence."""
    need(type(text) is str and 0 < len(text.encode('utf-8')) <= 16000, 'fixture_text_bound')
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)*", unicodedata.normalize('NFC', text).casefold())
    need(0 < len(words) <= 2048, 'fixture_words_bound')
    return digest(words)

def decoded_words_sha256(doc):
    rows = doc.get('transcript_segments')
    need(type(rows) is list and 0 < len(rows) <= 2048, 'fixture_segments_invalid')
    need(all(type(v) is dict and type(v.get('text')) is str for v in rows), 'fixture_segments_invalid')
    return words_sha256(' '.join(v['text'] for v in rows))

def fixture_manifest(audio_sha256, text, match_profile=None):
    from database import fixture_anchored_match as matcher
    need(pin(audio_sha256) and type(match_profile) is dict
         and set(match_profile) == {'contract','challenge'}
         and match_profile['contract'] == matcher.CONTRACT
         and matcher.challenge_shape(match_profile['challenge'])
         and text == matcher.text_for(match_profile['challenge']), 'fixture_audio_text_or_profile_invalid')
    return {'contract':'ella-fictional-fixture-text-v2','audio_sha256':audio_sha256,
        'text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'words_sha256':words_sha256(text),'match_profile':match_profile}

def challenge_registered(db, transaction, policy):
    req = policy['native_source_once']['fixture_requirement']
    profile = req['match_profile']
    snap = db.collection('ella_fixture_spoken_challenges').document(digest(profile['challenge'])).get(
        transaction=transaction,timeout=5,retry=None)
    expected = {'contract':'ella-fixture-spoken-challenge-once-v1','epoch':policy['epoch'],
        'profile_sha256':digest(profile),'fixture_audio_sha256':req['fixture_audio_sha256'],
        'registered_at':policy['source_completion_boundary']}
    need(snap.exists and snap.to_dict() == expected,'fixture_challenge_not_prospectively_registered')


def unique_pairs(items):
    value = {}
    for key, item in items:
        need(key not in value, 'fixture_duplicate_key')
        value[key] = item
    return value

def _checked_dir(fd, *, private=False):
    s = os.fstat(fd)
    need(stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
         'fixture_directory_unsafe')
    if private:
        need(stat.S_IMODE(s.st_mode) == 0o700, 'fixture_directory_not_private')

def _parent(epoch, *, create=False):
    need(uuid(epoch), 'fixture_epoch_invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, part in enumerate(['var', 'lib', 'ella-native-fixture-evidence', epoch]):
            _checked_dir(fd)
            if create and index >= 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _checked_dir(fd, private=index >= 2)
        return fd
    except BaseException:
        os.close(fd)
        raise

def _stable(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

def _identity(s, raw):
    return digest({'device': s.st_dev, 'inode': s.st_ino, 'ctime_ns': s.st_ctime_ns,
                   'mtime_ns': s.st_mtime_ns, 'size': s.st_size,
                   'sha256': hashlib.sha256(raw).hexdigest()})

def read_evidence(epoch, verification_id):
    """Bounded nofollow exact path, root-only immutable file; never creates."""
    need(uuid(verification_id), 'fixture_verification_id_invalid')
    parent = _parent(epoch)
    fd = None
    try:
        fd = os.open(verification_id + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
             and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= MAX_BYTES,
             'fixture_file_unsafe')
        raw = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        need(_stable(before) == _stable(after) and len(raw) == before.st_size, 'fixture_file_changed')
        named = os.stat(verification_id + '.json', dir_fd=parent, follow_symlinks=False)
        need(_stable(named) == _stable(after), 'fixture_file_replaced')
        body = json.loads(raw, object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(FixtureHold('fixture_nonfinite')))
        need(type(body) is dict and encode(body) == raw, 'fixture_file_encoding_invalid')
        return {'body': body, 'identity_sha256': _identity(after, raw), 'publication_time_ns': after.st_ctime_ns}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)

def publish_evidence(epoch, verification_id, body):
    """Private writer primitive: exclusive create, never update/delete/retry.

    Caller must use prepare_evidence() and its current-owner/store checks first.
    An unknown/partial create remains untouched and cannot be retried here.
    """
    need(uuid(verification_id), 'fixture_verification_id_invalid')
    raw = encode(body)
    need(0 < len(raw) <= MAX_BYTES, 'fixture_file_capacity')
    parent = _parent(epoch, create=True)
    fd = None
    try:
        fd = os.open(verification_id + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=parent)
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            need(count > 0, 'fixture_file_write_unknown')
            offset += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)
    result = read_evidence(epoch, verification_id)
    need(result['body'] == body, 'fixture_file_readback_unknown')
    return result

READY_FIELDS = {'contract', 'episode_id', 'owner_ref_sha256', 'conversation_id', 'capture_generation',
                'capture_owner_sha256', 'capture_protocol_version', 'ready_observation_sha256', 'backend_join_sha256', 'ready_evidence_kind',
                'ready_observed_at', 'selected_device_name', 'selected_exact_row_count'}
PLAYBACK_FIELDS = {'contract', 'episode_id', 'fixture_audio_sha256', 'playback_started_at',
                   'playback_finished_at', 'playback_count', 'exit_code', 'reaped'}

def prepare_evidence(db, uid, *, epoch, expected_policy_sha256, ready, playback, manifest, now, consent_guard, clock=None):
    """Read-only preparation for trusted root writer; no file or DB writes.

    No public route accepts these arguments. Caller is the existing retained
    operator with pinned actual sole-CUA Ready and audio-owner playback exports.
    No field in these receipts grants authority. Semantic fixture acceptance is
    computed later, against FINAL decoded segments in the stock transaction.
    """
    from database import normal_native_source_once as native
    from database import normal_enrichment_activation as activation
    need(callable(consent_guard) and consent_guard(uid) == uid, 'fixture_owner_consent_changed')
    need(type(ready) is dict and set(ready) == READY_FIELDS and ready['contract'] == 'ella-main-source-mapped-ready-witness-v1',
         'fixture_ready_shape')
    need(type(playback) is dict and set(playback) == PLAYBACK_FIELDS
         and playback['contract'] == 'ella-test-playback-witness-v1', 'fixture_playback_shape')
    need(type(manifest) is dict and set(manifest) == {'contract','audio_sha256','text','text_sha256','words_sha256','match_profile'}
         and fixture_manifest(manifest['audio_sha256'], manifest['text'], manifest['match_profile']) == manifest, 'fixture_manifest_invalid')
    need(isinstance(now, datetime) and now.tzinfo is not None, 'fixture_clock_invalid')
    policy_snap = activation.reference(db).get(timeout=5, retry=None)
    policy = policy_snap.to_dict() if policy_snap.exists else {}
    tenant_snap = activation.tenant_ref(db, uid).get(timeout=5, retry=None)
    tenant = tenant_snap.to_dict() if tenant_snap.exists else {}
    need(native.digest(policy) == expected_policy_sha256 and policy.get('epoch') == epoch
         and activation.valid_policy(policy, now) and native.holds_match(None, db, uid, policy, tenant)
         and policy.get('active') == [] and policy.get('hour_claims') == 0 and not tenant.get('active_job_id'),
         'fixture_scope_or_holds_changed')
    req = policy[native.KEY]['fixture_requirement']
    need(all(req[k] == v for k,v in {'fixture_audio_sha256': manifest['audio_sha256'],
                                   'fixture_text_sha256': manifest['text_sha256'],
                                   'expected_transcript_words_sha256': manifest['words_sha256'], 'match_profile': manifest['match_profile']}.items()),
         'fixture_manifest_not_prebound')
    need(ready['owner_ref_sha256'] == native.OWNER and ready['episode_id'] == req['episode_id']
         and playback['episode_id'] == req['episode_id'] and playback['fixture_audio_sha256'] == req['fixture_audio_sha256']
         and ready['selected_device_name'] == 'Friend' and type(ready['selected_exact_row_count']) is int
         and ready['selected_exact_row_count'] == 1 and type(ready['capture_protocol_version']) is int
         and ready['capture_protocol_version'] == 2 and pin(ready['ready_observation_sha256']) and pin(ready['backend_join_sha256'])
         and ready['ready_evidence_kind'] == 'main-owned-session-deviceRecord-transcriptServiceReady-v1'
         and type(playback['playback_count']) is int and playback['playback_count'] == 1
         and type(playback['exit_code']) is int and playback['exit_code'] == 0 and playback['reaped'] is True,
         'fixture_physical_witness_invalid')
    challenge_registered(db,None,policy)
    ready_cid = ready['conversation_id']
    current_authority = db.collection('users').document(uid).collection('capture_authority').document('current').get(timeout=5,retry=None).to_dict() or {}
    cid = current_authority.get('conversation_id')
    need(uuid(cid) and uuid(ready['capture_generation']) and pin(ready['capture_owner_sha256']), 'fixture_source_id_invalid')
    fields = ['id','uid','source','discarded','capture_protocol_version','capture_generation','capture_owner_token',
              'thin_cloudbot_source_binding','external_data','status','normal_source_completed_at','active_summary_version_id','ella_test_fixture_capture_lineage']
    source_snap = db.collection('users').document(uid).collection('conversations').document(cid).get(
        timeout=5, retry=None, field_paths=fields)
    source = source_snap.to_dict() if source_snap.exists else {}
    a = db.collection('users').document(uid).collection('capture_authority').document('current').get(
        timeout=5, retry=None).to_dict() or {}
    need(source_snap.exists and native.source_native(source, uid, cid)
         and source.get('status') != 'completed' and not source.get('normal_source_completed_at')
         and not source.get('active_summary_version_id') and source['capture_generation'] == ready['capture_generation']
         and native.sha(source['capture_owner_token']) == ready['capture_owner_sha256']
         and a.get('state') == 'active' and a.get('conversation_id') == cid
         and a.get('generation') == source['capture_generation'] and a.get('owner_token') == source['capture_owner_token']
         and native.aware(a.get('lease_expires_at')) and a['lease_expires_at'] > now,
         'fixture_current_capture_not_bound')
    start, end = instant(playback['playback_started_at']), instant(playback['playback_finished_at'])
    ready_at = instant(ready['ready_observed_at'])
    from database.fixture_capture_lineage import verify as verify_lineage
    lineage = verify_lineage(db,None,uid,policy,cid,source,ready_cid,ready_at,now)
    need(policy['source_completion_boundary'] < instant(lineage['ready_source_created_at']) <= ready_at <= start < end <= now < policy['expires_at'],
         'fixture_physical_order_invalid')
    marker = db.collection(native.COLLECTION).document(epoch).get(timeout=5, retry=None)
    need(not marker.exists, 'fixture_admission_already_exists')
    need(consent_guard(uid) == uid, 'fixture_owner_consent_changed')
    finished = clock() if clock is not None else now
    need(isinstance(finished, datetime) and finished.tzinfo is not None and now <= finished
         < min(policy['expires_at'], a['lease_expires_at']), 'fixture_authority_expired_during_read')
    return {**{k:v for k,v in req.items() if k != 'expected_transcript_words_sha256'},
            **lineage, 'owner_ref_sha256': native.OWNER, 'admission_epoch': epoch, 'conversation_id': cid,
            'capture_protocol_version': 2, 'capture_generation': source['capture_generation'],
            'capture_owner_sha256': native.sha(source['capture_owner_token']),
            'source_created_at': str(source_snap.create_time), 'ready_observed_at': str(ready_at),
            'ready_observation_sha256': ready['ready_observation_sha256'],
            'backend_join_sha256': ready['backend_join_sha256'], 'ready_evidence_kind': ready['ready_evidence_kind'],
            'playback_started_at': str(start), 'playback_finished_at': str(end),
            'published_at': str(finished), 'expires_at': str(policy['expires_at']),
            'capture_lease_expires_at': str(a['lease_expires_at']),
            'physical_evidence_sha256': digest({'ready':ready,'playback':playback,'manifest_sha256':digest(manifest)})}
