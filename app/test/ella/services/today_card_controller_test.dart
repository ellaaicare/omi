import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';

void main() {
  group('TodayCardController', () {
    test('waits for authenticated provisioning before the first load', () async {
      final repository = _QueueRepository([_ready(_recap())]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: false);
      expect(repository.requestedUids, isEmpty);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      expect(repository.requestedUids, ['uid-a']);
      expect(controller.state.status, TodayCardStatus.ready);
    });

    test('retries when the app resumes after provisioning', () async {
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'temporarily_unavailable',
        ),
        _ready(_recap(version: 2)),
      ]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      expect(controller.state.status, TodayCardStatus.degraded);

      await controller.onResumed();
      expect(repository.requestedUids, ['uid-a', 'uid-a']);
      expect(controller.state.card?.version, 2);
    });

    test('shows a last-valid card when refresh is degraded', () async {
      final cache = _MemoryCache()..cards['uid-a'] = _memory();
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'provider_unavailable',
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.card?.kind, TodayCardKind.memory);
      expect(controller.state.isCached, isTrue);
      expect(controller.state.errorCode, 'provider_unavailable');
    });

    test('keeps degraded state distinct when no cache exists', () async {
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'provider_unavailable',
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.card, isNull);
      expect(controller.state.isCached, isFalse);
    });

    test('new user is authoritative and clears an obsolete cache', () async {
      final cache = _MemoryCache()..cards['uid-a'] = _recap();
      final repository = _QueueRepository([
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.newUser, card: _welcome()),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.newUser);
      expect(controller.state.card?.kind, TodayCardKind.welcome);
      expect(cache.cards['uid-a'], isNull);
    });

    test('stale contract never replaces a current cached card', () async {
      final cached = _interest();
      final cache = _MemoryCache()..cards['uid-a'] = cached;
      final repository = _QueueRepository([
        TodayCardResponse(
          contractVersion: 'ella.today_card.v0',
          status: TodayCardStatus.ready,
          card: _recap(version: 99),
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.errorCode, 'stale_contract');
      expect(controller.state.card, same(cached));
      expect(cache.cards['uid-a'], same(cached));
    });

    test('late response from another account cannot cross the account boundary', () async {
      final first = Completer<TodayCardResponse>();
      final repository = _DeferredRepository(first.future, _ready(_memory(id: 'uid-b-card')));
      final cache = _MemoryCache();
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      final firstLoad = controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      await Future<void>.delayed(Duration.zero);
      await controller.updateAuthority(uid: 'uid-b', isProvisioningReady: true);
      first.complete(_ready(_recap(id: 'uid-a-card')));
      await firstLoad;

      expect(controller.state.card?.id, 'uid-b-card');
      expect(cache.cards['uid-a'], isNull);
      expect(cache.cards['uid-b']?.id, 'uid-b-card');
    });

    test('losing provisioning authority cancels an in-flight load and clears its card', () async {
      final pending = Completer<TodayCardResponse>();
      final repository = _DeferredRepository(pending.future, _ready(_memory()));
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      final load = controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      await Future<void>.delayed(Duration.zero);
      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: false);
      pending.complete(_ready(_recap()));
      await load;

      expect(controller.state.status, TodayCardStatus.preparing);
      expect(controller.state.card, isNull);
    });

    test('recap, memory, and interest preserve server-authored source labels', () {
      expect(_recap().eyebrow, 'A NOTE FROM YESTERDAY');
      expect(_memory().eyebrow, 'A MEMORY FROM JUNE 12');
      expect(_interest().eyebrow, 'SOMETHING YOU ENJOY');
    });
  });
}

TodayCardResponse _ready(TodayCard card) =>
    TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card);

TodayCard _recap({String id = 'recap-card', int version = 1}) => TodayCard(
      id: id,
      version: version,
      kind: TodayCardKind.recap,
      eyebrow: 'A NOTE FROM YESTERDAY',
      headline: 'A good conversation with Rose',
      body: 'You and Rose talked about the garden.',
      spokenText: 'You and Rose talked about the garden.',
      sourceDate: '2026-07-30',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'conversation_summary', id: 'conversation-1', versionId: 'v2')],
    );

TodayCard _memory({String id = 'memory-card'}) => TodayCard(
      id: id,
      version: 3,
      kind: TodayCardKind.memory,
      eyebrow: 'A MEMORY FROM JUNE 12',
      headline: 'The roses along Elm Street',
      body: 'You enjoyed the long walk home with Rose.',
      sourceDate: '2026-06-12',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1', versionId: 'v4')],
    );

TodayCard _interest() => TodayCard(
      id: 'interest-card',
      version: 2,
      kind: TodayCardKind.interest,
      eyebrow: 'SOMETHING YOU ENJOY',
      headline: 'Your garden',
      body: 'The roses are one of your favorite parts of summer.',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'confirmed_interest', id: 'interest-1')],
    );

TodayCard _welcome() => TodayCard(
      id: 'welcome-card',
      version: 1,
      kind: TodayCardKind.welcome,
      eyebrow: 'FOR YOU TODAY',
      headline: 'What matters to you?',
      body: 'Tell Ella about a person, place, or interest you would like to talk about.',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
    );

class _QueueRepository implements TodayCardRepository {
  _QueueRepository(this.responses);

  final List<TodayCardResponse> responses;
  final List<String> requestedUids = [];

  @override
  Future<TodayCardResponse> fetch({required String uid}) async {
    requestedUids.add(uid);
    return responses.removeAt(0);
  }
}

class _DeferredRepository implements TodayCardRepository {
  _DeferredRepository(this.first, this.second);

  final Future<TodayCardResponse> first;
  final TodayCardResponse second;
  int calls = 0;

  @override
  Future<TodayCardResponse> fetch({required String uid}) {
    calls++;
    return calls == 1 ? first : Future.value(second);
  }
}

class _MemoryCache implements TodayCardCache {
  final Map<String, TodayCard> cards = {};

  @override
  Future<void> clear({required String uid}) async => cards.remove(uid);

  @override
  Future<TodayCard?> read({required String uid}) async => cards[uid];

  @override
  Future<void> write({required String uid, required TodayCard card}) async => cards[uid] = card;
}
