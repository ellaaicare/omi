"""Explicit first-party Thin request selection. Headers select; server grants authorize.

Main callers return None and retain their existing consent dependencies. Cleanup
may parse the immutable scope without requiring a current grant, but must still
prove the existing operation/session's exact authenticated owner and binding.
"""
from dataclasses import dataclass, field
import re
from ella.services.thin_cloudbot_consent import ThinConsentHold, need

HEADERS = ('x-ella-consent-target', 'x-ella-consent-receipt-id', 'x-ella-account-epoch')
SCOPE_FIELDS = frozenset(('consent_target', 'receipt_id', 'account_epoch_hash'))


def parse_scope(headers):
    values = {}
    for name, value in headers.items():
        key = str(name).lower()
        if key in HEADERS:
            need(key not in values, 'thin_duplicate_target_header')
            need(type(value) is str, 'thin_target_header_invalid')
            values[key] = value
    if not values:
        return None
    need(set(values) == set(HEADERS), 'thin_target_headers_incomplete')
    scope = dict(consent_target=values[HEADERS[0]], receipt_id=values[HEADERS[1]],
                 account_epoch_hash=values[HEADERS[2]])
    return validate_scope(scope)


def validate_scope(scope):
    need(type(scope) is dict and set(scope) == SCOPE_FIELDS, 'thin_request_scope_invalid')
    need(scope['consent_target'] == 'thin-cloudbot', 'thin_request_target_invalid')
    need(type(scope['receipt_id']) is str and re.fullmatch(r'tccr_[0-9a-f]{32}', scope['receipt_id']),
         'thin_request_receipt_invalid')
    need(type(scope['account_epoch_hash']) is str and re.fullmatch(r'[0-9a-f]{64}', scope['account_epoch_hash']),
         'thin_request_epoch_invalid')
    return dict(scope)


def default_service():
    from database._client import db
    from ella.services.thin_cloudbot_consent import ThinCloudbotConsentService, FirestoreThinConsentRepository
    return ThinCloudbotConsentService(FirestoreThinConsentRepository(db))


@dataclass(frozen=True)
class RequestAuthority:
    uid: str
    auth_time: int
    _scope: tuple
    _witness: tuple
    _service: object = field(repr=False, compare=False)

    @property
    def scope(self):
        return dict(self._scope)

    @property
    def witness(self):
        return dict(self._witness)

    def revalidate(self):
        current = require_request_identity(self.uid, self.auth_time, self.scope, self._service)
        need(current == self.witness, 'thin_request_authority_changed')
        return self.witness

    def revalidate_in_transaction(self, tx, *, now):
        return self._service.repository.assert_witness_in_transaction(
            tx, self.uid, self.auth_time, self.witness, now)


def require_request_identity(uid, auth_time, scope, service):
    scope = validate_scope(scope)
    witness = service.require_current(uid, auth_time)
    need(witness.get('consent_target') == scope['consent_target']
         and witness.get('receipt_id') == scope['receipt_id']
         and witness.get('account_epoch_hash') == scope['account_epoch_hash'],
         'thin_request_receipt_or_epoch_changed')
    return witness


def require_request(identity, scope, *, service=None):
    """identity MUST originate in get_firebase_token_identity, never request JSON.

    Exact uid/auth_time validation also happens in the dedicated grant repository.
    This deliberately does not implement or override Main consent authorization.
    """
    if scope is None:
        return None
    need(not isinstance(identity, dict), 'thin_verified_identity_required')
    uid, auth_time = getattr(identity, 'uid', None), getattr(identity, 'auth_time', None)
    need(type(uid) is str and bool(uid) and '/' not in uid and type(auth_time) is int and auth_time > 0,
         'thin_verified_identity_required')
    service = service if service is not None else default_service()
    scope = validate_scope(scope)
    witness = require_request_identity(uid, auth_time, scope, service)
    return RequestAuthority(uid, auth_time, tuple(sorted(scope.items())), tuple(sorted(witness.items())), service)


def require_from_http(request, *, service=None):
    """Resolve only Thin; exact Firebase verification is explicit, no DevAuth fallback."""
    scope = parse_scope(request.headers)
    if scope is None:
        return None
    from utils.ella.exact_firebase_auth import get_firebase_token_identity
    identity = get_firebase_token_identity(
        authorization=request.headers.get('authorization'),
        x_app_version=request.headers.get('x-app-version'),
        x_ella_app_build=request.headers.get('x-ella-app-build'),
        x_ella_client_version=request.headers.get('x-ella-client-version'))
    return require_request(identity, scope, service=service)


def require_mcp_request(uid, scope, *, db=None, service=None):
    """Existing authenticated MCP key UID; reuse server-stored verified login epoch.

    This is persistent dedicated consent, not a fresh Firebase login or host
    connection proof. Caller MUST authenticate the MCP principal before use.
    """
    if scope is None:
        return None
    scope = validate_scope(scope)
    need(type(uid) is str and bool(uid) and '/' not in uid, 'thin_mcp_owner_required')
    if db is None:
        from database._client import db
    from ella.services.thin_cloudbot_consent import ThinCloudbotConsentService, FirestoreThinConsentRepository
    service = service if service is not None else ThinCloudbotConsentService(FirestoreThinConsentRepository(db))
    root = db.collection('ella_thin_cloudbot_consent').document(uid)
    current = root.get().to_dict() or {}
    private = root.collection('receipts').document(scope['receipt_id']).get().to_dict() or {}
    need((current.get('current_state') or {}).get('receipt_id') == scope['receipt_id']
         and private.get('owner_uid') == uid, 'thin_mcp_receipt_changed')
    auth_time = private.get('verified_auth_time')
    need(type(auth_time) is int and auth_time > 0, 'thin_mcp_verified_epoch_unavailable')
    from types import SimpleNamespace
    return require_request(SimpleNamespace(uid=uid, auth_time=auth_time), scope, service=service)
