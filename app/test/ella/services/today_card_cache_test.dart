import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_repository.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('cache round trip retains the exact truthful card fields', () async {
    final cache = SharedPreferencesTodayCardCache();
    final card = _card();

    await cache.write(uid: 'uid-a', card: card);
    final restored = await cache.read(uid: 'uid-a');

    expect(restored?.id, card.id);
    expect(restored?.version, card.version);
    expect(restored?.eyebrow, card.eyebrow);
    expect(restored?.sourceDate, card.sourceDate);
    expect(restored?.sourceRefs.single.versionId, 'v4');
  });

  test('cache is account-scoped and ignores another uid', () async {
    final cache = SharedPreferencesTodayCardCache();
    await cache.write(uid: 'uid-a', card: _card());

    expect(await cache.read(uid: 'uid-b'), isNull);
  });

  test('cache rejects a stale contract version', () async {
    SharedPreferences.setMockInitialValues({
      'ellaTodayCardCache:uid-a': jsonEncode({
        'cache_schema': 'today-card-cache-v1',
        'contract_version': 'ella.today_card.v0',
        'uid': 'uid-a',
        'card': _card().toCacheJson(),
      }),
    });
    await SharedPreferencesUtil.init();

    expect(await SharedPreferencesTodayCardCache().read(uid: 'uid-a'), isNull);
  });
}

TodayCard _card() => TodayCard(
      id: 'memory-card',
      version: 3,
      kind: TodayCardKind.memory,
      eyebrow: 'A MEMORY FROM JUNE 12',
      headline: 'The roses along Elm Street',
      body: 'You enjoyed the long walk home with Rose.',
      spokenText: 'You enjoyed the long walk home with Rose.',
      sourceDate: '2026-06-12',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1', versionId: 'v4')],
    );
