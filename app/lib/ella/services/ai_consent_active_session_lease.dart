import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

class AiConsentAuthorityLostException implements Exception {
  const AiConsentAuthorityLostException();

  @override
  String toString() => 'AI processing permission could not be verified';
}

enum AiConsentLeasePhase { inactive, active, retrying, terminal }

@immutable
class AiConsentLeaseDiagnostics {
  const AiConsentLeaseDiagnostics({
    this.phase = AiConsentLeasePhase.inactive,
    this.retryableFailures = 0,
    this.supportCode = '',
    this.lastConfirmedAt,
    this.nextRetryAt,
    this.terminalReason = '',
  });

  final AiConsentLeasePhase phase;
  final int retryableFailures;
  final String supportCode;
  final DateTime? lastConfirmedAt;
  final DateTime? nextRetryAt;
  final String terminalReason;
}

@immutable
class AiConsentAuthoritySnapshot {
  const AiConsentAuthoritySnapshot({
    required this.generation,
    required this.uid,
    required this.verifiedPersonaId,
    required this.profileBindingId,
    required this.receiptId,
    required this.policyVersion,
    required this.processorSetHash,
    required this.scopeVersion,
    required this.scopeHash,
    this.serverDecidedAt,
  });

  final int generation;
  final String uid;
  final String? verifiedPersonaId;
  final String profileBindingId;
  final String receiptId;
  final String policyVersion;
  final String processorSetHash;
  final String scopeVersion;
  final String scopeHash;
  final DateTime? serverDecidedAt;

  static AiConsentAuthoritySnapshot? capture({SharedPreferencesUtil? preferences, String? expectedUid}) {
    final current = preferences ?? SharedPreferencesUtil();
    final uid = current.uid;
    final verifiedPersonaId = current.verifiedPersonaId?.trim();
    final receiptId = current.aiConsentReceiptId;
    final serverDecidedAt = DateTime.tryParse(current.aiConsentServerDecidedAt);
    if (!current.getBool('aiConsentAccepted', defaultValue: false) ||
        uid.isEmpty ||
        (expectedUid != null && uid != expectedUid) ||
        current.aiConsentProfileBindingId.isEmpty ||
        !receiptId.startsWith(SharedPreferencesUtil.currentAiConsentReceiptPrefix) ||
        current.aiConsentReceiptUid != uid ||
        serverDecidedAt == null) {
      return null;
    }
    return AiConsentAuthoritySnapshot(
      generation: current.aiConsentAuthorityGeneration,
      uid: uid,
      verifiedPersonaId: verifiedPersonaId,
      profileBindingId: current.aiConsentProfileBindingId,
      receiptId: receiptId,
      policyVersion: current.aiConsentContractVersion,
      processorSetHash: current.aiConsentProcessorSetHash,
      scopeVersion: current.aiConsentScopeVersion,
      scopeHash: current.aiConsentScopeHash,
      serverDecidedAt: serverDecidedAt,
    );
  }

  bool isCurrent({SharedPreferencesUtil? preferences}) {
    final current = preferences ?? SharedPreferencesUtil();
    if (!current.getBool('aiConsentAccepted', defaultValue: false) ||
        current.uid != uid ||
        current.aiConsentReceiptUid != uid) {
      return false;
    }
    final currentReceiptId = current.aiConsentReceiptId;
    if (currentReceiptId == receiptId) return true;
    final currentDecidedAt = DateTime.tryParse(current.aiConsentServerDecidedAt);
    return currentReceiptId.startsWith(SharedPreferencesUtil.currentAiConsentReceiptPrefix) &&
        currentDecidedAt != null &&
        (serverDecidedAt == null || currentDecidedAt.isAfter(serverDecidedAt!));
  }
}

typedef AiConsentActiveSessionRefresher = Future<AiConsentAuthorityRefreshResult> Function(
  String uid,
  String expectedReceiptId,
  DateTime? expectedServerDecidedAt,
);

/// Keeps server-authoritative AI consent fresh while personal data is actively
/// being streamed. Refreshing one minute before the five-minute TTL leaves room
/// for the bounded backend request without extending stale authority.
class AiConsentActiveSessionLease {
  AiConsentActiveSessionLease({
    required this.uid,
    required FutureOr<void> Function() onAuthorityLost,
    AiConsentAuthoritySnapshot? authority,
    AiConsentActiveSessionRefresher? refreshAuthority,
    SharedPreferencesUtil? preferences,
    DateTime Function()? now,
    Duration gracePeriod = verificationGracePeriod,
  })  : _onAuthorityLost = onAuthorityLost,
        _authority = authority,
        _refreshAuthority = refreshAuthority ??
            ((uid, receiptId, decidedAt) => EllaAiConsentService().refreshActiveSessionAuthority(
                  uid: uid,
                  expectedReceiptId: receiptId,
                  expectedServerDecidedAt: decidedAt,
                )),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _now = now ?? DateTime.now,
        _gracePeriod = gracePeriod;

  static const Duration refreshInterval = Duration(minutes: 4);
  static const Duration refreshLeadTime = Duration(minutes: 1);
  static const Duration verificationGracePeriod = Duration(minutes: 30);
  static const Duration initialRetryDelay = Duration(seconds: 5);
  static const Duration maximumRetryDelay = Duration(minutes: 4);
  static final ValueNotifier<AiConsentLeaseDiagnostics> diagnostics = ValueNotifier<AiConsentLeaseDiagnostics>(
    const AiConsentLeaseDiagnostics(),
  );
  static int _nextDiagnosticOwner = 0;
  static int _diagnosticOwner = 0;

  final String uid;
  final FutureOr<void> Function() _onAuthorityLost;
  final AiConsentActiveSessionRefresher _refreshAuthority;
  final SharedPreferencesUtil _preferences;
  final DateTime Function() _now;
  final Duration _gracePeriod;
  AiConsentAuthoritySnapshot? _authority;
  String _lastServerReceiptId = '';
  DateTime? _lastServerDecidedAt;
  DateTime? _lastConfirmedAt;
  int _retryableFailures = 0;
  final int _diagnosticId = ++_nextDiagnosticOwner;

  Timer? _refreshTimer;
  bool _active = false;
  bool _refreshing = false;
  bool _authorityLossReported = false;

  bool get isActive => _active;
  bool get hasCurrentAuthority => _active && _authority?.isCurrent(preferences: _preferences) == true;

  void start() {
    if (_active) return;
    _authority ??= AiConsentAuthoritySnapshot.capture(preferences: _preferences, expectedUid: uid);
    _active = true;
    if (_authority == null || !_authority!.isCurrent(preferences: _preferences)) {
      unawaited(_loseAuthority('invalid_start_authority'));
      return;
    }
    _lastServerReceiptId = _authority!.receiptId;
    _lastServerDecidedAt = _authority!.serverDecidedAt;
    final now = _now();
    final verificationRemaining = _preferences.aiConsentServerVerificationRemaining;
    _lastConfirmedAt = verificationRemaining == null
        ? now
        : now.subtract(SharedPreferencesUtil.aiConsentServerVerificationTtl - verificationRemaining);
    _diagnosticOwner = _diagnosticId;
    _publishDiagnostics(AiConsentLeasePhase.active);
    _scheduleRefresh();
  }

  void stop() {
    _active = false;
    _refreshTimer?.cancel();
    _refreshTimer = null;
    if (_diagnosticOwner == _diagnosticId && diagnostics.value.phase != AiConsentLeasePhase.terminal) {
      diagnostics.value = const AiConsentLeaseDiagnostics();
      _diagnosticOwner = 0;
    }
  }

  @visibleForTesting
  Future<void> refreshNow() => _refresh();

  @visibleForTesting
  static Duration refreshDelayFor(Duration? remaining) {
    if (remaining == null || remaining <= refreshLeadTime) return Duration.zero;
    final beforeExpiry = remaining - refreshLeadTime;
    return beforeExpiry < refreshInterval ? beforeExpiry : refreshInterval;
  }

  @visibleForTesting
  static Duration retryDelayFor(int retryableFailures) {
    if (retryableFailures <= 1) return initialRetryDelay;
    final exponent = (retryableFailures - 1).clamp(0, 6).toInt();
    final multiplier = 1 << exponent;
    final delay = initialRetryDelay * multiplier;
    return delay > maximumRetryDelay ? maximumRetryDelay : delay;
  }

  void _scheduleRefresh() {
    if (!_active) return;
    _refreshTimer?.cancel();
    final delay = refreshDelayFor(_preferences.aiConsentServerVerificationRemaining);
    _refreshTimer = Timer(delay, () {
      unawaited(_refresh());
    });
  }

  Future<void> _refresh() async {
    if (!_active || _refreshing) return;
    _refreshing = true;
    _refreshTimer?.cancel();
    _refreshTimer = null;

    final authority = _authority;
    if (authority == null || !authority.isCurrent(preferences: _preferences)) {
      await _loseAuthority('local_authority_changed');
      return;
    }

    AiConsentAuthorityRefreshResult result;
    try {
      result = await _refreshAuthority(uid, _lastServerReceiptId, _lastServerDecidedAt);
    } catch (error) {
      Logger.debug('[AIConsent] Active-session authority refresh failed: ${error.runtimeType}');
      result = const AiConsentAuthorityRefreshResult(
        AiConsentAuthorityRefreshDisposition.retryable,
        supportCode: 'refresh_exception',
      );
    } finally {
      _refreshing = false;
    }

    if (!_active) return;
    if (!result.verified && !result.retryable) {
      await _loseAuthority('explicit_${result.disposition.name}');
      return;
    }
    if (!authority.isCurrent(preferences: _preferences)) {
      await _loseAuthority('local_authority_changed');
      return;
    }

    if (result.verified) {
      final status = result.status;
      if (status != null) {
        _lastServerReceiptId = status.receiptId;
        _lastServerDecidedAt = status.serverDecidedAt;
      }
      _lastConfirmedAt = _now();
      _retryableFailures = 0;
      _publishDiagnostics(AiConsentLeasePhase.active);
      unawaited(DebugLogManager.logEvent('ai_consent_active_session_refreshed', {'uid_matches': true}));
      _scheduleRefresh();
      return;
    }

    if (result.retryable) {
      _retryableFailures++;
      final confirmedAt = _lastConfirmedAt;
      final elapsed = confirmedAt == null ? _gracePeriod : _now().difference(confirmedAt);
      if (elapsed < _gracePeriod) {
        final retryDelay = retryDelayFor(_retryableFailures);
        _publishDiagnostics(
          AiConsentLeasePhase.retrying,
          supportCode: result.supportCode,
          nextRetryAt: _now().add(retryDelay),
        );
        unawaited(
          DebugLogManager.logWarning('ai_consent_active_session_refresh_retry', {
            'attempt': _retryableFailures,
            'retry_delay_seconds': retryDelay.inSeconds,
            'support_code': result.supportCode,
          }),
        );
        _refreshTimer = Timer(retryDelay, () => unawaited(_refresh()));
        return;
      }
      await _loseAuthority('verification_grace_expired');
      return;
    }
  }

  Future<void> _loseAuthority(String reason) async {
    if (_preferences.uid == uid) SharedPreferencesUtil.clearAiConsentServerVerification();
    _diagnosticOwner = _diagnosticId;
    diagnostics.value = AiConsentLeaseDiagnostics(
      phase: AiConsentLeasePhase.terminal,
      retryableFailures: _retryableFailures,
      lastConfirmedAt: _lastConfirmedAt,
      terminalReason: reason,
    );
    stop();
    if (_authorityLossReported) return;
    _authorityLossReported = true;
    unawaited(DebugLogManager.logWarning('ai_consent_active_session_stopped', {'reason': reason}));
    await _onAuthorityLost();
  }

  void _publishDiagnostics(AiConsentLeasePhase phase, {String supportCode = '', DateTime? nextRetryAt}) {
    if (_diagnosticOwner != _diagnosticId) return;
    diagnostics.value = AiConsentLeaseDiagnostics(
      phase: phase,
      retryableFailures: _retryableFailures,
      supportCode: supportCode,
      lastConfirmedAt: _lastConfirmedAt,
      nextRetryAt: nextRetryAt,
    );
  }
}
