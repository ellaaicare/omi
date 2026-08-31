import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';

const bool isEllaEntitlementGateEnabled = SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled;
const bool isEllaEntitlementStubConfigured = bool.fromEnvironment('ELLA_ENTITLEMENT_STUBS', defaultValue: false);
const bool isEllaEntitlementStubEnabled = !SharedPreferencesUtil.isPublicBuild && isEllaEntitlementStubConfigured;

enum EllaEntitlementStatus { invited, active, suspended, revoked, expired, none }

enum EllaInviteRedemptionError { invalid, expired, capacity, rateLimited }

/// Safe, user-facing categories for the authenticated entitlement boundary.
/// Raw transport and backend detail must never be rendered into the access UI.
enum EllaEntitlementFailureKind { authenticationRequired, accessDenied, updateRequired, unavailable, invalidResponse }

class EllaEntitlementRequestException implements Exception {
  const EllaEntitlementRequestException(this.kind, {required this.supportCode});

  final EllaEntitlementFailureKind kind;
  final String supportCode;
}

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
  const EllaEntitlement({required this.status, required this.quota, this.supportCode = '', this.correlationId = ''});

  final EllaEntitlementStatus status;
  final EllaQuota quota;
  final String supportCode;
  final String correlationId;

  bool get isActive => status == EllaEntitlementStatus.active;
  bool get canProvision => status == EllaEntitlementStatus.invited || status == EllaEntitlementStatus.active;

  factory EllaEntitlement.fromJson(Map<String, dynamic> json) {
    final rawStatus = json['status']?.toString().trim().toLowerCase();
    final status = EllaEntitlementStatus.values.where((value) => value.name == rawStatus).firstOrNull;
    if (status == null) {
      throw const FormatException('Unknown entitlement status');
    }
    return EllaEntitlement(
      status: status,
      quota: EllaQuota.fromJson(json['quota']),
      supportCode: _safeDiagnosticValue(json, 'support_code'),
      correlationId: _safeDiagnosticValue(json, 'correlation_id'),
    );
  }
}

class EllaInviteRedemptionException implements Exception {
  const EllaInviteRedemptionException(
    this.reason, {
    this.retryAfterSeconds,
    this.supportCode = '',
    this.correlationId = '',
  });

  final EllaInviteRedemptionError reason;
  final int? retryAfterSeconds;
  final String supportCode;
  final String correlationId;
}

@immutable
class EllaInviteRedemptionFailure {
  const EllaInviteRedemptionFailure({
    required this.reason,
    this.retryAfterSeconds,
    this.supportCode = '',
    this.correlationId = '',
  });

  final EllaInviteRedemptionError reason;
  final int? retryAfterSeconds;
  final String supportCode;
  final String correlationId;
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
      url: buildEllaEntitlementUrl(Env.apiBaseUrl ?? ''),
      headers: _headers,
      body: '',
      method: 'GET',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) {
      throw classifyEllaEntitlementFailure(isSignedIn: AuthService.instance.isSignedIn());
    }
    if (response.statusCode != 200) {
      throw classifyEllaEntitlementFailure(statusCode: response.statusCode, body: response.body);
    }
    try {
      return _decodeEntitlement(response.body);
    } on FormatException {
      throw classifyEllaEntitlementFailure(
        statusCode: response.statusCode,
        body: response.body,
        invalidResponse: true,
      );
    }
  }

  @override
  Future<EllaEntitlement> redeem(String code) async {
    final response = await makeApiCall(
      url: buildEllaInviteRedemptionUrl(Env.apiBaseUrl ?? ''),
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
    final failure = parseInviteRedemptionFailure(response.body);
    if (failure != null) {
      throw EllaInviteRedemptionException(
        failure.reason,
        retryAfterSeconds: failure.retryAfterSeconds,
        supportCode: failure.supportCode,
        correlationId: failure.correlationId,
      );
    }
    throw const FormatException('Unknown invite redemption response');
  }
}

@visibleForTesting
String buildEllaEntitlementUrl(String baseUrl) => '${baseUrl.endsWith('/') ? baseUrl : '$baseUrl/'}v1/entitlement';

@visibleForTesting
String buildEllaInviteRedemptionUrl(String baseUrl) =>
    '${baseUrl.endsWith('/') ? baseUrl : '$baseUrl/'}v1/invite/redeem';

@visibleForTesting
EllaEntitlementRequestException classifyEllaEntitlementFailure({
  int? statusCode,
  String body = '',
  bool isSignedIn = true,
  bool invalidResponse = false,
}) {
  final failure = invalidResponse
      ? const ClientApiFailure(ClientApiFailureKind.invalidResponse)
      : statusCode == null
          ? (isSignedIn
              ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
              : const ClientApiFailure(ClientApiFailureKind.authenticationRequired))
          : ClientApiFailure.fromHttp(statusCode: statusCode, body: body);
  final kind = switch (failure.kind) {
    ClientApiFailureKind.authenticationRequired => EllaEntitlementFailureKind.authenticationRequired,
    ClientApiFailureKind.forbidden => EllaEntitlementFailureKind.accessDenied,
    ClientApiFailureKind.updateRequired => EllaEntitlementFailureKind.updateRequired,
    ClientApiFailureKind.invalidResponse => EllaEntitlementFailureKind.invalidResponse,
    _ => EllaEntitlementFailureKind.unavailable,
  };
  final supportCode = switch (kind) {
    EllaEntitlementFailureKind.authenticationRequired => 'ELLA-AUTH-REFRESH',
    EllaEntitlementFailureKind.accessDenied => 'ELLA-ACCESS-DENIED',
    EllaEntitlementFailureKind.updateRequired => 'ELLA-UPDATE-REQUIRED',
    EllaEntitlementFailureKind.invalidResponse => 'ELLA-ACCESS-RESPONSE',
    EllaEntitlementFailureKind.unavailable => 'ELLA-ACCESS-RETRY',
  };
  return EllaEntitlementRequestException(kind, supportCode: supportCode);
}

EllaEntitlement _decodeEntitlement(String body) {
  final decoded = jsonDecode(body);
  if (decoded is! Map) throw const FormatException('Invalid entitlement response');
  return EllaEntitlement.fromJson(decoded.map((key, value) => MapEntry(key.toString(), value)));
}

@visibleForTesting
EllaInviteRedemptionFailure? parseInviteRedemptionFailure(String body) {
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
  final reason = switch (rawCode?.toString().trim().toLowerCase()) {
    'invalid' => EllaInviteRedemptionError.invalid,
    'expired' => EllaInviteRedemptionError.expired,
    'capacity' => EllaInviteRedemptionError.capacity,
    'rate_limited' => EllaInviteRedemptionError.rateLimited,
    _ => null,
  };
  if (reason == null) return null;
  final json = decoded.map((key, value) => MapEntry(key.toString(), value));
  final retryAfterSeconds = _intValueOrNull(_nestedValue(json, 'retry_after_s'));
  return EllaInviteRedemptionFailure(
    reason: reason,
    retryAfterSeconds: retryAfterSeconds?.clamp(1, 3600),
    supportCode: _safeDiagnosticValue(json, 'support_code'),
    correlationId: _safeDiagnosticValue(json, 'correlation_id'),
  );
}

@visibleForTesting
EllaInviteRedemptionError? parseInviteRedemptionError(String body) => parseInviteRedemptionFailure(body)?.reason;

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

int? _intValueOrNull(Object? value) {
  if (value is int) return value;
  if (value is num) return value.round();
  return int.tryParse(value?.toString() ?? '');
}

Object? _nestedValue(Map<String, dynamic> json, String key) {
  final direct = json[key];
  if (direct != null) return direct;
  for (final nestedKey in ['detail', 'error', 'data']) {
    final nested = json[nestedKey];
    if (nested is Map) {
      final value = _nestedValue(nested.map((key, value) => MapEntry(key.toString(), value)), key);
      if (value != null) return value;
    }
  }
  return null;
}

String _safeDiagnosticValue(Map<String, dynamic> json, String key) {
  final value = _nestedValue(json, key)?.toString().trim() ?? '';
  if (value.length > 64 || !RegExp(r'^[A-Za-z0-9._-]+$').hasMatch(value)) return '';
  return value;
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
