import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

const memoryArtworkSchemaVersion = 'ella.memory_artwork.v1';
const memoryArtworkDefaultStyle = 'ella.memory_artwork.style.soft-gouache.v1';
const memoryArtworkPaperCollageStyle = 'ella.memory_artwork.style.paper-collage.v1';
const memoryArtworkGraphicLandscapeStyle = 'ella.memory_artwork.style.graphic-landscape.v1';

typedef MemoryArtworkHttpCall = Future<http.Response?> Function({
  required String url,
  required Map<String, String> headers,
  required String body,
  required String method,
  Duration? timeout,
  int? retries,
  bool? requireAuthCheck,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});
typedef MemoryArtworkAuthorityProvider = ExactAccountAuthorityVerifier? Function();

enum MemoryArtworkResultStatus { generating, ready, unavailable, declined }

class MemoryArtworkResult {
  const MemoryArtworkResult({
    required this.status,
    this.url,
    this.cacheKey = '',
    this.styleVersion = '',
    this.enrichmentRevision = '',
    this.failureCode = '',
  });

  final MemoryArtworkResultStatus status;
  final Uri? url;
  final String cacheKey;
  final String styleVersion;
  final String enrichmentRevision;
  final String failureCode;

  bool get isReady => status == MemoryArtworkResultStatus.ready && url != null;
}

class MemoryArtworkPreferences {
  const MemoryArtworkPreferences({
    required this.consent,
    required this.consentVersion,
    required this.styleVersion,
    required this.releaseEnabled,
  });

  final String consent;
  final String consentVersion;
  final String styleVersion;
  final bool releaseEnabled;
}

class MemoryArtworkApi {
  MemoryArtworkApi({
    MemoryArtworkHttpCall request = makeApiCall,
    MemoryArtworkAuthorityProvider authorityProvider = WalOwnerAuthority.active,
    String? baseUrl,
  })  : _request = request,
        _authorityProvider = authorityProvider,
        _baseUrl = baseUrl?.trim() ?? '';

  final MemoryArtworkHttpCall _request;
  final MemoryArtworkAuthorityProvider _authorityProvider;
  final String _baseUrl;

  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty || !authority.isExactCurrent()) return '';
    return _cacheKey(
      authority: authority,
      memoryId: memoryId,
      styleVersion: styleVersion,
      enrichmentRevision: enrichmentRevision,
    );
  }

  Future<MemoryArtworkResult> fetch(String memoryId) async {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty) return _unavailable('memory_artwork_authority_unavailable');
    return _fetchWithAuthority(authority, memoryId);
  }

  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty) return _unavailable('memory_artwork_authority_unavailable');

    var result = await _fetchWithAuthority(authority, memoryId);
    if (result.isReady || result.status == MemoryArtworkResultStatus.declined || !authority.isExactCurrent()) {
      return result;
    }
    if (enqueueIfMissing) {
      result = await _enqueueWithAuthority(authority, memoryId);
      if (result.status != MemoryArtworkResultStatus.generating || !authority.isExactCurrent()) {
        return result;
      }
    }
    if (result.status != MemoryArtworkResultStatus.generating) return result;

    for (var attempt = 0; attempt < pollAttempts && authority.isExactCurrent(); attempt++) {
      await Future<void>.delayed(pollInterval);
      if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
      result = await _fetchWithAuthority(authority, memoryId);
      if (result.status != MemoryArtworkResultStatus.generating) return result;
    }
    return result;
  }

  Future<MemoryArtworkResult> _fetchWithAuthority(ExactAccountAuthorityVerifier authority, String memoryId) async {
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    final response = await _call(
      authority,
      method: 'GET',
      path: 'v1/ella/memories/${Uri.encodeComponent(memoryId)}/artwork',
    );
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    if (response == null || response.statusCode != 200) return _unavailable(_safeFailureCode(response?.body));
    final payload = _jsonObject(response.body);
    if (payload == null || payload['schema_version'] != memoryArtworkSchemaVersion) {
      return _unavailable('memory_artwork_response_invalid');
    }
    final status = MemoryArtworkResultStatus.values.asNameMap()[payload['status']?.toString() ?? ''] ??
        MemoryArtworkResultStatus.unavailable;
    final rawUrl = payload['url']?.toString().trim() ?? '';
    final url = Uri.tryParse(rawUrl);
    if (status == MemoryArtworkResultStatus.ready && (url == null || url.scheme != 'https' || url.host.isEmpty)) {
      return _unavailable('memory_artwork_url_invalid');
    }
    return MemoryArtworkResult(
      status: status,
      url: status == MemoryArtworkResultStatus.ready ? url : null,
      cacheKey: status == MemoryArtworkResultStatus.ready
          ? _cacheKey(
              authority: authority,
              memoryId: memoryId,
              styleVersion: payload['style_version']?.toString().trim() ?? '',
              enrichmentRevision: payload['enrichment_revision']?.toString().trim() ?? '',
            )
          : '',
      styleVersion: payload['style_version']?.toString().trim() ?? '',
      enrichmentRevision: payload['enrichment_revision']?.toString().trim() ?? '',
      failureCode: payload['failure_code']?.toString().trim() ?? '',
    );
  }

  Future<MemoryArtworkResult> _enqueueWithAuthority(ExactAccountAuthorityVerifier authority, String memoryId) async {
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    final response = await _call(
      authority,
      method: 'POST',
      path: 'v1/ella/memories/${Uri.encodeComponent(memoryId)}/artwork',
    );
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    if (response == null || response.statusCode < 200 || response.statusCode >= 300) {
      return _unavailable(_safeFailureCode(response?.body));
    }
    final payload = _jsonObject(response.body);
    final status = MemoryArtworkResultStatus.values.asNameMap()[payload?['status']?.toString() ?? ''] ??
        MemoryArtworkResultStatus.unavailable;
    return MemoryArtworkResult(
      status: status,
      failureCode: status == MemoryArtworkResultStatus.unavailable
          ? (payload?['outcome']?.toString().trim() ?? 'memory_artwork_unavailable')
          : '',
    );
  }

  Future<MemoryArtworkPreferences?> preferences() async {
    final authority = _authorityProvider();
    if (authority == null) return null;
    final response = await _call(authority, method: 'GET', path: 'v1/ella/memory-artwork/preferences');
    if (response == null || response.statusCode != 200) return null;
    final payload = _jsonObject(response.body);
    if (payload == null || payload['schema_version'] != memoryArtworkSchemaVersion) return null;
    return MemoryArtworkPreferences(
      consent: payload['consent']?.toString().trim() ?? 'not_set',
      consentVersion: payload['consent_version']?.toString().trim() ?? '',
      styleVersion: payload['style_version']?.toString().trim() ?? memoryArtworkDefaultStyle,
      releaseEnabled: payload['release_enabled'] == true,
    );
  }

  Future<bool> setStyle({required String consentVersion, required String styleVersion}) async {
    final authority = _authorityProvider();
    if (authority == null || consentVersion.isEmpty || !_supportedStyles.contains(styleVersion)) return false;
    final response = await _call(
      authority,
      method: 'PUT',
      path: 'v1/ella/memory-artwork/preferences',
      body: jsonEncode({'consent': 'accepted', 'consent_version': consentVersion, 'style_version': styleVersion}),
    );
    return response?.statusCode == 200;
  }

  Future<bool> backfillRecent() async {
    final authority = _authorityProvider();
    if (authority == null) return false;
    final response = await _call(authority, method: 'POST', path: 'v1/ella/memory-artwork/backfill');
    return response?.statusCode == 200;
  }

  Future<http.Response?> _call(
    ExactAccountAuthorityVerifier authority, {
    required String method,
    required String path,
    String body = '',
  }) {
    final normalizedBase = _resolvedBaseUrl();
    if (normalizedBase.isEmpty) return Future<http.Response?>.value(null);
    final base = normalizedBase.endsWith('/') ? normalizedBase : '$normalizedBase/';
    return _request(
      url: '$base$path',
      headers: const {'Accept': 'application/json'},
      body: body,
      method: method,
      timeout: const Duration(seconds: 15),
      retries: 0,
      requireAuthCheck: true,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
    );
  }

  String _resolvedBaseUrl() {
    if (_baseUrl.isNotEmpty) return _baseUrl;
    try {
      return Env.apiBaseUrl?.trim() ?? '';
    } catch (_) {
      // Widget tests and signed-out startup can run before Env is initialized.
      return '';
    }
  }

  static Map<String, dynamic>? _jsonObject(String body) {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map ? Map<String, dynamic>.from(decoded) : null;
    } catch (_) {
      return null;
    }
  }

  static String _safeFailureCode(String? body) {
    final payload = body == null ? null : _jsonObject(body);
    final detail = payload?['detail'];
    final raw = detail is Map ? detail['code']?.toString().trim() ?? '' : '';
    return RegExp(r'^[a-z0-9_]{1,80}$').hasMatch(raw) ? raw : 'memory_artwork_unavailable';
  }

  static MemoryArtworkResult _unavailable(String code) =>
      MemoryArtworkResult(status: MemoryArtworkResultStatus.unavailable, failureCode: code);

  static String _cacheKey({
    required ExactAccountAuthorityVerifier authority,
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) {
    final ownerNamespace = authority is ActiveWalAuthority
        ? authority.owner.storageNamespace
        : sha256.convert(utf8.encode(authority.uid)).toString().substring(0, 24);
    return sha256
        .convert(
          utf8.encode('ella-memory-artwork-cache-v1\n$ownerNamespace\n$memoryId\n$styleVersion\n$enrichmentRevision'),
        )
        .toString();
  }
}

const _supportedStyles = {
  memoryArtworkDefaultStyle,
  memoryArtworkPaperCollageStyle,
  memoryArtworkGraphicLandscapeStyle,
};
