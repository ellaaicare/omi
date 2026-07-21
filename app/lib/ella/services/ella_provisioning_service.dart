import 'dart:convert';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';

const bool isHermesProvisioningGateEnabled = bool.fromEnvironment('ELLA_HERMES_PROVISIONING_GATE', defaultValue: true);

const String ellaProvisioningTargetSchema = 'hermes-user-v1';

enum EllaProvisioningState { idle, checking, queued, provisioning, ready, degraded, blocked }

class EllaProvisioningRequestContext {
  EllaProvisioningRequestContext({
    required this.appVersion,
    required this.locale,
    required this.timezone,
    String? clientRequestId,
    this.consentReceiptId = '',
  }) : clientRequestId = clientRequestId ?? const Uuid().v4();

  final String appVersion;
  final String locale;
  final String timezone;
  final String clientRequestId;
  final String consentReceiptId;

  Map<String, dynamic> toJson() => {
        'target_schema_version': ellaProvisioningTargetSchema,
        'client_request_id': clientRequestId,
        'client': {'platform': 'ios', 'app_version': appVersion, 'locale': locale, 'timezone': timezone},
        if (consentReceiptId.isNotEmpty) 'consent_receipt_id': consentReceiptId,
      };

  EllaProvisioningRequestContext copyWithConsentReceiptId(String value) => EllaProvisioningRequestContext(
        appVersion: appVersion,
        locale: locale,
        timezone: timezone,
        clientRequestId: clientRequestId,
        consentReceiptId: value,
      );
}

class EllaProvisioningReceipt {
  const EllaProvisioningReceipt({
    required this.state,
    this.jobId = '',
    this.stage = '',
    this.retryable = false,
    this.retryAfter = const Duration(seconds: 2),
    this.supportCode = '',
    this.targetSchemaVersion = '',
    this.bindingState = '',
    this.bindingRevision = 0,
    this.effectivePolicyRevision = '',
    this.effectiveVoiceMode = '',
    this.effectiveModel = '',
    this.errorCode = '',
  });

  final EllaProvisioningState state;
  final String jobId;
  final String stage;
  final bool retryable;
  final Duration retryAfter;
  final String supportCode;
  final String targetSchemaVersion;
  final String bindingState;
  final int bindingRevision;
  final String effectivePolicyRevision;
  final String effectiveVoiceMode;
  final String effectiveModel;
  final String errorCode;

  bool get isOperational =>
      state == EllaProvisioningState.ready &&
      bindingState.toLowerCase() == 'active' &&
      bindingRevision > 0 &&
      effectivePolicyRevision.isNotEmpty;

  Map<String, dynamic> toCacheJson() => {
        'state': state.name,
        'job_id': jobId,
        'stage': stage,
        'retryable': retryable,
        'retry_after_ms': retryAfter.inMilliseconds,
        'support_code': supportCode,
        'target_schema_version': targetSchemaVersion,
        'binding_state': bindingState,
        'binding_revision': bindingRevision,
        'effective_policy_revision': effectivePolicyRevision,
        'effective_voice_mode': effectiveVoiceMode,
        'effective_model': effectiveModel,
        'error_code': errorCode,
      };

  factory EllaProvisioningReceipt.fromJson(Map<String, dynamic> responseJson) {
    if (_containsForbiddenCredentialField(responseJson)) {
      return const EllaProvisioningReceipt(
        state: EllaProvisioningState.blocked,
        supportCode: 'IOS-UNSAFE-PROVISIONING-CONTRACT',
        errorCode: 'unsafe_response_contract',
      );
    }

    final receipt = _mapValue(responseJson, const ['receipt', 'provisioning']) ?? responseJson;
    final binding = _mapValue(receipt, const ['binding', 'hermes_binding']) ?? const <String, dynamic>{};
    final policy = _mapValue(receipt, const ['effective_policy', 'policy']) ?? const <String, dynamic>{};
    final errorValue = responseJson['error'] ?? receipt['error'];
    final error = errorValue is Map ? errorValue.map((key, value) => MapEntry(key.toString(), value)) : null;
    final detailValue = responseJson['detail'];
    final detail = detailValue is Map ? detailValue.map((key, value) => MapEntry(key.toString(), value)) : null;

    final rawState = _stringValue(receipt, const ['state', 'status']) ??
        _stringValue(responseJson, const ['state', 'status']) ??
        'blocked';
    final retryAfterMs = _intValue(receipt, const ['retry_after_ms', 'retryAfterMs']) ??
        ((_intValue(receipt, const ['retry_after_seconds', 'retry_after', 'retryAfter']) ?? 2) * 1000);

    return EllaProvisioningReceipt(
      state: _parseState(rawState),
      jobId: _stringValue(receipt, const ['job_id', 'jobId']) ?? '',
      stage: _stringValue(receipt, const ['stage']) ?? '',
      retryable: _boolValue(receipt, const ['retryable']) ?? false,
      retryAfter: Duration(milliseconds: retryAfterMs.clamp(500, 15000)),
      supportCode: _stringValue(receipt, const ['support_code', 'supportCode']) ??
          _stringValue(responseJson, const ['support_code', 'supportCode']) ??
          '',
      targetSchemaVersion: _stringValue(receipt, const ['target_schema_version', 'targetSchemaVersion']) ?? '',
      bindingState: _stringValue(binding, const ['state', 'status']) ??
          _stringValue(receipt, const ['binding_state', 'bindingState']) ??
          '',
      bindingRevision: _intValue(binding, const ['revision', 'binding_revision']) ??
          _intValue(receipt, const ['binding_revision', 'bindingRevision']) ??
          0,
      effectivePolicyRevision: _stringValue(policy, const ['revision', 'policy_revision']) ??
          _stringValue(receipt, const ['effective_policy_revision', 'policy_revision', 'policyRevision']) ??
          '',
      effectiveVoiceMode: _stringValue(policy, const ['voice_mode', 'voiceMode']) ??
          _stringValue(receipt, const ['effective_voice_mode', 'voice_mode', 'voiceMode']) ??
          '',
      effectiveModel: _stringValue(policy, const ['model', 'chat_model', 'chatModel']) ??
          _stringValue(receipt, const ['effective_model', 'model', 'chat_model', 'chatModel']) ??
          '',
      errorCode: _stringValue(error, const ['code']) ??
          _stringValue(detail, const ['code']) ??
          (errorValue is String ? errorValue : null) ??
          (detailValue is String ? detailValue : null) ??
          _stringValue(receipt, const ['error_code', 'errorCode']) ??
          '',
    );
  }

  static EllaProvisioningState _parseState(String value) {
    switch (value.toLowerCase().replaceAll('-', '_')) {
      case 'checking':
        return EllaProvisioningState.checking;
      case 'queued':
        return EllaProvisioningState.queued;
      case 'provisioning':
      case 'running':
      case 'in_progress':
        return EllaProvisioningState.provisioning;
      case 'ready':
      case 'complete':
      case 'completed':
        return EllaProvisioningState.ready;
      case 'degraded':
      case 'retrying':
        return EllaProvisioningState.degraded;
      case 'blocked':
      case 'failed':
      case 'error':
        return EllaProvisioningState.blocked;
      default:
        return EllaProvisioningState.blocked;
    }
  }
}

class EllaProvisioningResponse {
  const EllaProvisioningResponse({required this.statusCode, this.receipt});

  final int statusCode;
  final EllaProvisioningReceipt? receipt;

  bool get isAccepted => statusCode == 200 || statusCode == 201 || statusCode == 202;
}

abstract class EllaProvisioningTransport {
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context);

  Future<EllaProvisioningResponse> status();
}

class EllaProvisioningHttpTransport implements EllaProvisioningTransport {
  const EllaProvisioningHttpTransport();

  static const _headers = {'X-Ella-Client': 'ios-app', 'X-Ella-Route-Source': 'authenticated-provisioning'};

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) async {
    final response = await makeApiCall(
      url: buildEllaProvisioningEnsureUrl(Env.apiBaseUrl),
      headers: _headers,
      body: jsonEncode(context.toJson()),
      method: 'POST',
      timeout: const Duration(seconds: 20),
      retries: 0,
    );
    return _decode(response?.statusCode ?? 0, response?.body);
  }

  @override
  Future<EllaProvisioningResponse> status() async {
    final response = await makeApiCall(
      url: buildEllaProvisioningStatusUrl(Env.apiBaseUrl),
      headers: _headers,
      body: '',
      method: 'GET',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    return _decode(response?.statusCode ?? 0, response?.body);
  }

  EllaProvisioningResponse _decode(int statusCode, String? body) {
    if (body == null || body.isEmpty) {
      return EllaProvisioningResponse(statusCode: statusCode);
    }
    try {
      final json = jsonDecode(body);
      if (json is! Map<String, dynamic>) {
        return EllaProvisioningResponse(statusCode: statusCode);
      }
      return EllaProvisioningResponse(statusCode: statusCode, receipt: EllaProvisioningReceipt.fromJson(json));
    } catch (_) {
      return EllaProvisioningResponse(statusCode: statusCode);
    }
  }
}

String buildEllaProvisioningEnsureUrl(String? apiBaseUrl) => '${apiBaseUrl ?? ''}v1/ella/onboarding/ensure';

String buildEllaProvisioningStatusUrl(String? apiBaseUrl) => Uri.parse(
      '${apiBaseUrl ?? ''}v1/ella/onboarding/status',
    ).replace(queryParameters: {'target_schema_version': ellaProvisioningTargetSchema}).toString();

Map<String, dynamic>? _mapValue(Map<String, dynamic> source, List<String> keys) {
  for (final key in keys) {
    final value = source[key];
    if (value is Map<String, dynamic>) return value;
    if (value is Map) return value.map((key, value) => MapEntry(key.toString(), value));
  }
  return null;
}

String? _stringValue(Map<String, dynamic>? source, List<String> keys) {
  if (source == null) return null;
  for (final key in keys) {
    final value = source[key];
    if (value is String && value.trim().isNotEmpty) return value.trim();
  }
  return null;
}

int? _intValue(Map<String, dynamic> source, List<String> keys) {
  for (final key in keys) {
    final value = source[key];
    if (value is int) return value;
    if (value is num) return value.round();
    if (value is String) return int.tryParse(value);
  }
  return null;
}

bool? _boolValue(Map<String, dynamic> source, List<String> keys) {
  for (final key in keys) {
    final value = source[key];
    if (value is bool) return value;
    if (value is String) {
      if (value.toLowerCase() == 'true') return true;
      if (value.toLowerCase() == 'false') return false;
    }
  }
  return null;
}

bool _containsForbiddenCredentialField(Object? value) {
  const forbidden = {
    'token',
    'access_token',
    'gateway_token',
    'gateway_url',
    'credential',
    'credentials',
    'api_key',
    'password',
    'secret',
  };
  if (value is Map) {
    for (final entry in value.entries) {
      final normalizedKey = entry.key
          .toString()
          .replaceAllMapped(RegExp(r'([a-z0-9])([A-Z])'), (match) => '${match.group(1)}_${match.group(2)}')
          .toLowerCase();
      if (forbidden.contains(normalizedKey) ||
          normalizedKey.endsWith('_token') ||
          normalizedKey.endsWith('_secret') ||
          normalizedKey.endsWith('_password') ||
          normalizedKey.endsWith('_api_key')) {
        return true;
      }
      if (_containsForbiddenCredentialField(entry.value)) return true;
    }
  } else if (value is Iterable) {
    for (final item in value) {
      if (_containsForbiddenCredentialField(item)) return true;
    }
  }
  return false;
}
