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

  static AiConsentAuthoritySnapshot? capture({
    SharedPreferencesUtil? preferences,
    String? expectedUid,
  }) {
    final current = preferences ?? SharedPreferencesUtil();
    final uid = current.uid;
    final verifiedPersonaId = current.verifiedPersonaId?.trim();
    if (!current.aiConsentAccepted ||
        uid.isEmpty ||
        (expectedUid != null && uid != expectedUid) ||
        current.aiConsentProfileBindingId.isEmpty) {
      return null;
    }
    return AiConsentAuthoritySnapshot(
      generation: current.aiConsentAuthorityGeneration,
      uid: uid,
      verifiedPersonaId: verifiedPersonaId,
      profileBindingId: current.aiConsentProfileBindingId,
      receiptId: current.aiConsentReceiptId,
      policyVersion: current.aiConsentContractVersion,
      processorSetHash: current.aiConsentProcessorSetHash,
      scopeVersion: current.aiConsentScopeVersion,
      scopeHash: current.aiConsentScopeHash,
    );
  }

  bool isCurrent({SharedPreferencesUtil? preferences}) {
    final current = preferences ?? SharedPreferencesUtil();
    return current.aiConsentAccepted &&
        current.aiConsentAuthorityGeneration == generation &&
        current.uid == uid &&
        current.verifiedPersonaId?.trim() == verifiedPersonaId &&
        current.aiConsentProfileBindingId == profileBindingId &&
        current.aiConsentReceiptId == receiptId &&
        current.aiConsentContractVersion == policyVersion &&
        current.aiConsentProcessorSetHash == processorSetHash &&
        current.aiConsentScopeVersion == scopeVersion &&
        current.aiConsentScopeHash == scopeHash;
  }
}

/// Keeps server-authoritative AI consent fresh while personal data is actively
/// being streamed. Refreshing one minute before the five-minute TTL leaves room
/// for the bounded backend request without extending stale authority.
class AiConsentActiveSessionLease {
  AiConsentActiveSessionLease({
    required this.uid,
    required FutureOr<void> Function() onAuthorityLost,
    AiConsentAuthoritySnapshot? authority,
    Future<bool> Function(String uid)? refreshAuthority,
    SharedPreferencesUtil? preferences,
  })  : _onAuthorityLost = onAuthorityLost,
        _authority = authority,
        _refreshAuthority = refreshAuthority ?? ((uid) => EllaAiConsentService().refreshServerAuthority(uid: uid)),
        _preferences = preferences ?? SharedPreferencesUtil();

  static const Duration refreshInterval = Duration(minutes: 4);
  static const Duration refreshLeadTime = Duration(minutes: 1);

  final String uid;
  final FutureOr<void> Function() _onAuthorityLost;
  final Future<bool> Function(String uid) _refreshAuthority;
  final SharedPreferencesUtil _preferences;
  AiConsentAuthoritySnapshot? _authority;

  Timer? _refreshTimer;
  bool _active = false;
  bool _refreshing = false;
  bool _authorityLossReported = false;

  bool get isActive => _active;

  void start() {
    if (_active) return;
    _authority ??= AiConsentAuthoritySnapshot.capture(preferences: _preferences, expectedUid: uid);
    _active = true;
    if (_authority == null || !_authority!.isCurrent(preferences: _preferences)) {
      unawaited(_loseAuthority('invalid_start_authority'));
      return;
    }
    _scheduleRefresh();
  }

  void stop() {
    _active = false;
    _refreshTimer?.cancel();
    _refreshTimer = null;
  }

  @visibleForTesting
  Future<void> refreshNow() => _refresh();

  @visibleForTesting
  static Duration refreshDelayFor(Duration? remaining) {
    if (remaining == null || remaining <= refreshLeadTime) return Duration.zero;
    final beforeExpiry = remaining - refreshLeadTime;
    return beforeExpiry < refreshInterval ? beforeExpiry : refreshInterval;
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

    var authorized = false;
    try {
      authorized = await _refreshAuthority(uid);
    } catch (error) {
      Logger.debug('[AIConsent] Active-session authority refresh failed: ${error.runtimeType}');
    } finally {
      _refreshing = false;
    }

    if (!_active) return;
    if (!authorized || _authority == null || !_authority!.isCurrent(preferences: _preferences)) {
      await _loseAuthority('refresh_denied_or_unavailable');
      return;
    }

    unawaited(DebugLogManager.logEvent('ai_consent_active_session_refreshed', {'uid_matches': true}));
    _scheduleRefresh();
  }

  Future<void> _loseAuthority(String reason) async {
    SharedPreferencesUtil.clearAiConsentServerVerification();
    stop();
    if (_authorityLossReported) return;
    _authorityLossReported = true;
    unawaited(DebugLogManager.logWarning('ai_consent_active_session_stopped', {'reason': reason}));
    await _onAuthorityLost();
  }
}
