import 'dart:convert';

import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/env/env.dart';

typedef ImessageEnrollmentTransport = Future<http.Response?> Function({
  required String url,
  required Map<String, String> headers,
  required String body,
  required String method,
  Duration? timeout,
  int? retries,
});

abstract interface class ImessageEnrollmentGateway {
  Future<ImessageEnrollmentStatus> fetchStatus();

  Future<ImessageEnrollmentStartResponse> start({
    required String handsetE164,
    required String consentReceiptId,
    required String idempotencyKey,
  });

  Future<ImessageEnrollmentStatus> revoke({required int expectedGeneration, required String idempotencyKey});
}

abstract interface class ImessageConsentGateway {
  Future<ImessageConsentPolicy> fetchConsentPolicy();

  Future<ImessageConsentReceipt> recordConsent({
    required ImessageConsentDecision decision,
    required ImessageConsentPolicy policy,
    required String requestId,
    required String appVersion,
    required String buildNumber,
  });
}

enum ImessageEnrollmentFailureKind {
  unauthenticated,
  conflict,
  rateLimited,
  unavailable,
  malformedResponse,
  transport,
  authorityChanged,
  consentUnavailable,
  messagesUnavailable,
  proofExpired,
}

class ImessageEnrollmentFailure implements Exception {
  const ImessageEnrollmentFailure(this.kind, {this.code, this.supportCode});

  final ImessageEnrollmentFailureKind kind;
  final String? code;
  final String? supportCode;

  @override
  String toString() => 'ImessageEnrollmentFailure($kind, code: $code)';
}

class ImessageEnrollmentApi implements ImessageEnrollmentGateway, ImessageConsentGateway {
  const ImessageEnrollmentApi({ImessageEnrollmentTransport transport = makeApiCall, String? baseUrl})
      : _transport = transport,
        _baseUrl = baseUrl;

  final ImessageEnrollmentTransport _transport;
  final String? _baseUrl;

  static const _path = 'v1/ella/imessage/enrollment';
  static const _consentPath = 'v1/ella/imessage/consent';

  @override
  Future<ImessageConsentPolicy> fetchConsentPolicy() async {
    final response = await _request(method: 'GET', path: '$_consentPath/policy');
    return _decodePolicy(response.body);
  }

  @override
  Future<ImessageConsentReceipt> recordConsent({
    required ImessageConsentDecision decision,
    required ImessageConsentPolicy policy,
    required String requestId,
    required String appVersion,
    required String buildNumber,
  }) async {
    final response = await _request(
      method: 'POST',
      path: _consentPath,
      body: {
        'decision': decision.wireValue,
        'policy_version': policy.policyVersion,
        'processor_set_hash': policy.processorSetHash,
        'scope_version': policy.scopeVersion,
        'scope_hash': policy.scopeHash,
        'request_id': requestId,
        'app_version': appVersion,
        'build_number': buildNumber,
      },
    );
    return _decodeReceipt(response.body);
  }

  @override
  Future<ImessageEnrollmentStatus> fetchStatus() async {
    final response = await _request(method: 'GET', path: _path);
    return _decodeStatus(response.body);
  }

  @override
  Future<ImessageEnrollmentStartResponse> start({
    required String handsetE164,
    required String consentReceiptId,
    required String idempotencyKey,
  }) async {
    final response = await _request(
      method: 'POST',
      path: '$_path/start',
      body: {'handset_e164': handsetE164, 'consent_receipt_id': consentReceiptId, 'idempotency_key': idempotencyKey},
    );
    return _decodeStart(response.body);
  }

  @override
  Future<ImessageEnrollmentStatus> revoke({required int expectedGeneration, required String idempotencyKey}) async {
    final response = await _request(
      method: 'POST',
      path: '$_path/revoke',
      body: {'expected_generation': expectedGeneration, 'idempotency_key': idempotencyKey},
    );
    return _decodeStatus(response.body);
  }

  Future<http.Response> _request({required String method, required String path, Map<String, dynamic>? body}) async {
    http.Response? response;
    try {
      response = await _transport(
        url: '${_baseUrl ?? Env.apiBaseUrl}$path',
        headers: const {'Content-Type': 'application/json'},
        body: body == null ? '' : jsonEncode(body),
        method: method,
        timeout: const Duration(seconds: 15),
        retries: 0,
      );
    } catch (_) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
    }

    if (response == null) {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw _failureFor(response);
    }
    return response;
  }

  ImessageEnrollmentStatus _decodeStatus(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is! Map<String, dynamic>) throw const FormatException('Status is not an object');
      return ImessageEnrollmentStatus.fromJson(decoded);
    } on FormatException {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.malformedResponse);
    }
  }

  ImessageConsentPolicy _decodePolicy(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is! Map<String, dynamic>) throw const FormatException('Policy is not an object');
      return ImessageConsentPolicy.fromJson(decoded);
    } on FormatException {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.malformedResponse);
    }
  }

  ImessageConsentReceipt _decodeReceipt(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is! Map<String, dynamic>) throw const FormatException('Receipt is not an object');
      return ImessageConsentReceipt.fromJson(decoded);
    } on FormatException {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.malformedResponse);
    }
  }

  ImessageEnrollmentStartResponse _decodeStart(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is! Map<String, dynamic>) throw const FormatException('Start response is not an object');
      return ImessageEnrollmentStartResponse.fromJson(decoded);
    } on FormatException {
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.malformedResponse);
    }
  }

  ImessageEnrollmentFailure _failureFor(http.Response response) {
    String? code;
    String? supportCode;
    try {
      final decoded = jsonDecode(response.body);
      if (decoded is Map<String, dynamic> && decoded['detail'] is Map<String, dynamic>) {
        final detail = decoded['detail'] as Map<String, dynamic>;
        code = detail['code'] as String?;
        supportCode = detail['support_code'] as String?;
      }
    } catch (_) {}

    final kind = switch (response.statusCode) {
      401 => ImessageEnrollmentFailureKind.unauthenticated,
      409 => ImessageEnrollmentFailureKind.conflict,
      429 => ImessageEnrollmentFailureKind.rateLimited,
      503 => ImessageEnrollmentFailureKind.unavailable,
      _ => ImessageEnrollmentFailureKind.transport,
    };
    return ImessageEnrollmentFailure(kind, code: code, supportCode: supportCode);
  }
}
