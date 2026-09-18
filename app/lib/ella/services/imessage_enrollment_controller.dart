import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';

typedef ImessageAuthorityReader = String? Function();
typedef ImessageMessagesLauncher = Future<bool> Function(Uri uri);
typedef ImessageIdGenerator = String Function();
typedef ImessageAppInfoReader = Future<({String version, String buildNumber})> Function();
typedef _ImessageAuthorityLease = ({String uid, int epoch});
typedef _ImessagePendingStartAttempt = ({
  String uid,
  String handsetE164,
  ImessageConsentReceipt receipt,
  String idempotencyKey,
});

enum ImessageEnrollmentOperation {
  idle,
  refreshing,
  loadingConsent,
  starting,
  decliningConsent,
  revokingBinding,
  revokingConsent,
}

extension on ImessageEnrollmentOperation {
  bool get isMutation => switch (this) {
        ImessageEnrollmentOperation.starting ||
        ImessageEnrollmentOperation.decliningConsent ||
        ImessageEnrollmentOperation.revokingBinding ||
        ImessageEnrollmentOperation.revokingConsent =>
          true,
        _ => false,
      };
}

/// Retains one owner's sensitive enrollment attempt in process memory only.
///
/// Proof codes and phone identifiers are intentionally never written to disk.
class ImessageEnrollmentSessionStore {
  String? _ownerUid;
  ImessageEnrollmentStatus? _status;
  ImessageEnrollmentProof? _proof;
  ImessageConsentPolicy? _consentPolicy;
  _ImessagePendingStartAttempt? _pendingStartAttempt;
  bool _consentRevocationPending = false;

  void clear() {
    _ownerUid = null;
    _status = null;
    _proof = null;
    _consentPolicy = null;
    _pendingStartAttempt = null;
    _consentRevocationPending = false;
  }
}

class ImessageEnrollmentController extends ChangeNotifier {
  ImessageEnrollmentController({
    required ImessageEnrollmentGateway gateway,
    required ImessageConsentGateway consentGateway,
    required ImessageAuthorityReader authorityReader,
    required ImessageMessagesLauncher messagesLauncher,
    required ImessageIdGenerator idGenerator,
    required ImessageAppInfoReader appInfoReader,
    ImessageEnrollmentSessionStore? sessionStore,
    DateTime Function()? now,
  })  : _gateway = gateway,
        _consentGateway = consentGateway,
        _authorityReader = authorityReader,
        _messagesLauncher = messagesLauncher,
        _idGenerator = idGenerator,
        _appInfoReader = appInfoReader,
        _sessionStore = sessionStore ?? ImessageEnrollmentSessionStore(),
        _now = now ?? DateTime.now;

  final ImessageEnrollmentGateway _gateway;
  final ImessageConsentGateway _consentGateway;
  final ImessageAuthorityReader _authorityReader;
  final ImessageMessagesLauncher _messagesLauncher;
  final ImessageIdGenerator _idGenerator;
  final ImessageAppInfoReader _appInfoReader;
  final ImessageEnrollmentSessionStore _sessionStore;
  final DateTime Function() _now;

  String? _ownerUid;
  int _authorityEpoch = 0;
  ImessageEnrollmentStatus? _status;
  ImessageEnrollmentProof? _proof;
  ImessageConsentPolicy? _consentPolicy;
  ImessageEnrollmentFailure? _failure;
  _ImessagePendingStartAttempt? _pendingStartAttempt;
  bool _consentRevocationPending = false;
  ImessageEnrollmentOperation _operation = ImessageEnrollmentOperation.idle;
  int _operationSequence = 0;
  int _activeOperation = 0;
  int _mutationRevision = 0;

  ImessageEnrollmentStatus? get status => _ownsCurrentAuthority ? _status : null;
  ImessageEnrollmentProof? get proof => _ownsCurrentAuthority ? _proof : null;
  ImessageConsentPolicy? get consentPolicy => _ownsCurrentAuthority ? _consentPolicy : null;
  ImessageEnrollmentFailure? get failure => _ownsCurrentAuthority ? _failure : null;
  bool get loading => _ownsCurrentAuthority && _operation != ImessageEnrollmentOperation.idle;
  ImessageEnrollmentOperation get operation => _ownsCurrentAuthority ? _operation : ImessageEnrollmentOperation.idle;
  bool get consentRevocationPending => _ownsCurrentAuthority && _consentRevocationPending;
  bool get canRetryPendingStart {
    final attempt = _pendingStartAttempt;
    return _ownsCurrentAuthority &&
        _status?.state == ImessageEnrollmentState.verificationPending &&
        attempt != null &&
        attempt.uid == _ownerUid;
  }

  bool get canDisconnect {
    if (!_ownsCurrentAuthority || _consentRevocationPending) return false;
    final currentStatus = _status;
    return currentStatus != null &&
        currentStatus.authorityGeneration > 0 &&
        currentStatus.state != ImessageEnrollmentState.notConnected &&
        currentStatus.state != ImessageEnrollmentState.revoked;
  }

  bool get isReady => status?.isReady ?? false;
  bool get canOpenMessages {
    if (!_ownsCurrentAuthority) return false;
    final currentProof = _proof;
    return _status?.state == ImessageEnrollmentState.verificationPending &&
        _status?.assignedDestination != null &&
        currentProof != null &&
        !currentProof.isExpiredAt(_now().toUtc());
  }

  bool handleAuthorityChanged() {
    final authority = _currentAuthority;
    if (authority == _ownerUid) return false;
    if (_ownerUid == null && authority != null) {
      _ownerUid = authority;
      _authorityEpoch += 1;
      _restoreSession(authority);
      _failure = null;
      notifyListeners();
      return true;
    }
    _replaceAuthority(
      authority,
      authority == null
          ? const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.unauthenticated)
          : const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged),
    );
    notifyListeners();
    return true;
  }

  Future<void> load() async {
    if (_operation.isMutation || _operation == ImessageEnrollmentOperation.refreshing) return;
    await _runStatusRefresh();
  }

  Future<void> loadConsentPolicy() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    if (_operation.isMutation) return;
    final operation = _begin(ImessageEnrollmentOperation.loadingConsent);
    try {
      final policy = await _consentGateway.fetchConsentPolicy();
      _assertAuthority(lease);
      if (!_isOperationCurrent(operation)) return;
      _consentPolicy = policy;
      _persistSession();
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<void> start(String handsetE164) async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    if (_operation.isMutation) return;
    final policy = _consentPolicy;
    if (policy == null) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.consentUnavailable,
        code: 'imessage_consent_policy_missing',
      );
      notifyListeners();
      return;
    }
    final operation = _begin(ImessageEnrollmentOperation.starting, mutation: true);
    _proof = null;
    _persistSession();
    _ImessagePendingStartAttempt? attempt;
    try {
      final retry = _pendingStartAttempt;
      if (retry != null &&
          retry.uid == lease.uid &&
          retry.handsetE164 == handsetE164 &&
          retry.receipt.matches(policy, ImessageConsentDecision.granted)) {
        attempt = retry;
      } else {
        final receipt = await _recordConsent(lease, ImessageConsentDecision.granted, policy);
        _assertAuthority(lease);
        if (!receipt.matches(policy, ImessageConsentDecision.granted)) {
          throw const ImessageEnrollmentFailure(
            ImessageEnrollmentFailureKind.malformedResponse,
            code: 'imessage_consent_receipt_mismatch',
          );
        }
        attempt = (
          uid: lease.uid,
          handsetE164: handsetE164,
          receipt: receipt,
          idempotencyKey: _idGenerator(),
        );
        _pendingStartAttempt = attempt;
        _persistSession();
      }
      final response = await _gateway.start(
        handsetE164: handsetE164,
        consentReceiptId: attempt.receipt.receiptId,
        idempotencyKey: attempt.idempotencyKey,
      );
      _assertAuthority(lease);
      if (!_isValidStartResponse(response)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_start_response_incoherent',
        );
      }
      _status = response.status;
      _proof = response.proof;
      _pendingStartAttempt = attempt;
      _consentRevocationPending = false;
      _persistSession();
    } on ImessageEnrollmentFailure catch (error) {
      if (!_isAmbiguousStartFailure(error) && _pendingStartAttempt == attempt) {
        _pendingStartAttempt = null;
        _persistSession();
      }
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<void> retryPendingStart() async {
    final lease = _readAuthorityOrFail();
    if (lease == null || _operation.isMutation) return;
    final attempt = _pendingStartAttempt;
    if (attempt == null || attempt.uid != lease.uid || _status?.state != ImessageEnrollmentState.verificationPending) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'imessage_pending_attempt_unavailable',
      );
      notifyListeners();
      return;
    }

    final operation = _begin(ImessageEnrollmentOperation.starting, mutation: true);
    try {
      final response = await _gateway.start(
        handsetE164: attempt.handsetE164,
        consentReceiptId: attempt.receipt.receiptId,
        idempotencyKey: attempt.idempotencyKey,
      );
      _assertAuthority(lease);
      if (!_isOperationCurrent(operation) || !_isValidStartResponse(response)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_start_response_incoherent',
        );
      }
      _status = response.status;
      _proof = response.proof;
      _persistSession();
    } on ImessageEnrollmentFailure catch (error) {
      if (!_isAmbiguousStartFailure(error)) {
        _pendingStartAttempt = null;
        _persistSession();
      }
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<void> declineConsent() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    if (_operation.isMutation) return;
    final policy = _consentPolicy;
    if (policy == null) {
      _consentPolicy = null;
      _pendingStartAttempt = null;
      _persistSession();
      return;
    }
    final operation = _begin(ImessageEnrollmentOperation.decliningConsent, mutation: true);
    try {
      final receipt = await _recordConsent(lease, ImessageConsentDecision.declined, policy);
      _assertAuthority(lease);
      if (!receipt.matches(policy, ImessageConsentDecision.declined)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_consent_receipt_mismatch',
        );
      }
      _consentPolicy = null;
      _pendingStartAttempt = null;
      _persistSession();
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<bool> openMessages() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return false;
    final currentStatus = _status;
    final currentProof = _proof;
    if (currentStatus?.state != ImessageEnrollmentState.verificationPending ||
        currentStatus?.assignedDestination == null ||
        currentProof == null) {
      _failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable);
      notifyListeners();
      return false;
    }
    if (currentProof.isExpiredAt(_now().toUtc())) {
      _failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.proofExpired);
      _proof = null;
      _persistSession();
      notifyListeners();
      return false;
    }

    final uri = Uri.parse(
      'sms:${currentStatus!.assignedDestination}&body=${Uri.encodeComponent(currentProof.code)}',
    );
    try {
      _assertAuthority(lease);
      final opened = await _messagesLauncher(uri);
      _assertAuthority(lease);
      if (!opened) {
        _failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable);
        notifyListeners();
      }
      return opened;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
      notifyListeners();
      return false;
    } catch (_) {
      _commitFailureIfCurrent(
        lease,
        const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable),
      );
      notifyListeners();
      return false;
    }
  }

  Future<void> revoke() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    if (_operation.isMutation) return;
    if (_consentRevocationPending) {
      await _resumeConsentRevocation(lease);
      return;
    }
    final generation = _status?.authorityGeneration ?? 0;
    if (generation < 1) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'missing_authority_generation',
      );
      notifyListeners();
      return;
    }
    final operation = _begin(ImessageEnrollmentOperation.revokingBinding, mutation: true);
    try {
      final response = await _gateway.revoke(
        expectedGeneration: generation,
        idempotencyKey: _idGenerator(),
      );
      _assertAuthority(lease);
      _status = response;
      _proof = null;
      _pendingStartAttempt = null;
      _consentRevocationPending = true;
      _persistSession();
      await _completeConsentRevocation(lease);
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<void> _resumeConsentRevocation(_ImessageAuthorityLease lease) async {
    final operation = _begin(ImessageEnrollmentOperation.revokingConsent, mutation: true);
    try {
      await _completeConsentRevocation(lease);
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  Future<void> _completeConsentRevocation(_ImessageAuthorityLease lease) async {
    final policy = await _consentGateway.fetchConsentPolicy();
    _assertAuthority(lease);
    final receipt = await _recordConsent(lease, ImessageConsentDecision.revoked, policy);
    _assertAuthority(lease);
    if (!receipt.matches(policy, ImessageConsentDecision.revoked)) {
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.malformedResponse,
        code: 'imessage_consent_receipt_mismatch',
      );
    }
    _consentPolicy = null;
    _consentRevocationPending = false;
    _persistSession();
  }

  Future<ImessageConsentReceipt> _recordConsent(
    _ImessageAuthorityLease lease,
    ImessageConsentDecision decision,
    ImessageConsentPolicy policy,
  ) async {
    final appInfo = await _appInfoReader();
    _assertAuthority(lease);
    return _consentGateway.recordConsent(
      decision: decision,
      policy: policy,
      requestId: _idGenerator(),
      appVersion: appInfo.version,
      buildNumber: appInfo.buildNumber,
    );
  }

  Future<void> _runStatusRefresh() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    final previousStatus = _status;
    final previousProof = _proof;
    final mutationRevision = _mutationRevision;
    final operation = _begin(ImessageEnrollmentOperation.refreshing);
    try {
      final response = await _gateway.fetchStatus();
      _assertAuthority(lease);
      if (!_isOperationCurrent(operation) || mutationRevision != _mutationRevision) return;
      _status = response;
      final preservePendingAttempt = _canPreserveProof(previousStatus, previousProof, response);
      if (!preservePendingAttempt) {
        _proof = null;
      }
      if (response.state != ImessageEnrollmentState.verificationPending || !preservePendingAttempt) {
        _pendingStartAttempt = null;
      }
      _persistSession();
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease, operation);
    }
  }

  _ImessageAuthorityLease? _readAuthorityOrFail() {
    final authority = _currentAuthority;
    if (authority == null) {
      _replaceAuthority(null, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.unauthenticated));
      notifyListeners();
      return null;
    }
    if (_ownerUid != null && _ownerUid != authority) {
      _replaceAuthority(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged));
      notifyListeners();
      return null;
    }
    if (_ownerUid == null) {
      _ownerUid = authority;
      _authorityEpoch += 1;
      _restoreSession(authority);
    }
    return (uid: authority, epoch: _authorityEpoch);
  }

  void _assertAuthority(_ImessageAuthorityLease expected) {
    if (!_isLeaseCurrent(expected)) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
  }

  int _begin(ImessageEnrollmentOperation operation, {bool mutation = false}) {
    if (mutation) _mutationRevision += 1;
    _operation = operation;
    _activeOperation = ++_operationSequence;
    _failure = null;
    notifyListeners();
    return _activeOperation;
  }

  void _commitFailureIfCurrent(_ImessageAuthorityLease lease, ImessageEnrollmentFailure error) {
    if (_isLeaseCurrent(lease)) {
      _failure = error;
    } else {
      final authority = _currentAuthority;
      if (_ownerUid != authority) {
        _replaceAuthority(
          authority,
          const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged),
        );
      }
    }
  }

  void _finishIfCurrent(_ImessageAuthorityLease lease, int operation) {
    if (_isLeaseCurrent(lease) && _isOperationCurrent(operation)) {
      _operation = ImessageEnrollmentOperation.idle;
    } else {
      final authority = _currentAuthority;
      if (_ownerUid != authority) {
        _replaceAuthority(
          authority,
          const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged),
        );
      }
    }
    notifyListeners();
  }

  bool _isValidStartResponse(ImessageEnrollmentStartResponse response) {
    final pendingStatus = response.status;
    final destination = pendingStatus.assignedDestination;
    return pendingStatus.state == ImessageEnrollmentState.verificationPending &&
        pendingStatus.reason == ImessageEnrollmentReason.verificationPending &&
        destination != null &&
        RegExp(r'^\+[1-9][0-9]{7,14}$').hasMatch(destination) &&
        !pendingStatus.features.textDm &&
        !pendingStatus.features.groups &&
        !pendingStatus.features.attachments &&
        !pendingStatus.features.caregiverDelivery &&
        RegExp(r'^[0-9]{6}$').hasMatch(response.proof.code) &&
        !response.proof.isExpiredAt(_now().toUtc());
  }

  bool _canPreserveProof(
    ImessageEnrollmentStatus? previousStatus,
    ImessageEnrollmentProof? previousProof,
    ImessageEnrollmentStatus response,
  ) {
    return previousStatus?.state == ImessageEnrollmentState.verificationPending &&
        response.state == ImessageEnrollmentState.verificationPending &&
        previousStatus?.authorityGeneration == response.authorityGeneration &&
        previousStatus?.assignedDestination == response.assignedDestination &&
        previousProof != null &&
        !previousProof.isExpiredAt(_now().toUtc());
  }

  bool _isAmbiguousStartFailure(ImessageEnrollmentFailure failure) {
    return switch (failure.kind) {
      ImessageEnrollmentFailureKind.transport ||
      ImessageEnrollmentFailureKind.unavailable ||
      ImessageEnrollmentFailureKind.rateLimited ||
      ImessageEnrollmentFailureKind.malformedResponse =>
        true,
      _ => false,
    };
  }

  String? get _currentAuthority {
    final authority = _authorityReader();
    if (authority == null || authority.isEmpty) return null;
    return authority;
  }

  bool get _ownsCurrentAuthority => _ownerUid == _currentAuthority;

  bool _isLeaseCurrent(_ImessageAuthorityLease lease) {
    return lease.uid == _ownerUid && lease.uid == _currentAuthority && lease.epoch == _authorityEpoch;
  }

  bool _isOperationCurrent(int operation) => operation == _activeOperation;

  void _restoreSession(String authority) {
    if (_sessionStore._ownerUid != authority) {
      _sessionStore.clear();
      _sessionStore._ownerUid = authority;
      return;
    }
    _status = _sessionStore._status;
    _proof = _sessionStore._proof;
    _consentPolicy = _sessionStore._consentPolicy;
    _pendingStartAttempt = _sessionStore._pendingStartAttempt;
    _consentRevocationPending = _sessionStore._consentRevocationPending;
    if (_proof?.isExpiredAt(_now().toUtc()) ?? false) {
      _proof = null;
      _persistSession();
    }
  }

  void _persistSession() {
    final ownerUid = _ownerUid;
    if (ownerUid == null || ownerUid != _currentAuthority) return;
    if (_sessionStore._ownerUid != ownerUid) {
      _sessionStore.clear();
      _sessionStore._ownerUid = ownerUid;
    }
    _sessionStore._status = _status;
    _sessionStore._proof = _proof;
    _sessionStore._consentPolicy = _consentPolicy;
    _sessionStore._pendingStartAttempt = _pendingStartAttempt;
    _sessionStore._consentRevocationPending = _consentRevocationPending;
  }

  void _replaceAuthority(String? authority, ImessageEnrollmentFailure failure) {
    _authorityEpoch += 1;
    _mutationRevision += 1;
    _activeOperation = ++_operationSequence;
    _sessionStore.clear();
    _ownerUid = authority;
    _operation = ImessageEnrollmentOperation.idle;
    _status = null;
    _proof = null;
    _consentPolicy = null;
    _pendingStartAttempt = null;
    _consentRevocationPending = false;
    _failure = failure;
  }
}
