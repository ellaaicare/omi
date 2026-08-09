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
    return parseEnvelope(
      decoded,
      headerEtag: response.headers['etag'] ?? '',
      headerCacheControl: response.headers['cache-control'] ?? '',
    );
  }

  @visibleForTesting
  static TodayCardResponse parseEnvelope(
    Object? decoded, {
    String headerEtag = '',
    String headerCacheControl = '',
  }) {
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
      cacheMaxAge: _cacheMaxAge(headerCacheControl),
    );
  }

  static Duration _cacheMaxAge(String value) {
    final match = RegExp(r'(?:^|,)\s*max-age\s*=\s*(\d+)\s*(?:,|$)', caseSensitive: false).firstMatch(value);
    final seconds = int.tryParse(match?.group(1) ?? '') ?? 60;
    return Duration(seconds: seconds.clamp(0, 300));
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
  Future<TodayCard?> read({required String uid, required String authorityKey});

  Future<bool> write({
    required String uid,
    required String authorityKey,
    required TodayCard card,
    required Duration maxAge,
    required bool Function() isCurrent,
  });

  Future<void> clear({required String uid, String authorityKey = ''});
}

class SharedPreferencesTodayCardCache implements TodayCardCache {
  SharedPreferencesTodayCardCache({
    SharedPreferencesUtil? preferences,
    DateTime Function()? now,
    Future<void> Function()? beforeCommit,
  })  : _preferences = preferences ?? SharedPreferencesUtil(),
        _now = now ?? DateTime.now,
        _beforeCommit = beforeCommit;

  static const String _cacheSchemaVersion = 'today-card-cache-v2';
  static int _writeSequence = 0;
  final SharedPreferencesUtil _preferences;
  final DateTime Function() _now;
  final Future<void> Function()? _beforeCommit;

  String _key(String uid) => 'ellaTodayCardCache:$uid';

  @override
  Future<TodayCard?> read({required String uid, required String authorityKey}) async {
    if (uid.isEmpty || authorityKey.isEmpty) return null;
    final encoded = _preferences.getString(_key(uid));
    if (encoded.isEmpty) return null;
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is! Map ||
          decoded['cache_schema'] != _cacheSchemaVersion ||
          decoded['contract_version'] != todayCardContractVersion ||
          decoded['uid'] != uid) {
        await clear(uid: uid);
        return null;
      }
      // A stale reader must never erase a newer authority's same-UID cache.
      if (decoded['authority_key'] != authorityKey) return null;
      final expiresAt = DateTime.tryParse(decoded['expires_at']?.toString() ?? '');
      if (expiresAt == null || !_now().toUtc().isBefore(expiresAt.toUtc())) {
        await clear(uid: uid, authorityKey: authorityKey);
        return null;
      }
      return TodayCard.fromCacheJson(decoded['card']);
    } catch (_) {
      await clear(uid: uid);
      return null;
    }
  }

  @override
  Future<bool> write({
    required String uid,
    required String authorityKey,
    required TodayCard card,
    required Duration maxAge,
    required bool Function() isCurrent,
  }) async {
    if (uid.isEmpty || authorityKey.isEmpty || !card.isValid || !isCurrent()) return false;
    final boundedAge = Duration(seconds: maxAge.inSeconds.clamp(0, 300));
    if (boundedAge == Duration.zero) {
      await clear(uid: uid, authorityKey: authorityKey);
      return isCurrent();
    }
    final writeToken = '${_now().microsecondsSinceEpoch}:${_writeSequence++}';
    await _beforeCommit?.call();
    await _preferences.saveString(
      _key(uid),
      jsonEncode({
        'cache_schema': _cacheSchemaVersion,
        'contract_version': todayCardContractVersion,
        'uid': uid,
        'authority_key': authorityKey,
        'write_token': writeToken,
        'expires_at': _now().toUtc().add(boundedAge).toIso8601String(),
        'card': card.toCacheJson(),
      }),
    );
    if (isCurrent()) return true;
    await _clearMatchingWrite(uid: uid, authorityKey: authorityKey, writeToken: writeToken);
    return false;
  }

  @override
  Future<void> clear({required String uid, String authorityKey = ''}) async {
    if (uid.isEmpty) return;
    if (authorityKey.isNotEmpty) {
      final encoded = _preferences.getString(_key(uid));
      try {
        final decoded = jsonDecode(encoded);
        if (decoded is Map && decoded['authority_key'] != authorityKey) return;
      } catch (_) {}
    }
    await _preferences.remove(_key(uid));
  }

  Future<void> _clearMatchingWrite({
    required String uid,
    required String authorityKey,
    required String writeToken,
  }) async {
    final encoded = _preferences.getString(_key(uid));
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is Map && decoded['authority_key'] == authorityKey && decoded['write_token'] == writeToken) {
        await _preferences.remove(_key(uid));
      }
    } catch (_) {}
  }
}
