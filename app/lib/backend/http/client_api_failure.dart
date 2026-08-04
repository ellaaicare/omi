import 'dart:convert';

/// Sanitized client-facing failure categories. Raw backend payloads must never
/// cross this boundary into user-visible or persisted content.
enum ClientApiFailureKind {
  consentRequired,
  authenticationRequired,
  forbidden,
  updateRequired,
  workspaceRequired,
  accountChanged,
  featureUnavailable,
  unavailable,
  invalidResponse,
}

class ClientApiFailure implements Exception {
  const ClientApiFailure(this.kind, {this.statusCode, this.backendCode, this.retryable = false});

  final ClientApiFailureKind kind;
  final int? statusCode;
  final String? backendCode;
  final bool retryable;

  factory ClientApiFailure.fromHttp({required int statusCode, String body = ''}) {
    final code = _extractBackendCode(body);
    return ClientApiFailure(
      _kindFor(statusCode: statusCode, backendCode: code),
      statusCode: statusCode,
      backendCode: code,
      retryable: statusCode == 408 || statusCode == 429 || statusCode >= 500,
    );
  }

  static ClientApiFailure? fromStreamLine(String line) {
    final separator = line.indexOf(':');
    if (separator < 0) return null;
    final field = line.substring(0, separator).trim().toLowerCase();
    final payload = line.substring(separator + 1).trim();
    if (field == 'error') {
      final code = _extractBackendCode(payload);
      return ClientApiFailure(_kindFor(statusCode: null, backendCode: code), backendCode: code, retryable: true);
    }
    if (field != 'data') return null;

    final lower = payload.toLowerCase();
    if (lower.startsWith('error:') || lower.startsWith('error ')) {
      final code = _extractBackendCode(payload);
      return ClientApiFailure(_kindFor(statusCode: null, backendCode: code), backendCode: code, retryable: true);
    }

    if (!payload.startsWith('{')) return null;
    try {
      final decoded = jsonDecode(payload);
      if (decoded is! Map<String, dynamic> ||
          !const {'error', 'error_code', 'detail', 'code'}.any(decoded.containsKey)) {
        return null;
      }
    } catch (_) {
      return null;
    }
    final code = _extractBackendCode(payload);
    return ClientApiFailure(_kindFor(statusCode: null, backendCode: code), backendCode: code, retryable: true);
  }

  @override
  String toString() => 'ClientApiFailure(kind: $kind, statusCode: $statusCode, retryable: $retryable)';
}

String? _extractBackendCode(String body) {
  if (body.trim().isEmpty) return null;
  final normalized = body.trim();
  try {
    final decoded = jsonDecode(normalized);
    if (decoded is Map<String, dynamic>) {
      for (final key in const ['code', 'error_code', 'detail', 'error']) {
        final value = decoded[key];
        if (value is String) return _normalizeCode(value);
        if (value is Map<String, dynamic>) {
          final nested = value['code'] ?? value['detail'];
          if (nested is String) return _normalizeCode(nested);
        }
      }
    }
  } catch (_) {
    // Non-JSON failures are reduced to an allowlisted code below.
  }
  return _normalizeCode(normalized.replaceFirst(RegExp(r'^error\s*:\s*', caseSensitive: false), ''));
}

String? _normalizeCode(String value) {
  final candidate = value.trim().toLowerCase().replaceAll(RegExp(r'[^a-z0-9_-]'), '_');
  const allowed = {
    'hermes_runtime_required',
    'workspace_required',
    'upgrade_required',
    'update_required',
    'client_update_required',
    'auth_required',
    'authentication_required',
    'forbidden',
    'consent_required',
    'ai_consent_required',
  };
  return allowed.contains(candidate) ? candidate : null;
}

ClientApiFailureKind _kindFor({required int? statusCode, required String? backendCode}) {
  if (statusCode == 426 ||
      const {'upgrade_required', 'update_required', 'client_update_required'}.contains(backendCode)) {
    return ClientApiFailureKind.updateRequired;
  }
  if (const {'hermes_runtime_required', 'workspace_required'}.contains(backendCode)) {
    return ClientApiFailureKind.workspaceRequired;
  }
  if (const {'consent_required', 'ai_consent_required'}.contains(backendCode)) {
    return ClientApiFailureKind.consentRequired;
  }
  if (statusCode == 401 || const {'auth_required', 'authentication_required'}.contains(backendCode)) {
    return ClientApiFailureKind.authenticationRequired;
  }
  if (statusCode == 403 || backendCode == 'forbidden') return ClientApiFailureKind.forbidden;
  if (statusCode != null && statusCode >= 400 && statusCode < 500) return ClientApiFailureKind.invalidResponse;
  return ClientApiFailureKind.unavailable;
}
