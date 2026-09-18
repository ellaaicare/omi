import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';
import 'package:omi/ella/services/imessage_enrollment_attempt_store.dart';

typedef ImessageAuthorityReader = String? Function();
typedef ImessageMessagesLauncher = Future<bool> Function(Uri uri);
typedef ImessageIdGenerator = String Function();
typedef ImessageAppInfoReader = Future<({String version, String buildNumber})> Function();
typedef _ImessageAuthorityLease = ({String uid, int epoch});

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

/// Retains status and one-time proof material only for the current process.
/// Durable replay identities are stored separately in platform secure storage.
class ImessageEnrollmentSessionStore {
  String? _ownerUid;
  ImessageEnrollmentStatus? _status;
  ImessageEnrollmentProof? _proof;
  ImessageConsentPolicy? _consentPolicy;
  ImessagePendingStartAttempt? _pendingStartAttempt;
  ImessagePendingBindingRevoke? _pendingBindingRevoke;
  ImessagePendingConsentRevoke? _pendingConsentRevoke;
  bool _consentRevocationPending = false;

  void clear() {
    _ownerUid = null;
    _status = null;
    _proof = null;
    _consentPolicy = null;
    _pendingStartAttempt = null;
    _pendingBindingRevoke = null;
    _pendingConsentRevoke = null;
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
    ImessageEnrollmentAttemptStore? attemptStore,
    DateTime Function()? now,
  })  : _gateway = gateway,
        _consentGateway = consentGateway,
        _authorityReader = authorityReader,
        _messagesLauncher = messagesLauncher,
        _idGenerator = idGenerator,
        _appInfoReader = appInfoReader,
        _sessionStore = sessionStore ?? ImessageEnrollmentSessionStore(),
        _attemptStore = attemptStore ?? ImessageEnrollmentMemoryAttemptStore(),
        _now = now ?? DateTime.now;

  final ImessageEnrollmentGateway _gateway;
  final ImessageConsentGateway _consentGateway;
  final ImessageAuthorityReader _authorityReader;
  final ImessageMessagesLauncher _messagesLauncher;
  final ImessageIdGenerator _idGenerator;
  final ImessageAppInfoReader _appInfoReader;
  final ImessageEnrollmentSessionStore _sessionStore;
  final ImessageEnrollmentAttemptStore _attemptStore;
  final DateTime Function() _now;

  String? _ownerUid;
  int _authorityEpoch = 0;
  ImessageEnrollmentStatus? _status;
  ImessageEnrollmentProof? _proof;
  ImessageConsentPolicy? _consentPolicy;
  ImessageEnrollmentFailure? _failure;
  ImessagePendingStartAttempt? _pendingStartAttempt;
  ImessagePendingBindingRevoke? _pendingBindingRevoke;
  ImessagePendingConsentRevoke? _pendingConsentRevoke;
  bool _consentRevocationPending = false;
  bool _attemptStoreAvailable = true;
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
        attempt.ownerUid == _ownerUid &&
        attempt.receipt != null;
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
    ImessagePendingStartAttempt? attempt;
    try {
      final retry = _pendingStartAttempt;
      if (retry != null &&
          retry.ownerUid == lease.uid &&
          retry.handsetE164 == handsetE164 &&
          _samePolicy(retry.policy, policy)) {
        attempt = retry;
      } else {
        final appInfo = await _appInfoReader();
        _assertAuthority(lease);
        attempt = ImessagePendingStartAttempt(
          ownerUid: lease.uid,
          handsetE164: handsetE164,
          consentRequestId: _idGenerator(),
          policy: policy,
          appVersion: appInfo.version,
          buildNumber: appInfo.buildNumber,
          idempotencyKey: _idGenerator(),
        );
        _pendingStartAttempt = attempt;
        _persistSession();
        await _persistAttemptJournal(lease);
      }

      var receipt = attempt.receipt;
      if (receipt == null) {
        receipt = await _recordConsentExact(
          lease,
          decision: ImessageConsentDecision.granted,
          policy: attempt.policy,
          requestId: attempt.consentRequestId,
          appVersion: attempt.appVersion,
          buildNumber: attempt.buildNumber,
        );
        _assertAuthority(lease);
        if (!receipt.matches(policy, ImessageConsentDecision.granted)) {
          throw const ImessageEnrollmentFailure(
            ImessageEnrollmentFailureKind.malformedResponse,
            code: 'imessage_consent_receipt_mismatch',
          );
        }
        attempt = attempt.withReceipt(receipt);
        _pendingStartAttempt = attempt;
        _persistSession();
        await _persistAttemptJournal(lease);
      }
      final response = await _gateway.start(
        handsetE164: handsetE164,
        consentReceiptId: receipt.receiptId,
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
      await _persistAttemptJournal(lease);
    } on ImessageEnrollmentFailure catch (error) {
      if (!_isAmbiguousStartFailure(error) && _pendingStartAttempt == attempt) {
        _pendingStartAttempt = null;
        _persistSession();
        await _persistAttemptJournalBestEffort(lease);
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
    if (attempt == null ||
        attempt.ownerUid != lease.uid ||
        attempt.receipt == null ||
        _status?.state != ImessageEnrollmentState.verificationPending) {
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
        consentReceiptId: attempt.receipt!.receiptId,
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
      await _persistAttemptJournal(lease);
    } on ImessageEnrollmentFailure catch (error) {
      if (!_isAmbiguousStartFailure(error)) {
        _pendingStartAttempt = null;
        _persistSession();
        await _persistAttemptJournalBestEffort(lease);
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
      await _persistAttemptJournal(lease);
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
    final existingAttempt = _pendingBindingRevoke;
    if (existingAttempt != null &&
        (existingAttempt.ownerUid != lease.uid || existingAttempt.expectedGeneration != generation)) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'imessage_revoke_reconciliation_required',
      );
      notifyListeners();
      return;
    }
    final operation = _begin(ImessageEnrollmentOperation.revokingBinding, mutation: true);
    try {
      final attempt = existingAttempt ??
          ImessagePendingBindingRevoke(
            ownerUid: lease.uid,
            expectedGeneration: generation,
            idempotencyKey: _idGenerator(),
          );
      _pendingBindingRevoke = attempt;
      _persistSession();
      await _persistAttemptJournal(lease);
      final response = await _gateway.revoke(
        expectedGeneration: attempt.expectedGeneration,
        idempotencyKey: attempt.idempotencyKey,
      );
      _assertAuthority(lease);
      if (!_isBindingRevoked(response)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_revoke_response_incoherent',
        );
      }
      _status = response;
      _proof = null;
      _pendingStartAttempt = null;
      _pendingBindingRevoke = null;
      _consentRevocationPending = true;
      _persistSession();
      await _persistAttemptJournal(lease);
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
    var attempt = _pendingConsentRevoke;
    if (attempt == null) {
      final policy = await _consentGateway.fetchConsentPolicy();
      _assertAuthority(lease);
      final appInfo = await _appInfoReader();
      _assertAuthority(lease);
      attempt = ImessagePendingConsentRevoke(
        ownerUid: lease.uid,
        requestId: _idGenerator(),
        policy: policy,
        appVersion: appInfo.version,
        buildNumber: appInfo.buildNumber,
      );
      _pendingConsentRevoke = attempt;
      _persistSession();
      await _persistAttemptJournal(lease);
    }
    if (attempt.ownerUid != lease.uid) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
    final receipt = await _recordConsentExact(
      lease,
      decision: ImessageConsentDecision.revoked,
      policy: attempt.policy,
      requestId: attempt.requestId,
      appVersion: attempt.appVersion,
      buildNumber: attempt.buildNumber,
    );
    _assertAuthority(lease);
    if (!receipt.matches(attempt.policy, ImessageConsentDecision.revoked)) {
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.malformedResponse,
        code: 'imessage_consent_receipt_mismatch',
      );
    }
    final terminalStatus = await _gateway.fetchStatus();
    _assertAuthority(lease);
    if (!_isConsentRevoked(terminalStatus)) {
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'imessage_consent_revoke_not_committed',
      );
    }
    _status = terminalStatus;
    _consentPolicy = null;
    _pendingConsentRevoke = null;
    _consentRevocationPending = false;
    _persistSession();
    await _persistAttemptJournal(lease);
  }

  Future<ImessageConsentReceipt> _recordConsent(
    _ImessageAuthorityLease lease,
    ImessageConsentDecision decision,
    ImessageConsentPolicy policy,
  ) async {
    final appInfo = await _appInfoReader();
    _assertAuthority(lease);
    return _recordConsentExact(
      lease,
      decision: decision,
      policy: policy,
      requestId: _idGenerator(),
      appVersion: appInfo.version,
      buildNumber: appInfo.buildNumber,
    );
  }

  Future<ImessageConsentReceipt> _recordConsentExact(
    _ImessageAuthorityLease lease, {
    required ImessageConsentDecision decision,
    required ImessageConsentPolicy policy,
    required String requestId,
    required String appVersion,
    required String buildNumber,
  }) async {
    _assertAuthority(lease);
    return _consentGateway.recordConsent(
      decision: decision,
      policy: policy,
      requestId: requestId,
      appVersion: appVersion,
      buildNumber: buildNumber,
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
      await _hydrateAttemptJournal(lease, mutationRevision);
      _assertAuthority(lease);
      if (!_isOperationCurrent(operation) || mutationRevision != _mutationRevision) return;
      final response = await _gateway.fetchStatus();
      _assertAuthority(lease);
      if (!_isOperationCurrent(operation) || mutationRevision != _mutationRevision) return;
      _status = response;
      final preserveProof = _canPreserveProof(previousStatus, previousProof, response);
      if (!preserveProof) {
        _proof = null;
      }
      if (response.state != ImessageEnrollmentState.verificationPending && !_isNotEnrolled(response)) {
        _pendingStartAttempt = null;
      }
      if (_isBindingRevoked(response)) {
        _pendingBindingRevoke = null;
        _pendingStartAttempt = null;
        _consentRevocationPending = true;
      } else if (_isConsentRevoked(response)) {
        _pendingStartAttempt = null;
        _pendingBindingRevoke = null;
        _pendingConsentRevoke = null;
        _consentRevocationPending = false;
        _consentPolicy = null;
      } else {
        _consentRevocationPending = false;
        _pendingConsentRevoke = null;
      }
      final pendingBindingRevoke = _pendingBindingRevoke;
      if (pendingBindingRevoke != null &&
          pendingBindingRevoke.expectedGeneration < response.authorityGeneration &&
          !_isBindingRevoked(response) &&
          !_isConsentRevoked(response)) {
        _pendingBindingRevoke = null;
        _failure = const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.conflict,
          code: 'imessage_revoke_reconciliation_required',
        );
      }
      _persistSession();
      await _persistAttemptJournal(lease);
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

  bool _isNotEnrolled(ImessageEnrollmentStatus status) {
    return status.state == ImessageEnrollmentState.notConnected &&
        status.reason == ImessageEnrollmentReason.notEnrolled;
  }

  bool _isBindingRevoked(ImessageEnrollmentStatus status) {
    return status.state == ImessageEnrollmentState.revoked && status.reason == ImessageEnrollmentReason.bindingRevoked;
  }

  bool _isConsentRevoked(ImessageEnrollmentStatus status) {
    return status.state == ImessageEnrollmentState.revoked && status.reason == ImessageEnrollmentReason.consentRevoked;
  }

  bool _samePolicy(ImessageConsentPolicy left, ImessageConsentPolicy right) {
    return left.policyVersion == right.policyVersion &&
        left.processorSetHash == right.processorSetHash &&
        left.scopeVersion == right.scopeVersion &&
        left.scopeHash == right.scopeHash;
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

  Future<void> _hydrateAttemptJournal(_ImessageAuthorityLease lease, int expectedMutationRevision) async {
    try {
      final journal = await _attemptStore.read(lease.uid);
      _assertAuthority(lease);
      if (expectedMutationRevision != _mutationRevision || journal == null) return;
      if (journal.ownerUid != lease.uid) {
        throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
      }
      _pendingStartAttempt = journal.start;
      _pendingBindingRevoke = journal.bindingRevoke;
      _pendingConsentRevoke = journal.consentRevoke;
      _attemptStoreAvailable = true;
      _persistSession();
    } on ImessageEnrollmentFailure {
      rethrow;
    } catch (_) {
      _attemptStoreAvailable = false;
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.unavailable,
        code: 'imessage_secure_state_unavailable',
      );
    }
  }

  Future<void> _persistAttemptJournal(_ImessageAuthorityLease lease) async {
    _assertAuthority(lease);
    if (!_attemptStoreAvailable) {
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.unavailable,
        code: 'imessage_secure_state_unavailable',
      );
    }
    final journal = ImessageEnrollmentAttemptJournal(
      ownerUid: lease.uid,
      start: _pendingStartAttempt,
      bindingRevoke: _pendingBindingRevoke,
      consentRevoke: _pendingConsentRevoke,
    );
    try {
      if (journal.isEmpty) {
        await _attemptStore.clear(lease.uid);
      } else {
        await _attemptStore.write(journal);
      }
      _assertAuthority(lease);
    } on ImessageEnrollmentFailure {
      rethrow;
    } catch (_) {
      _attemptStoreAvailable = false;
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.unavailable,
        code: 'imessage_secure_state_unavailable',
      );
    }
  }

  Future<void> _persistAttemptJournalBestEffort(_ImessageAuthorityLease lease) async {
    try {
      await _persistAttemptJournal(lease);
    } catch (_) {}
  }

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
    _pendingBindingRevoke = _sessionStore._pendingBindingRevoke;
    _pendingConsentRevoke = _sessionStore._pendingConsentRevoke;
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
    _sessionStore._pendingBindingRevoke = _pendingBindingRevoke;
    _sessionStore._pendingConsentRevoke = _pendingConsentRevoke;
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
    _pendingBindingRevoke = null;
    _pendingConsentRevoke = null;
    _consentRevocationPending = false;
    _attemptStoreAvailable = true;
    _failure = failure;
  }
}
