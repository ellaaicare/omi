import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';

const bool isEllaEntitlementGateEnabled = bool.fromEnvironment('ELLA_ENTITLEMENT_GATE', defaultValue: false);
const bool isEllaEntitlementStubEnabled = bool.fromEnvironment('ELLA_ENTITLEMENT_STUBS', defaultValue: false);

enum EllaEntitlementStatus { invited, active, suspended, revoked, expired, none }

enum EllaInviteRedemptionError { invalid, expired, capacity, rateLimited }

enum EllaVoicePolicyReason { quotaDaily, quotaMonthly, concurrent, suspended, sessionMax }

@immutable
class EllaQuota {
  const EllaQuota({
    required this.dailyUsedSeconds,
    required this.dailyLimitSeconds,
    required this.monthlyUsedSeconds,
    required this.monthlyLimitSeconds,
    required this.maxSessionSeconds,
    required this.resetsAt,
  });

  final int dailyUsedSeconds;
  final int dailyLimitSeconds;
  final int monthlyUsedSeconds;
  final int monthlyLimitSeconds;
  final int maxSessionSeconds;
  final DateTime? resetsAt;

  int get dailyRemainingSeconds => (dailyLimitSeconds - dailyUsedSeconds).clamp(0, dailyLimitSeconds);
  int get monthlyRemainingSeconds => (monthlyLimitSeconds - monthlyUsedSeconds).clamp(0, monthlyLimitSeconds);
  int get voiceRemainingSeconds {
    final candidates = [
      if (dailyLimitSeconds > 0) dailyRemainingSeconds,
      if (monthlyLimitSeconds > 0) monthlyRemainingSeconds,
      if (maxSessionSeconds > 0) maxSessionSeconds,
    ];
    if (candidates.isEmpty) return 0;
    return candidates.reduce((left, right) => left < right ? left : right);
  }

  double get dailyFraction => dailyLimitSeconds <= 0 ? 0 : (dailyUsedSeconds / dailyLimitSeconds).clamp(0, 1);
  double get monthlyFraction => monthlyLimitSeconds <= 0 ? 0 : (monthlyUsedSeconds / monthlyLimitSeconds).clamp(0, 1);
  bool get isSoftWarning => dailyFraction >= 0.8 || monthlyFraction >= 0.8;
  bool get isHardStop => dailyFraction >= 1 || monthlyFraction >= 1;

  factory EllaQuota.fromJson(Object? value) {
    final json = value is Map ? value.map((key, value) => MapEntry(key.toString(), value)) : const <String, dynamic>{};
    return EllaQuota(
      dailyUsedSeconds: _intValue(json['daily_used_s']),
      dailyLimitSeconds: _intValue(json['daily_limit_s']),
      monthlyUsedSeconds: _intValue(json['monthly_used_s']),
      monthlyLimitSeconds: _intValue(json['monthly_limit_s']),
      maxSessionSeconds: _intValue(json['max_session_s']),
      resetsAt: DateTime.tryParse(json['resets_at']?.toString() ?? '')?.toLocal(),
    );
  }
}

@immutable
class EllaEntitlement {
  const EllaEntitlement({required this.status, required this.quota});

  final EllaEntitlementStatus status;
  final EllaQuota quota;

  bool get isActive => status == EllaEntitlementStatus.active;

  factory EllaEntitlement.fromJson(Map<String, dynamic> json) {
    final rawStatus = json['status']?.toString().trim().toLowerCase();
    final status = EllaEntitlementStatus.values.where((value) => value.name == rawStatus).firstOrNull;
    if (status == null) {
      throw const FormatException('Unknown entitlement status');
    }
    return EllaEntitlement(status: status, quota: EllaQuota.fromJson(json['quota']));
  }
}

class EllaInviteRedemptionException implements Exception {
  const EllaInviteRedemptionException(this.reason);

  final EllaInviteRedemptionError reason;
}

abstract class EllaEntitlementTransport {
  Future<EllaEntitlement> fetch();

  Future<EllaEntitlement> redeem(String code);
}

class EllaEntitlementHttpTransport implements EllaEntitlementTransport {
  const EllaEntitlementHttpTransport();

  static const _headers = {'X-Ella-Client': 'ios-app'};

  @override
  Future<EllaEntitlement> fetch() async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl ?? ''}v1/entitlement',
      headers: _headers,
      body: '',
      method: 'GET',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null || response.statusCode != 200) {
      throw const FormatException('Entitlement request failed');
    }
    return _decodeEntitlement(response.body);
  }

  @override
  Future<EllaEntitlement> redeem(String code) async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl ?? ''}v1/invite/redeem',
      headers: {..._headers, 'Content-Type': 'application/json'},
      body: jsonEncode({'code': code}),
      method: 'POST',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) {
      throw const FormatException('Invite redemption request failed');
    }
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return _decodeEntitlement(response.body);
    }
    final reason = parseInviteRedemptionError(response.body);
    if (reason != null) throw EllaInviteRedemptionException(reason);
    throw const FormatException('Unknown invite redemption response');
  }
}

EllaEntitlement _decodeEntitlement(String body) {
  final decoded = jsonDecode(body);
  if (decoded is! Map) throw const FormatException('Invalid entitlement response');
  return EllaEntitlement.fromJson(decoded.map((key, value) => MapEntry(key.toString(), value)));
}

@visibleForTesting
EllaInviteRedemptionError? parseInviteRedemptionError(String body) {
  Object? decoded;
  try {
    decoded = jsonDecode(body);
  } catch (_) {
    return null;
  }
  if (decoded is! Map) return null;
  final detail = decoded['detail'];
  final error = decoded['error'];
  final rawCode = switch ((detail, error)) {
    (Map detail, _) => detail['code'],
    (_, Map error) => error['code'],
    (String detail, _) => detail,
    (_, String error) => error,
    _ => decoded['code'],
  };
  return switch (rawCode?.toString().trim().toLowerCase()) {
    'invalid' => EllaInviteRedemptionError.invalid,
    'expired' => EllaInviteRedemptionError.expired,
    'capacity' => EllaInviteRedemptionError.capacity,
    'rate_limited' => EllaInviteRedemptionError.rateLimited,
    _ => null,
  };
}

EllaVoicePolicyReason? parseEllaVoicePolicyReason(Object? value) => switch (value?.toString().trim().toLowerCase()) {
      'quota_daily' => EllaVoicePolicyReason.quotaDaily,
      'quota_monthly' => EllaVoicePolicyReason.quotaMonthly,
      'concurrent' => EllaVoicePolicyReason.concurrent,
      'suspended' => EllaVoicePolicyReason.suspended,
      'session_max' => EllaVoicePolicyReason.sessionMax,
      _ => null,
    };

String ellaVoicePolicyReasonCode(EllaVoicePolicyReason reason) => switch (reason) {
      EllaVoicePolicyReason.quotaDaily => 'quota_daily',
      EllaVoicePolicyReason.quotaMonthly => 'quota_monthly',
      EllaVoicePolicyReason.concurrent => 'concurrent',
      EllaVoicePolicyReason.suspended => 'suspended',
      EllaVoicePolicyReason.sessionMax => 'session_max',
    };

int _intValue(Object? value) {
  if (value is int) return value.clamp(0, 1 << 31);
  if (value is num) return value.round().clamp(0, 1 << 31);
  return int.tryParse(value?.toString() ?? '')?.clamp(0, 1 << 31) ?? 0;
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
