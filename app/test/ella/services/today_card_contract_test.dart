import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';

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
    final response = HttpTodayCardRepository.parseEnvelope({
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
    }, headerEtag: 'today-2');

    expect(response.isValid, isTrue);
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

    await controller.updateAuthority(uid: 'account-a', isProvisioningReady: false);
    expect(repository.fetches, 0);

    await controller.updateAuthority(uid: 'account-a', isProvisioningReady: true);
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

    final oldLoad = controller.updateAuthority(uid: 'account-a', isProvisioningReady: true);
    await Future<void>.delayed(Duration.zero);
    await controller.updateAuthority(uid: 'account-b', isProvisioningReady: false);
    delayed.complete(
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card));
    await oldLoad;

    expect(controller.state.card, isNull);
    expect(cache.cards['account-b'], isNull);
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

class _MemoryTodayCardCache implements TodayCardCache {
  final Map<String, TodayCard> cards = {};

  @override
  Future<void> clear({required String uid}) async {
    cards.remove(uid);
  }

  @override
  Future<TodayCard?> read({required String uid}) async => cards[uid];

  @override
  Future<void> write({required String uid, required TodayCard card}) async {
    cards[uid] = card;
  }
}
