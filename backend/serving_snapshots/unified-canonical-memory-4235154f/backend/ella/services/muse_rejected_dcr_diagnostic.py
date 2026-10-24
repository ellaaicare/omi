"""Default-off, one-shot metadata observer for an ALREADY REJECTED DCR.

No client registration, network request, credentials, grant or access permit.
The retained callback is an UNTRUSTED candidate, never authorization evidence.
Only a backend owner can create the fixed root-private discovery directory.
"""
import json
import math
import os
import re
import stat
import time
import uuid

ROOT = '/run/ella-muse-rejected-dcr-discovery'
ACCESS_TICKET = '/run/ella-muse-reader-pilot.json'
MAX_TTL = 120
CONTRACT = 'ella.muse.rejected-dcr-discovery.v1'


def _file_identity(fd, uid):
    s = os.fstat(fd)
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != uid
            or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1):
        raise ValueError('private_file_identity')
    return (s.st_dev, s.st_ino)


def _read_permit(dfd, uid):
    fd = os.open('PERMIT.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        identity = _file_identity(fd, uid)
        raw = os.read(fd, 2049)
        if len(raw) > 2048:
            raise ValueError('permit_bound')
        p = json.loads(raw)
        if (type(p) is not dict or set(p) != {
                'contract', 'armed', 'attempt_id', 'not_before', 'expires_at', 'expires_monotonic'}
                or p['contract'] != CONTRACT or p['armed'] is not True
                or type(p['attempt_id']) is not str
                or str(uuid.UUID(p['attempt_id'])) != p['attempt_id']
                or type(p['not_before']) is not int or type(p['expires_at']) is not int
                or not 0 < p['expires_at'] - p['not_before'] <= MAX_TTL
                or type(p['expires_monotonic']) not in (int,float)
                or not math.isfinite(p['expires_monotonic']) or p['expires_monotonic']<=0):
            raise ValueError('permit_contract')
        return p, raw, identity
    finally:
        os.close(fd)


def _create(dfd, name, value, uid):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')
    if len(raw) > 4096:
        raise ValueError('receipt_bound')
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=dfd)
    try:
        _file_identity(fd, uid)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short_write')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(dfd)


def _candidate(data):
    if type(data) is not dict:
        raise ValueError('invalid_public_metadata')
    uris = data.get('redirect_uris')
    if type(uris) is not list or len(uris) != 1 or type(uris[0]) is not str:
        raise ValueError('invalid_public_metadata')
    uri = uris[0]
    # Conservative diagnostic grammar: do not retain opaque query/fragment,
    # userinfo, percent-encoding or arbitrary Unicode that could carry secrets.
    # No candidate URL is resolved, fetched, normalized or trusted.
    if not 1 <= len(uri) <= 1024 or not uri.isascii():
        raise ValueError('invalid_public_metadata')
    match = re.fullmatch(r'https://([A-Za-z0-9.-]+)(?::443)?(/[A-Za-z0-9/_.~-]*)?', uri)
    if not match:
        raise ValueError('invalid_public_metadata')
    host = match.group(1)
    labels = host.split('.')
    if (len(host) > 253 or len(labels) < 2
            or not re.fullmatch(r'[A-Za-z]{2,63}', labels[-1])
            or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x)
                   for x in labels)
            or host.lower().endswith(('.local', '.localhost', '.internal', '.invalid'))):
        raise ValueError('invalid_public_metadata')
    if (data.get('token_endpoint_auth_method', 'none') != 'none'
            or data.get('grant_types', ['authorization_code']) != ['authorization_code']
            or data.get('response_types', ['code']) != ['code']):
        raise ValueError('invalid_public_metadata')
    return {'redirect_uri': uri, 'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code'], 'response_types': ['code']}


REJECTION_REASON_CODES = frozenset({
    'metadata_not_object', 'redirect_uris_missing', 'redirect_uris_not_array',
    'redirect_uris_count_not_one', 'redirect_uri_not_string',
    'redirect_uri_length_bound', 'redirect_uri_non_ascii',
    'redirect_uri_query_disallowed', 'redirect_uri_fragment_disallowed',
    'redirect_uri_at_sign_disallowed', 'redirect_uri_percent_encoding_disallowed',
    'redirect_uri_backslash_or_control', 'redirect_uri_restricted_grammar',
    'redirect_uri_hostname_restricted', 'token_auth_method_not_none',
    'grant_types_not_auth_code_only', 'response_types_not_code_only',
    'unclassified_candidate_rejection',
})


def _rejection_reason(data):
    """Closed diagnostic code only. Never return or interpolate input values.

    This deliberately does NOT decide acceptance: _candidate remains unchanged.
    A code on a future receipt cannot explain the already-consumed historical DCR.
    """
    if type(data) is not dict:
        return 'metadata_not_object'
    if 'redirect_uris' not in data:
        return 'redirect_uris_missing'
    uris = data['redirect_uris']
    if type(uris) is not list:
        return 'redirect_uris_not_array'
    if len(uris) != 1:
        return 'redirect_uris_count_not_one'
    uri = uris[0]
    if type(uri) is not str:
        return 'redirect_uri_not_string'
    if not 1 <= len(uri) <= 1024:
        return 'redirect_uri_length_bound'
    if not uri.isascii():
        return 'redirect_uri_non_ascii'
    if '?' in uri:
        return 'redirect_uri_query_disallowed'
    if '#' in uri:
        return 'redirect_uri_fragment_disallowed'
    if '@' in uri:
        return 'redirect_uri_at_sign_disallowed'
    if '%' in uri:
        return 'redirect_uri_percent_encoding_disallowed'
    if '\\' in uri or any(ord(c) < 32 or ord(c) == 127 for c in uri):
        return 'redirect_uri_backslash_or_control'
    match = re.fullmatch(r'https://([A-Za-z0-9.-]+)(?::443)?(/[A-Za-z0-9/_.~-]*)?', uri)
    if not match:
        return 'redirect_uri_restricted_grammar'
    host = match.group(1)
    labels = host.split('.')
    if (len(host) > 253 or len(labels) < 2
            or not re.fullmatch(r'[A-Za-z]{2,63}', labels[-1])
            or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x)
                   for x in labels)
            or host.lower().endswith(('.local', '.localhost', '.internal', '.invalid'))):
        return 'redirect_uri_hostname_restricted'
    if data.get('token_endpoint_auth_method', 'none') != 'none':
        return 'token_auth_method_not_none'
    if data.get('grant_types', ['authorization_code']) != ['authorization_code']:
        return 'grant_types_not_auth_code_only'
    if data.get('response_types', ['code']) != ['code']:
        return 'response_types_not_code_only'
    return None


GRANT_SHAPE_CODES=frozenset({'grant_types_absent_default','grant_types_exact_auth_code_only','grant_types_not_array','grant_types_empty_array','grant_types_nonstring_element','grant_types_duplicate_auth_code_only','grant_types_exact_auth_code_plus_refresh','grant_types_auth_code_refresh_with_duplicates','grant_types_refresh_only','grant_types_other_values'})
def _grant_shape(data):
    if 'grant_types' not in data:return 'grant_types_absent_default'
    value=data['grant_types']
    if type(value) is not list:return 'grant_types_not_array'
    if value==['authorization_code']:return 'grant_types_exact_auth_code_only'
    if not value:return 'grant_types_empty_array'
    if any(type(item) is not str for item in value):return 'grant_types_nonstring_element'
    known=set(value)
    if known=={'authorization_code'}:return 'grant_types_duplicate_auth_code_only'
    if known=={'authorization_code','refresh_token'}:
        return 'grant_types_exact_auth_code_plus_refresh' if len(value)==2 else 'grant_types_auth_code_refresh_with_duplicates'
    if known=={'refresh_token'}:return 'grant_types_refresh_only'
    return 'grant_types_other_values'

AUTH_SHAPE_CODES=frozenset({'auth_absent_default_none','auth_exact_none','auth_client_secret_basic','auth_client_secret_post','auth_nonstring','auth_other'})
RESPONSE_SHAPE_CODES=frozenset({'response_absent_default_code','response_exact_code','response_not_array','response_empty_array','response_nonstring_element','response_duplicate_code_only','response_code_plus_token','response_token_only','response_other'})
REDIRECT_SHAPE_CODES=frozenset({'redirect_strict_single_https'})|frozenset(x for x in REJECTION_REASON_CODES if x.startswith('redirect_') or x=='metadata_not_object')
def _metadata_shapes(data):
    if type(data) is not dict:
        return {'redirect':'metadata_not_object','auth':'auth_absent_default_none','grants':'grant_types_absent_default','responses':'response_absent_default_code'}
    projection={} if 'redirect_uris' not in data else {'redirect_uris':data['redirect_uris']}
    redirect=_rejection_reason(projection)
    if redirect is None:redirect='redirect_strict_single_https'
    if 'token_endpoint_auth_method' not in data:auth='auth_absent_default_none'
    elif type(data['token_endpoint_auth_method']) is not str:auth='auth_nonstring'
    else:auth={'none':'auth_exact_none','client_secret_basic':'auth_client_secret_basic','client_secret_post':'auth_client_secret_post'}.get(data['token_endpoint_auth_method'],'auth_other')
    if 'response_types' not in data:responses='response_absent_default_code'
    else:
        value=data['response_types']
        if type(value) is not list:responses='response_not_array'
        elif value==['code']:responses='response_exact_code'
        elif not value:responses='response_empty_array'
        elif any(type(x) is not str for x in value):responses='response_nonstring_element'
        elif set(value)=={'code'}:responses='response_duplicate_code_only'
        elif set(value)=={'code','token'}:responses='response_code_plus_token'
        elif set(value)=={'token'}:responses='response_token_only'
        else:responses='response_other'
    return {'redirect':redirect,'auth':auth,'grants':_grant_shape(data),'responses':responses}

def _validate_shapes(value):
    return (type(value) is dict and set(value)=={'redirect','auth','grants','responses'}
            and type(value['redirect']) is str and value['redirect'] in REDIRECT_SHAPE_CODES
            and type(value['auth']) is str and value['auth'] in AUTH_SHAPE_CODES
            and type(value['grants']) is str and value['grants'] in GRANT_SHAPE_CODES
            and type(value['responses']) is str and value['responses'] in RESPONSE_SHAPE_CODES)

def _observe(data, root, access_ticket, clock, mono, required_uid):
    """Internal test seam; public entry point has no configurable arguments."""
    if os.path.lexists(access_ticket):
        return
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ds = os.fstat(dfd)
        if ds.st_uid != required_uid or stat.S_IMODE(ds.st_mode) != 0o700:
            return
        permit, raw, identity = _read_permit(dfd, required_uid)
        now = clock()
        if not permit['not_before'] <= now < permit['expires_at'] or not 0 < permit['expires_monotonic']-mono() <= MAX_TTL:
            return
        mono_end = min(permit['expires_monotonic'],mono() + min(MAX_TTL, permit['expires_at'] - now))
        def collecting():
            return (permit['not_before'] <= clock() < permit['expires_at']
                    and mono() < mono_end and not os.path.lexists(access_ticket))
        if not collecting():
            return
        # Consume before inspecting untrusted request metadata. A malformed or
        # unrelated public request may consume the slot; never reset or retry.
        _create(dfd, 'START.json', {'contract': CONTRACT,
                'attempt_id': permit['attempt_id'], 'consumed': True}, required_uid)
        # START fsync may consume the remaining budget. Never inspect metadata
        # without a new check after this blocking operation.
        if not collecting():
            return
        receipt = {'contract': CONTRACT, 'attempt_id': permit['attempt_id'],
                   'observed_unix': now, 'registration_rejected': True,
                   'client_created': False, 'grant_created': False,
                   'code_or_token_issued': False, 'raw_request_retained': False,
                   'candidate_is_provider_provenance': False,
                   'access_ticket_or_clock_authorized': False, 'metadata_shapes': _metadata_shapes(data)}
        if not collecting():
            return
        try:
            receipt['candidate'] = _candidate(data)
            receipt['status'] = 'REJECTED_DCR_UNTRUSTED_CANDIDATE_ONLY'
        except ValueError:
            receipt['status'] = 'REJECTED_DCR_METADATA_NOT_RETAINED'
            reason = _rejection_reason(data)
            receipt['metadata_rejection_reason'] = (reason if reason in REJECTION_REASON_CODES
                                                    else 'unclassified_candidate_rejection')
        again, again_raw, again_id = _read_permit(dfd, required_uid)
        current = os.stat(root, follow_symlinks=False)
        if (again_raw != raw or again_id != identity or again != permit
                or not stat.S_ISDIR(current.st_mode)
                or current.st_uid != required_uid
                or stat.S_IMODE(current.st_mode) != 0o700
                or (current.st_dev, current.st_ino) != (ds.st_dev, ds.st_ino)
                or not permit['not_before'] <= clock() < permit['expires_at']
                or mono() >= mono_end or os.path.lexists(access_ticket)):
            return  # START without receipt means UNKNOWN, never retry.
        _create(dfd, 'RECEIPT.json', receipt, required_uid)
    finally:
        os.close(dfd)


def observe_rejected_registration(data):
    """Best effort only. Never change the original rejected HTTP response."""
    try:
        _observe(data, ROOT, ACCESS_TICKET, time.time, time.monotonic, 0)
    except Exception:
        # No exception text: paths/request metadata must not enter application logs.
        return
