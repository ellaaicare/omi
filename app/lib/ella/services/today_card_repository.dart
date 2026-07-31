import 'dart:convert';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/today_card.dart';

abstract interface class TodayCardRepository {
  Future<TodayCardResponse> fetch({required String uid});
}

/// Safe placeholder until the backend lane publishes the exact v1 wire schema.
///
/// Keeping this typed avoids retaining the legacy DailySummary list-first path
/// or guessing fields that would become an accidental competing contract.
class PendingTodayCardRepository implements TodayCardRepository {
  const PendingTodayCardRepository();

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => const TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.degraded,
        errorCode: 'today_card_contract_pending',
      );
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
