import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

void main() {
  const source = TodayCardSourceRef(kind: 'hermes_memory', id: 'memory-1', versionId: 'memory-v1');
  final card = TodayCard(
    id: 'today-1',
    version: 2,
    kind: TodayCardKind.memory,
    eyebrow: 'A THOUGHT FROM ELLA',
    headline: 'Something worth remembering',
    body: 'A verified thought from the private Hermes memory ledger.',
    generatedAt: DateTime.utc(2032, 5, 6, 8),
    sourceRefs: const [source],
  );

  test('typed envelope accepts only the exact contract, matching ETag, and evidence-backed card', () {
    final envelope = {
      'contract_version': todayCardContractVersion,
      'state': 'ready',
      'etag': 'today-2',
      'server_time': '2032-05-06T08:01:00Z',
      'card': {
        'card_id': 'today-1',
        'version': 2,
        'kind': 'memory',
        'eyebrow': 'A THOUGHT FROM ELLA',
        'headline': 'Something worth remembering',
        'body': 'A verified thought from the private Hermes memory ledger.',
        'generated_at': '2032-05-06T08:00:00Z',
        'source_refs': [
          {'source_type': 'hermes_memory', 'source_id': 'memory-1', 'source_version_id': 'memory-v1'},
        ],
      },
    };
    final response = HttpTodayCardRepository.parseEnvelope(
      envelope,
      headerEtag: 'today-2',
      headerCacheControl: 'private, max-age=60, must-revalidate',
    );

    expect(response.isValid, isTrue);
    expect(response.cacheMaxAge, const Duration(seconds: 60));
    expect(
      HttpTodayCardRepository.parseEnvelope(
        envelope,
        headerEtag: 'today-2',
        headerCacheControl: 'max-age=60',
      ).cacheMaxAge,
      Duration.zero,
    );
    expect(
      HttpTodayCardRepository.parseEnvelope(envelope, headerEtag: 'today-2').cacheMaxAge,
      Duration.zero,
    );
    expect(
      HttpTodayCardRepository.parseEnvelope(
        envelope,
        headerEtag: 'today-2',
        headerCacheControl: 'private, max-age=invalid, must-revalidate',
      ).cacheMaxAge,
      Duration.zero,
    );
    expect(
      HttpTodayCardRepository.parseEnvelope(
        envelope,
        headerEtag: 'today-2',
        headerCacheControl: 'private, no-cache, max-age=60, must-revalidate',
      ).cacheMaxAge,
      Duration.zero,
    );
    expect(response.card?.sourceRefs.single.kind, 'hermes_memory');
    expect(
      HttpTodayCardRepository.parseEnvelope({
        'contract_version': todayCardContractVersion,
        'state': 'ready',
        'etag': 'today-2',
        'server_time': '2032-05-06T08:01:00Z',
        'card': {
          'card_id': 'today-1',
          'version': 2,
          'kind': 'memory',
          'eyebrow': 'A THOUGHT FROM ELLA',
          'headline': 'Something worth remembering',
          'body': 'Unsupported content without a source.',
          'generated_at': '2032-05-06T08:00:00Z',
          'source_refs': const [],
        },
      }, headerEtag: 'today-2')
          .isValid,
      isFalse,
    );
  });

  test('controller does not fetch until provisioning is ready and caches only a valid typed card', () async {
    final repository = _QueueTodayCardRepository([
      TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card),
    ]);
    final cache = _MemoryTodayCardCache();
    final controller = TodayCardController(repository: repository, cache: cache);
    addTearDown(controller.dispose);

    await controller.updateAuthority(uid: 'account-a', authorityKey: 'authority-a', isProvisioningReady: false);
    expect(repository.fetches, 0);

    await controller.updateAuthority(uid: 'account-a', authorityKey: 'authority-a', isProvisioningReady: true);
    expect(repository.fetches, 1);
    expect(controller.state.status, TodayCardStatus.ready);
    expect(controller.state.card?.id, 'today-1');
    expect(cache.cards['account-a']?.id, 'today-1');
  });

  test('an older account response cannot populate the new account state', () async {
    final delayed = Completer<TodayCardResponse>();
    final repository = _DeferredTodayCardRepository(delayed.future);
    final cache = _MemoryTodayCardCache();
    final controller = TodayCardController(repository: repository, cache: cache);
    addTearDown(controller.dispose);

    final oldLoad = controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );
    await Future<void>.delayed(Duration.zero);
    await controller.updateAuthority(uid: 'account-b', authorityKey: '', isProvisioningReady: false);
    delayed.complete(
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card));
    await oldLoad;

    expect(controller.state.card, isNull);
    expect(cache.cards['account-b'], isNull);
  });

  test('authority loss during a delayed cache commit cannot resurrect the old account card', () async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final commitStarted = Completer<void>();
    final releaseCommit = Completer<void>();
    final cache = SharedPreferencesTodayCardCache(
      beforeCommit: (_) async {
        if (!commitStarted.isCompleted) commitStarted.complete();
        await releaseCommit.future;
      },
    );
    final controller = TodayCardController(
      repository: _QueueTodayCardRepository([
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card),
      ]),
      cache: cache,
    );
    addTearDown(controller.dispose);

    final oldLoad = controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );
    await commitStarted.future;
    await controller.updateAuthority(uid: 'account-b', authorityKey: '', isProvisioningReady: false);
    releaseCommit.complete();
    await oldLoad;

    final preferences = SharedPreferencesUtil();
    expect(preferences.getString('ellaTodayCardCache:account-a'), isEmpty);
    expect(preferences.getString('ellaTodayCardCache:account-b'), isEmpty);
    expect(controller.state.card, isNull);
  });

  test('a late obsolete same-UID write cannot erase the replacement authority cache', () async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final oldCommitStarted = Completer<void>();
    final releaseOldCommit = Completer<void>();
    final replacementCard = TodayCard(
      id: 'today-2',
      version: 3,
      kind: card.kind,
      eyebrow: card.eyebrow,
      headline: 'The replacement authority memo',
      body: card.body,
      generatedAt: card.generatedAt,
      sourceRefs: card.sourceRefs,
    );
    final cache = SharedPreferencesTodayCardCache(
      beforeCommit: (authorityKey) async {
        if (authorityKey != 'authority-a') return;
        oldCommitStarted.complete();
        await releaseOldCommit.future;
      },
    );
    final controller = TodayCardController(
      repository: _QueueTodayCardRepository([
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card),
        TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.ready,
          card: replacementCard,
        ),
      ]),
      cache: cache,
    );
    addTearDown(controller.dispose);

    final oldLoad = controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );
    await oldCommitStarted.future;
    await controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-b',
      isProvisioningReady: true,
    );
    expect((await cache.read(uid: 'account-a', authorityKey: 'authority-b'))?.card.id, 'today-2');

    releaseOldCommit.complete();
    await oldLoad;

    expect((await cache.read(uid: 'account-a', authorityKey: 'authority-b'))?.card.id, 'today-2');
    expect(SharedPreferencesUtil().getString('ellaTodayCardCache:account-a'), isNotEmpty);
  });

  test('cache is authority-bound, expires at the server max-age, and removes stale entries', () async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    var now = DateTime.utc(2032, 5, 6, 8);
    final cache = SharedPreferencesTodayCardCache(now: () => now);
    var current = true;

    expect(
      await cache.write(
        uid: 'account-a',
        authorityKey: 'authority-a',
        card: card,
        maxAge: const Duration(seconds: 60),
        isCurrent: () => current,
      ),
      isTrue,
    );
    expect((await cache.read(uid: 'account-a', authorityKey: 'authority-a'))?.card, isNotNull);
    expect(await cache.read(uid: 'account-a', authorityKey: 'authority-b'), isNull);

    expect(
      await cache.write(
        uid: 'account-a',
        authorityKey: 'authority-b',
        card: card,
        maxAge: const Duration(seconds: 60),
        isCurrent: () => current,
      ),
      isTrue,
    );
    expect(await cache.read(uid: 'account-a', authorityKey: 'authority-a'), isNull);
    expect((await cache.read(uid: 'account-a', authorityKey: 'authority-b'))?.card, isNotNull);
    now = now.add(const Duration(seconds: 61));
    expect(await cache.read(uid: 'account-a', authorityKey: 'authority-b'), isNull);
    expect(SharedPreferencesUtil().getString('ellaTodayCardCache:account-a'), isEmpty);
    current = false;
  });

  test('a typed source tombstone clears the cached Daily Memo immediately', () async {
    final cache = _MemoryTodayCardCache()..cards['account-a'] = card;
    final controller = TodayCardController(
      repository: _QueueTodayCardRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'today_card_source_stale',
        ),
      ]),
      cache: cache,
    );
    addTearDown(controller.dispose);

    await controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );

    expect(controller.state.card, isNull);
    expect(cache.cards['account-a'], isNull);
  });

  test('a ready response without the private revalidation policy stays hidden', () async {
    final cache = _MemoryTodayCardCache();
    final controller = TodayCardController(
      repository: _QueueTodayCardRepository([
        TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.ready,
          card: card,
          cacheMaxAge: Duration.zero,
        ),
      ]),
      cache: cache,
    );
    addTearDown(controller.dispose);

    await controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );

    expect(controller.state.status, TodayCardStatus.degraded);
    expect(controller.state.errorCode, 'invalid_today_card_cache_policy');
    expect(controller.state.card, isNull);
    expect(cache.cards['account-a'], isNull);
  });

  test('foreground expiry hides the memo before authoritative revalidation completes', () async {
    final tombstone = Completer<TodayCardResponse>();
    final repository = _FutureQueueTodayCardRepository([
      Future.value(
        TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.ready,
          card: card,
          cacheMaxAge: const Duration(seconds: 60),
        ),
      ),
      tombstone.future,
    ]);
    final cache = _MemoryTodayCardCache();
    final scheduler = _ManualExpiryScheduler();
    final controller = TodayCardController(
      repository: repository,
      cache: cache,
      expiryScheduler: scheduler.schedule,
    );
    addTearDown(controller.dispose);

    await controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );
    expect(controller.state.card?.id, 'today-1');
    expect(scheduler.latestDuration, const Duration(seconds: 60));

    scheduler.fireLatest();
    expect(controller.state.card, isNull);
    expect(controller.state.isLoading, isTrue);
    await Future<void>.delayed(Duration.zero);
    tombstone.complete(
      const TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.degraded,
        errorCode: 'today_card_source_retracted',
      ),
    );
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);

    expect(controller.state.card, isNull);
    expect(controller.state.status, TodayCardStatus.degraded);
    expect(cache.cards['account-a'], isNull);
  });

  test('disposing the controller cancels foreground revalidation', () async {
    final repository = _QueueTodayCardRepository([
      TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card),
    ]);
    final scheduler = _ManualExpiryScheduler();
    final controller = TodayCardController(
      repository: repository,
      cache: _MemoryTodayCardCache(),
      expiryScheduler: scheduler.schedule,
    );

    await controller.updateAuthority(
      uid: 'account-a',
      authorityKey: 'authority-a',
      isProvisioningReady: true,
    );
    controller.dispose();
    scheduler.fireLatest();

    expect(scheduler.latestIsActive, isFalse);
    expect(repository.fetches, 1);
  });

  test('daily-card stale scope refresh stays daily-card and never loads a conversation', () async {
    final refreshed = await EllaVoiceChatPage.refreshSessionScope(
      const V2VSessionScope.dailyCard(cardId: 'today-1', expectedVersion: 1),
      uid: 'account-a',
      todayCardRepository: _QueueTodayCardRepository([
        TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.ready,
          card: TodayCard(
            id: card.id,
            version: 3,
            kind: card.kind,
            eyebrow: card.eyebrow,
            headline: card.headline,
            body: card.body,
            generatedAt: card.generatedAt,
            sourceRefs: card.sourceRefs,
          ),
        ),
      ]),
      memoryLoader: (_) => fail('daily-card refresh must not call the conversation endpoint'),
    );

    expect(refreshed?.kind, V2VSessionScopeKind.dailyCard);
    expect(refreshed?.cardId, 'today-1');
    expect(refreshed?.expectedVersion, 3);
    expect(refreshed?.conversationId, isEmpty);
  });

  test('Daily Memo read-aloud stops when exact account authority drifts mid-utterance', () async {
    final authority = _MutableAuthority('account-a');
    var stops = 0;
    final spoken = await TodayPage.readAloudWithAuthority(
      uid: 'account-a',
      text: 'A bounded daily thought.',
      authorityProvider: (_) => authority,
      speaker: (_, exactAuthority) async {
        expect(exactAuthority, same(authority));
        authority.current = false;
        throw ExactAccountAuthorityChangedException('test drift');
      },
      stop: () async => stops++,
    );

    expect(spoken, isFalse);
    expect(stops, 1);
  });
}

class _QueueTodayCardRepository implements TodayCardRepository {
  _QueueTodayCardRepository(this.responses);

  final List<TodayCardResponse> responses;
  int fetches = 0;

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => responses[fetches++];
}

class _DeferredTodayCardRepository implements TodayCardRepository {
  _DeferredTodayCardRepository(this.response);

  final Future<TodayCardResponse> response;

  @override
  Future<TodayCardResponse> fetch({required String uid}) => response;
}

class _FutureQueueTodayCardRepository implements TodayCardRepository {
  _FutureQueueTodayCardRepository(this.responses);

  final List<Future<TodayCardResponse>> responses;
  int fetches = 0;

  @override
  Future<TodayCardResponse> fetch({required String uid}) => responses[fetches++];
}

class _MemoryTodayCardCache implements TodayCardCache {
  final Map<String, TodayCard> cards = {};

  @override
  Future<void> clear({required String uid, String authorityKey = ''}) async {
    cards.remove(uid);
  }

  @override
  Future<TodayCardCacheEntry?> read({required String uid, required String authorityKey}) async {
    final card = cards[uid];
    return card == null ? null : TodayCardCacheEntry(card: card, freshnessRemaining: const Duration(seconds: 60));
  }

  @override
  Future<bool> write({
    required String uid,
    required String authorityKey,
    required TodayCard card,
    required Duration maxAge,
    required bool Function() isCurrent,
  }) async {
    if (!isCurrent()) return false;
    cards[uid] = card;
    return true;
  }
}

class _ManualExpiryScheduler {
  final List<_ManualTimer> timers = [];
  Duration? latestDuration;

  Timer schedule(Duration duration, void Function() callback) {
    latestDuration = duration;
    final timer = _ManualTimer(callback);
    timers.add(timer);
    return timer;
  }

  bool get latestIsActive => timers.last.isActive;

  void fireLatest() => timers.last.fire();
}

class _ManualTimer implements Timer {
  _ManualTimer(this._callback);

  final void Function() _callback;
  bool _isActive = true;
  int _tick = 0;

  @override
  bool get isActive => _isActive;

  @override
  int get tick => _tick;

  @override
  void cancel() => _isActive = false;

  void fire() {
    if (!_isActive) return;
    _isActive = false;
    _tick++;
    _callback();
  }
}

class _MutableAuthority implements ExactAccountAuthorityVerifier {
  _MutableAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isExactCurrent() => current;
}
