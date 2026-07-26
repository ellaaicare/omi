const String redactedLogValue = '<redacted>';

const Set<String> _sensitiveKeys = {
  'authorization',
  'access_token',
  'id_token',
  'token',
  'refresh_token',
  'code',
  'state',
  'key',
  'api_key',
  'secret',
  'signature',
  'session_token',
};

String redactUrlForLogs(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null) return redactSensitiveLogText(value);
  final sanitizedQuery = <String, String>{};
  for (final entry in uri.queryParameters.entries) {
    sanitizedQuery[entry.key] = _isSensitiveKey(entry.key) ? redactedLogValue : entry.value;
  }
  final sanitized = uri
      .replace(
        userInfo: uri.userInfo.isEmpty ? null : redactedLogValue,
        queryParameters: sanitizedQuery.isEmpty ? null : sanitizedQuery,
        fragment: '',
      )
      .toString();
  return sanitized.endsWith('#') ? sanitized.substring(0, sanitized.length - 1) : sanitized;
}

Map<String, String> redactHeadersForLogs(Map<String, String> headers) => {
      for (final entry in headers.entries)
        entry.key: _isSensitiveKey(entry.key) ? redactedLogValue : redactSensitiveLogText(entry.value),
    };

String redactSensitiveLogText(String value) {
  var result = value.replaceAll(
    RegExp(r'Bearer\s+[A-Za-z0-9._~+/=-]+', caseSensitive: false),
    'Bearer $redactedLogValue',
  );
  result = result.replaceAllMapped(
    RegExp(
      r'([?&](?:authorization|access_token|id_token|token|refresh_token|code|state|key|api_key|secret|signature|session_token)=)[^&#\s]+',
      caseSensitive: false,
    ),
    (match) => '${match.group(1)}$redactedLogValue',
  );
  result = result.replaceAllMapped(
    RegExp(
      r'("(?:authorization|access_token|id_token|token|refresh_token|code|state|key|api_key|secret|signature|session_token)"\s*:\s*")[^"]*(")',
      caseSensitive: false,
    ),
    (match) => '${match.group(1)}$redactedLogValue${match.group(2)}',
  );
  return result;
}

bool _isSensitiveKey(String key) => _sensitiveKeys.contains(key.trim().toLowerCase().replaceAll('-', '_'));
