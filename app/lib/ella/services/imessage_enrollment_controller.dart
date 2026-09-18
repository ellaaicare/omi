import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';

typedef ImessageAuthorityReader = String? Function();
typedef ImessageMessagesLauncher = Future<bool> Function(Uri uri);
typedef ImessageIdGenerator = String Function();
typedef ImessageAppInfoReader = Future<({String version, String buildNumber})> Function();
typedef _ImessageAuthorityLease = ({String uid, int epoch});

class ImessageEnrollmentController extends ChangeNotifier {
  ImessageEnrollmentController({
    required ImessageEnrollmentGateway gateway,
    required ImessageConsentGateway consentGateway,
    required ImessageAuthorityReader authorityReader,
    required ImessageMessagesLauncher messagesLauncher,
    required ImessageIdGenerator idGenerator,
    required ImessageAppInfoReader appInfoReader,
    DateTime Function()? now,
  })  : _gateway = gateway,
        _consentGateway = consentGateway,
        _authorityReader = authorityReader,
        _messagesLauncher = messagesLauncher,
        _idGenerator = idGenerator,
        _appInfoReader = appInfoReader,
        _now = now ?? DateTime.now;

  final ImessageEnrollmentGateway _gateway;
  final ImessageConsentGateway _consentGateway;
  final ImessageAuthorityReader _authorityReader;
  final ImessageMessagesLauncher _messagesLauncher;
  final ImessageIdGenerator _idGenerator;
  final ImessageAppInfoReader _appInfoReader;
  final DateTime Function() _now;

  String? _ownerUid;
  int _authorityEpoch = 0;
  ImessageEnrollmentStatus? _status;
  ImessageEnrollmentProof? _proof;
  ImessageConsentPolicy? _consentPolicy;
  ImessageEnrollmentFailure? _failure;
  bool _loading = false;

  ImessageEnrollmentStatus? get status => _ownsCurrentAuthority ? _status : null;
  ImessageEnrollmentProof? get proof => _ownsCurrentAuthority ? _proof : null;
  ImessageConsentPolicy? get consentPolicy => _ownsCurrentAuthority ? _consentPolicy : null;
  ImessageEnrollmentFailure? get failure => _ownsCurrentAuthority ? _failure : null;
  bool get loading => _ownsCurrentAuthority && _loading;

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
    await _run((_) => _gateway.fetchStatus(), clearProof: true);
  }

  Future<void> loadConsentPolicy() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    _begin();
    try {
      final policy = await _consentGateway.fetchConsentPolicy();
      _assertAuthority(lease);
      _consentPolicy = policy;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease);
    }
  }

  Future<void> start(String handsetE164) async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    final policy = _consentPolicy;
    if (policy == null) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.consentUnavailable,
        code: 'imessage_consent_policy_missing',
      );
      notifyListeners();
      return;
    }
    _begin();
    _proof = null;
    try {
      final receipt = await _recordConsent(lease, ImessageConsentDecision.granted, policy);
      _assertAuthority(lease);
      if (!receipt.matches(policy, ImessageConsentDecision.granted)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_consent_receipt_mismatch',
        );
      }
      final response = await _gateway.start(
        handsetE164: handsetE164,
        consentReceiptId: receipt.receiptId,
        idempotencyKey: _idGenerator(),
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
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease);
    }
  }

  Future<void> declineConsent() async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    final policy = _consentPolicy;
    if (policy == null) {
      _consentPolicy = null;
      return;
    }
    _begin();
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
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease);
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
      notifyListeners();
      return false;
    }

    final uri = Uri(
      scheme: 'sms',
      path: currentStatus!.assignedDestination,
      queryParameters: {'body': currentProof.code},
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
    final generation = _status?.authorityGeneration ?? 0;
    if (generation < 1) {
      _failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'missing_authority_generation',
      );
      notifyListeners();
      return;
    }
    _begin();
    try {
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
      final response = await _gateway.revoke(
        expectedGeneration: generation,
        idempotencyKey: _idGenerator(),
      );
      _assertAuthority(lease);
      _status = response;
      _proof = null;
      _consentPolicy = null;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease);
    }
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

  Future<void> _run(
    Future<ImessageEnrollmentStatus> Function(_ImessageAuthorityLease lease) operation, {
    required bool clearProof,
  }) async {
    final lease = _readAuthorityOrFail();
    if (lease == null) return;
    _begin();
    try {
      final response = await operation(lease);
      _assertAuthority(lease);
      _status = response;
      if (clearProof) _proof = null;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(lease, error);
    } catch (_) {
      _commitFailureIfCurrent(lease, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(lease);
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
    }
    return (uid: authority, epoch: _authorityEpoch);
  }

  void _assertAuthority(_ImessageAuthorityLease expected) {
    if (!_isLeaseCurrent(expected)) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
  }

  void _begin() {
    _loading = true;
    _failure = null;
    notifyListeners();
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

  void _finishIfCurrent(_ImessageAuthorityLease lease) {
    if (_isLeaseCurrent(lease)) {
      _loading = false;
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

  String? get _currentAuthority {
    final authority = _authorityReader();
    if (authority == null || authority.isEmpty) return null;
    return authority;
  }

  bool get _ownsCurrentAuthority => _ownerUid == _currentAuthority;

  bool _isLeaseCurrent(_ImessageAuthorityLease lease) {
    return lease.uid == _ownerUid && lease.uid == _currentAuthority && lease.epoch == _authorityEpoch;
  }

  void _replaceAuthority(String? authority, ImessageEnrollmentFailure failure) {
    _authorityEpoch += 1;
    _ownerUid = authority;
    _loading = false;
    _status = null;
    _proof = null;
    _consentPolicy = null;
    _failure = failure;
  }
}
