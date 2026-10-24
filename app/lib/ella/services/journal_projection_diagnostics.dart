import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

const journalDiagnosticConversationId = '9152000e-71e8-4bf0-9b56-c8d6b94ae17b';
typedef JournalDiagnosticTransport = Future<http.Response?> Function(
    String url, ExactAccountAuthorityVerifier authority);

Future<http.Response?> _read(String url, ExactAccountAuthorityVerifier authority) => makeApiCall(
    url: url,
    headers: const {},
    method: 'GET',
    body: '',
    timeout: const Duration(seconds: 15),
    retries: 0,
    retryOnUnauthorized: false,
    enforceAbsoluteTimeout: true,
    expectedAuthenticatedUid: authority.uid,
    exactAuthority: authority);

Map<String, Object?> _projection(Object? value, String owner) {
  if (value is! Map || value['id'] != journalDiagnosticConversationId) return {'result': 'not_found'};
  if (value.containsKey('uid') && value['uid'] != owner) return {'result': 'owner_mismatch'};
  final structured = value['structured'];
  final title = structured is Map ? structured['title'] : null;
  final overview = structured is Map ? structured['overview'] : null;
  final version = value['active_summary_version_id'];
  if (title is! String ||
      overview is! String ||
      title.length > 256 ||
      overview.length > 8000 ||
      (version != null && (version is! String || version.length > 128))) return {'result': 'invalid_projection'};
  final versions = value['summary_versions'];
  final selected = versions is List
      ? versions.where((v) => v is Map && v['id'] == version && v['is_active'] == true).toList()
      : const [];
  return {
    'result': 'observed',
    'conversation_id': journalDiagnosticConversationId,
    'title': title,
    'overview': overview,
    'pair_sha256': sha256.convert(utf8.encode(jsonEncode({'overview': overview, 'title': title}))).toString(),
    'active_summary_version_id': version,
    'active_summary_matches_structured':
        selected.length == 1 && selected.single['title'] == title && selected.single['overview'] == overview,
  };
}

/// Two bounded GETs through normal app authentication. Only the root-authorized
/// synthetic conversation can be retained; headers, tokens and other rows never
/// enter the receipt. This does not mutate the provider or infer enrichment.
Future<Map<String, Object?>> readJournalProjectionDiagnostics(ExactAccountAuthorityVerifier authority,
    {JournalDiagnosticTransport? transport}) async {
  final send = transport ?? _read;
  void check() {
    if (!authority.isExactCurrent()) throw StateError('retired');
  }

  check();
  final result = <String, Object?>{
    'version': 1,
    'conversation_id': journalDiagnosticConversationId,
    'list_scope': 'first_50_include_discarded',
    'read_at_utc': DateTime.now().toUtc().toIso8601String(),
  };
  for (final kind in ['list', 'detail']) {
    check();
    final url = kind == 'list'
        ? '${Env.apiBaseUrl}v1/conversations?include_discarded=true&limit=50&offset=0&statuses='
        : '${Env.apiBaseUrl}v1/conversations/$journalDiagnosticConversationId';
    final response = await send(url, authority);
    check();
    final receipt = <String, Object?>{'http_status': response?.statusCode, 'result': 'transport_unavailable'};
    if (response != null) {
      receipt['result'] = 'non_200';
      if (response.statusCode == 200) {
        try {
          if (response.bodyBytes.length > 2000000) throw const FormatException();
          final body = jsonDecode(utf8.decode(response.bodyBytes));
          Object? row = body;
          if (kind == 'list') {
            if (body is! List) throw const FormatException();
            final matches = body.where((v) => v is Map && v['id'] == journalDiagnosticConversationId).toList();
            if (matches.length > 1) throw const FormatException();
            row = matches.isEmpty ? null : matches.single;
          }
          receipt.addAll(_projection(row, authority.uid));
        } catch (_) {
          receipt['result'] = 'invalid_response';
        }
      }
    }
    result[kind] = receipt;
  }
  check();
  return result;
}
