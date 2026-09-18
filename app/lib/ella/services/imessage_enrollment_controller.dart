import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';

typedef ImessageAuthorityReader = String? Function();
typedef ImessageMessagesLauncher = Future<bool> Function(Uri uri);
typedef ImessageIdGenerator = String Function();
typedef ImessageAppInfoReader = Future<({String version, String buildNumber})> Function();

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

  ImessageEnrollmentStatus? status;
  ImessageEnrollmentProof? proof;
  ImessageConsentPolicy? consentPolicy;
  ImessageEnrollmentFailure? failure;
  bool loading = false;

  bool get isReady => status?.isReady ?? false;
  bool get canOpenMessages {
    final currentProof = proof;
    return status?.state == ImessageEnrollmentState.verificationPending &&
        status?.assignedDestination != null &&
        currentProof != null &&
        !currentProof.isExpiredAt(_now().toUtc());
  }

  Future<void> load() async {
    await _run((_) => _gateway.fetchStatus(), clearProof: true);
  }

  Future<void> loadConsentPolicy() async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return;
    _begin();
    try {
      final policy = await _consentGateway.fetchConsentPolicy();
      _assertAuthority(authority);
      consentPolicy = policy;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
    } catch (_) {
      _commitFailureIfCurrent(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(authority);
    }
  }

  Future<void> start(String handsetE164) async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return;
    final policy = consentPolicy;
    if (policy == null) {
      failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.consentUnavailable,
        code: 'imessage_consent_policy_missing',
      );
      notifyListeners();
      return;
    }
    _begin();
    try {
      final receipt = await _recordConsent(ImessageConsentDecision.granted, policy);
      _assertAuthority(authority);
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
      _assertAuthority(authority);
      status = response.status;
      proof = response.proof;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
    } catch (_) {
      _commitFailureIfCurrent(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(authority);
    }
  }

  Future<void> declineConsent() async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return;
    final policy = consentPolicy;
    if (policy == null) {
      consentPolicy = null;
      return;
    }
    _begin();
    try {
      final receipt = await _recordConsent(ImessageConsentDecision.declined, policy);
      _assertAuthority(authority);
      if (!receipt.matches(policy, ImessageConsentDecision.declined)) {
        throw const ImessageEnrollmentFailure(
          ImessageEnrollmentFailureKind.malformedResponse,
          code: 'imessage_consent_receipt_mismatch',
        );
      }
      consentPolicy = null;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
    } catch (_) {
      _commitFailureIfCurrent(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(authority);
    }
  }

  Future<bool> openMessages() async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return false;
    final currentStatus = status;
    final currentProof = proof;
    if (currentStatus?.state != ImessageEnrollmentState.verificationPending ||
        currentStatus?.assignedDestination == null ||
        currentProof == null) {
      failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable);
      notifyListeners();
      return false;
    }
    if (currentProof.isExpiredAt(_now().toUtc())) {
      failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.proofExpired);
      proof = null;
      notifyListeners();
      return false;
    }

    final uri = Uri(
      scheme: 'sms',
      path: currentStatus!.assignedDestination,
      queryParameters: {'body': currentProof.code},
    );
    try {
      final opened = await _messagesLauncher(uri);
      _assertAuthority(authority);
      if (!opened) {
        failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable);
        notifyListeners();
      }
      return opened;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
      notifyListeners();
      return false;
    } catch (_) {
      _commitFailureIfCurrent(
        authority,
        const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.messagesUnavailable),
      );
      notifyListeners();
      return false;
    }
  }

  Future<void> revoke() async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return;
    final generation = status?.authorityGeneration ?? 0;
    if (generation < 1) {
      failure = const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'missing_authority_generation',
      );
      notifyListeners();
      return;
    }
    _begin();
    try {
      final policy = await _consentGateway.fetchConsentPolicy();
      _assertAuthority(authority);
      final receipt = await _recordConsent(ImessageConsentDecision.revoked, policy);
      _assertAuthority(authority);
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
      _assertAuthority(authority);
      status = response;
      proof = null;
      consentPolicy = null;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
    } catch (_) {
      _commitFailureIfCurrent(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(authority);
    }
  }

  Future<ImessageConsentReceipt> _recordConsent(
    ImessageConsentDecision decision,
    ImessageConsentPolicy policy,
  ) async {
    final appInfo = await _appInfoReader();
    return _consentGateway.recordConsent(
      decision: decision,
      policy: policy,
      requestId: _idGenerator(),
      appVersion: appInfo.version,
      buildNumber: appInfo.buildNumber,
    );
  }

  Future<void> _run(
    Future<ImessageEnrollmentStatus> Function(String authority) operation, {
    required bool clearProof,
  }) async {
    final authority = _readAuthorityOrFail();
    if (authority == null) return;
    _begin();
    try {
      final response = await operation(authority);
      _assertAuthority(authority);
      status = response;
      if (clearProof) proof = null;
    } on ImessageEnrollmentFailure catch (error) {
      _commitFailureIfCurrent(authority, error);
    } catch (_) {
      _commitFailureIfCurrent(authority, const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport));
    } finally {
      _finishIfCurrent(authority);
    }
  }

  String? _readAuthorityOrFail() {
    final authority = _authorityReader();
    if (authority == null || authority.isEmpty) {
      loading = false;
      status = null;
      proof = null;
      consentPolicy = null;
      failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.unauthenticated);
      notifyListeners();
      return null;
    }
    return authority;
  }

  void _assertAuthority(String expected) {
    if (_authorityReader() != expected) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
  }

  void _begin() {
    loading = true;
    failure = null;
    notifyListeners();
  }

  void _commitFailureIfCurrent(String authority, ImessageEnrollmentFailure error) {
    if (_authorityReader() == authority) {
      failure = error;
    } else {
      status = null;
      proof = null;
      consentPolicy = null;
      failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
  }

  void _finishIfCurrent(String authority) {
    if (_authorityReader() == authority) {
      loading = false;
    } else {
      loading = false;
      status = null;
      proof = null;
      consentPolicy = null;
      failure = const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.authorityChanged);
    }
    notifyListeners();
  }
}
