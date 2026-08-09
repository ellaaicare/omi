import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef TodayCardHttpCall = Future<http.Response?> Function({
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

abstract interface class TodayCardRepository {
  Future<TodayCardResponse> fetch({required String uid});
}

class HttpTodayCardRepository implements TodayCardRepository {
  HttpTodayCardRepository({TodayCardHttpCall request = makeApiCall, String? baseUrl})
      : _request = request,
        _baseUrl = baseUrl ?? Env.apiBaseUrl ?? '';

  final TodayCardHttpCall _request;
  final String _baseUrl;

  @override
  Future<TodayCardResponse> fetch({required String uid}) async {
    final normalizedUid = uid.trim();
    final authority = WalOwnerAuthority.active();
    if (normalizedUid.isEmpty || _baseUrl.trim().isEmpty || authority == null || authority.uid != normalizedUid) {
      return _degraded('today_card_unavailable');
    }
    final base = _baseUrl.endsWith('/') ? _baseUrl : '$_baseUrl/';
    final response = await _request(
      url: '${base}v1/ella/today-card',
      headers: const {'Accept': 'application/json'},
      body: '',
      method: 'GET',
      timeout: const Duration(seconds: 10),
      retries: 0,
      requireAuthCheck: true,
      expectedAuthenticatedUid: normalizedUid,
      exactAuthority: authority,
    );
    if (response == null) return _degraded('today_card_unavailable');
    if (response.statusCode != 200) return _degraded(_safeErrorCode(response.body));

    Object? decoded;
    try {
      decoded = jsonDecode(response.body);
    } catch (_) {
      return _degraded('invalid_today_card_response');
    }
    return parseEnvelope(decoded, headerEtag: response.headers['etag'] ?? '');
  }

  @visibleForTesting
  static TodayCardResponse parseEnvelope(Object? decoded, {String headerEtag = ''}) {
    if (decoded is! Map) return _degraded('invalid_today_card_response');
    final contractVersion = decoded['contract_version']?.toString().trim() ?? '';
    final status = TodayCardStatus.tryParse(decoded['state']?.toString() ?? '');
    final bodyEtag = decoded['etag']?.toString().trim() ?? '';
    final serverTime = DateTime.tryParse(decoded['server_time']?.toString() ?? '');
    final retryAfter = decoded['retry_after_seconds'];
    if (contractVersion.isEmpty || status == null || bodyEtag.isEmpty || serverTime == null) {
      return _degraded('invalid_today_card_response', contractVersion: contractVersion);
    }
    if (headerEtag.isNotEmpty && headerEtag != bodyEtag) {
      return _degraded('invalid_today_card_response', contractVersion: contractVersion);
    }

    final card = decoded['card'] == null ? null : TodayCard.fromApiJson(decoded['card']);
    return TodayCardResponse(
      contractVersion: contractVersion,
      status: status,
      card: card,
      errorCode: decoded['reason_code']?.toString().trim() ?? '',
      etag: bodyEtag,
      serverTime: serverTime,
      retryAfter: retryAfter is num ? Duration(seconds: retryAfter.toInt().clamp(0, 3600)) : Duration.zero,
    );
  }

  static TodayCardResponse _degraded(String code, {String contractVersion = todayCardContractVersion}) =>
      TodayCardResponse(contractVersion: contractVersion, status: TodayCardStatus.degraded, errorCode: code);

  static String _safeErrorCode(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is Map) {
        final detail = decoded['detail'];
        if (detail is Map) {
          final code = detail['code']?.toString().trim() ?? '';
          if (RegExp(r'^[a-z0-9_]{1,80}$').hasMatch(code)) return code;
        }
      }
    } catch (_) {}
    return 'today_card_unavailable';
  }
}

abstract interface class TodayCardCache {
  Future<TodayCard?> read({required String uid});

  Future<void> write({required String uid, required TodayCard card});

  Future<void> clear({required String uid});
}

class SharedPreferencesTodayCardCache implements TodayCardCache {
  SharedPreferencesTodayCardCache({SharedPreferencesUtil? preferences})
      : _preferences = preferences ?? SharedPreferencesUtil();

  static const String _cacheSchemaVersion = 'today-card-cache-v1';
  final SharedPreferencesUtil _preferences;

  String _key(String uid) => 'ellaTodayCardCache:$uid';

  @override
  Future<TodayCard?> read({required String uid}) async {
    if (uid.isEmpty) return null;
    final encoded = _preferences.getString(_key(uid));
    if (encoded.isEmpty) return null;
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is! Map ||
          decoded['cache_schema'] != _cacheSchemaVersion ||
          decoded['contract_version'] != todayCardContractVersion ||
          decoded['uid'] != uid) {
        return null;
      }
      return TodayCard.fromCacheJson(decoded['card']);
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> write({required String uid, required TodayCard card}) async {
    if (uid.isEmpty || !card.isValid) return;
    await _preferences.saveString(
      _key(uid),
      jsonEncode({
        'cache_schema': _cacheSchemaVersion,
        'contract_version': todayCardContractVersion,
        'uid': uid,
        'card': card.toCacheJson(),
      }),
    );
  }

  @override
  Future<void> clear({required String uid}) async {
    if (uid.isEmpty) return;
    await _preferences.remove(_key(uid));
  }
}
