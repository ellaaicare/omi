import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _Authority implements AccountCommitAuthority {
  const _Authority(this.uid, this.current);
  @override
  final String uid;
  final bool Function() current;
  @override
  bool isCurrent() => current();
  @override
  bool isExactCurrent() => current();
}

String key(int i) => i.toRadixString(16).padLeft(64, '0');
ServerMessage row(int i, {String? id, int day = 12, bool local = false, bool overheard = false}) => ServerMessage(
      id ?? 'row-$i',
      DateTime.utc(2026, 10, day),
      'Synthetic row $i',
      MessageSender.ai,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
      canonicalEventKey: local ? null : key(i),
      isCanonicalOverheardTranscript: overheard,
    );
EllaServiceResult<EllaChatHistoryPage> page(List<ServerMessage> rows, {String? cursor}) => EllaServiceResult.success(
      EllaChatHistoryPage(messages: rows, hasMore: cursor != null, isKeyset: true, nextCursor: cursor),
    );
MessageProvider provider(EllaChatKeysetFetcher fetch) => MessageProvider(
      historyPaginationEnabled: true,
      activeAuthority: () {
        final uid = SharedPreferencesUtil().uid;
        return _Authority(uid, () => SharedPreferencesUtil().uid == uid);
      },
      ellaChatKeysetFetcher: fetch,
    );
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  PlatformManager.initializeForTesting();
  Env.init(_TestEnv());
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'owner-a'});
    await SharedPreferencesUtil.init();
  });
  Map<String, Object?> earlier(MessageProvider p) =>
      p.historyAdmissionDiagnostic['earlier_page'] as Map<String, Object?>;
  EllaServiceResult<EllaChatHistoryPage> witnessed(List<ServerMessage> rows, {String? cursor}) =>
      EllaServiceResult.success(
        EllaChatHistoryPage(
          messages: rows,
          hasMore: cursor != null,
          isKeyset: true,
          paginationVersion: 1,
          rawRowCount: rows.length,
          nextCursor: cursor,
        ),
      );

  for (final empty in [false, true]) {
    test('actual earlier adoption metadata distinguishes empty terminal=$empty', () async {
      var calls = 0;
      final p = provider(
        ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => ++calls == 1
            ? witnessed([row(2)], cursor: 'PRIVATE_CURSOR_SENTINEL')
            : witnessed(empty ? [] : [row(1), row(0, overheard: true)]),
      );
      addTearDown(p.dispose);
      await p.refreshMessages();
      expect(earlier(p)['disposition'], 'not_observed_current_account');
      expect(await p.loadEarlierMessages(), isTrue);
      final d = earlier(p);
      expect(d['disposition'], 'adopted');
      expect(d['adopted'], true);
      expect(d['response_valid'], true);
      expect(d['version'], 1);
      expect(d['raw_row_count'], empty ? 0 : 2);
      expect(d['parsed_row_count'], empty ? 0 : 2);
      expect(d['display_row_count'], empty ? 0 : 1);
      expect(d['added_timeline_row_count'], empty ? 0 : 2);
      expect(d['has_more'], false);
      expect(d['cursor_present'], false);
      expect(p.historyAdmissionDiagnostic['has_more'], true, reason: 'first-page snapshot remains separate');
      expect(p.historyAdmissionDiagnostic['current_timeline_row_count'], empty ? 1 : 3);
      expect(p.historyAdmissionDiagnostic.toString(), isNot(contains('PRIVATE_CURSOR_SENTINEL')));
      expect(p.historyAdmissionDiagnostic.toString(), isNot(contains('Synthetic row')));
      expect(p.historyAdmissionDiagnostic.toString(), isNot(contains('owner-a')));
      expect(() => d['adopted'] = false, throwsUnsupportedError);
    });
  }
  test('rejected earlier continuation is not terminal adoption and clamps status', () async {
    var calls = 0;
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => ++calls == 1
          ? witnessed([row(2)], cursor: 'PRIVATE_CURSOR_SENTINEL')
          : const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse, statusCode: 409)),
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), isFalse);
    expect(earlier(p)['disposition'], 'rejected_response');
    expect(earlier(p)['response_valid'], false);
    expect(earlier(p)['adopted'], false);
    expect(earlier(p)['has_more'], null);
    expect(earlier(p)['status'], 409);
    expect(p.messages.length, 1);
  });
  test('pending earlier response after latest refresh cannot overwrite current metadata', () async {
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var calls = 0;
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      calls++;
      if (calls == 1) return witnessed([row(2)], cursor: 'PRIVATE_CURSOR_SENTINEL');
      if (calls == 2) return pending.future;
      return witnessed([row(5)]);
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    final earlierRequest = p.loadEarlierMessages();
    expect(earlier(p)['disposition'], 'fetch_started');
    await p.refreshMessages();
    final before = p.historyAdmissionDiagnostic;
    pending.complete(witnessed([row(1)]));
    expect(await earlierRequest, isFalse);
    expect(p.historyAdmissionDiagnostic, before);
    expect(earlier(p)['adopted'], null);
    expect(earlier(p)['disposition'], 'not_observed_current_account');
  });
  test('account drift and reset retire earlier evidence without contents', () async {
    var calls = 0;
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          ++calls == 1 ? witnessed([row(2)], cursor: 'PRIVATE_CURSOR_SENTINEL') : witnessed([row(1)]),
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    await p.loadEarlierMessages();
    expect(earlier(p)['adopted'], true);
    SharedPreferencesUtil().uid = 'owner-b';
    expect(earlier(p)['adopted'], null);
    expect(earlier(p)['parsed_row_count'], null);
    p.reset();
    expect(earlier(p)['disposition'], 'not_observed_current_account');
  });
  test('closed first page diagnostic separates validated metadata and adoption', () async {
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) => pending.future,
    );
    addTearDown(p.dispose);
    final refresh = p.refreshMessages();
    expect(p.historyAdmissionDiagnostic['disposition'], 'fetching');
    pending.complete(
      EllaServiceResult.success(
        EllaChatHistoryPage(
          messages: [row(1), row(2, overheard: true)],
          hasMore: true,
          isKeyset: true,
          paginationVersion: 1,
          rawRowCount: 3,
          nextCursor: 'PRIVATE_CURSOR_SENTINEL',
        ),
      ),
    );
    await refresh;
    final d = p.historyAdmissionDiagnostic;
    expect(d['version'], 1);
    expect(d['raw_row_count'], 3);
    expect(d['parsed_row_count'], 2);
    expect(d['timeline_row_count'], 2);
    expect(d['display_timeline_row_count'], 1);
    expect(d['display_row_count'], 1);
    expect(d['has_more'], true);
    expect(d['cursor_present'], true);
    expect(d['adopted'], true);
    expect(d['disposition'], 'adopted');
    expect(d['can_load_earlier'], true);
    expect(d.toString(), isNot(contains('PRIVATE_CURSOR_SENTINEL')));
    expect(d.toString(), isNot(contains('owner-a')));
    expect(d.toString(), isNot(contains('Synthetic row')));
    expect(() => d['adopted'] = false, throwsUnsupportedError);
    SharedPreferencesUtil().uid = 'owner-b';
    expect(p.historyAdmissionDiagnostic['version'], null);
    expect(p.historyAdmissionDiagnostic['disposition'], 'not_observed_current_account');
    p.reset();
    expect(p.historyAdmissionDiagnostic['adopted'], null);
  });
  test('valid first page deferred during real held send is not adopted', () async {
    final stream = StreamController<ServerMessageChunk>();
    final entered = Completer<void>();
    final p = MessageProvider(
      historyPaginationEnabled: true,
      activeAuthority: () => _Authority('owner-a', () => true),
      aiConsentEnsurer: () async => true,
      ellaChatKeysetFetcher: ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          EllaServiceResult.success(
        EllaChatHistoryPage(
          messages: [row(1)],
          hasMore: false,
          isKeyset: true,
          paginationVersion: 1,
          rawRowCount: 1,
        ),
      ),
      ellaChatStreamSender: (text,
          {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) {
        entered.complete();
        return stream.stream;
      },
    );
    addTearDown(p.dispose);
    final send = p.sendMessageStreamToServer('PRIVATE_DRAFT_SENTINEL');
    await entered.future;
    await p.refreshMessages();
    expect(p.historyAdmissionDiagnostic['response_valid'], true);
    expect(p.historyAdmissionDiagnostic['adopted'], false);
    expect(p.historyAdmissionDiagnostic['disposition'], 'deferred_send');
    expect(p.historyAdmissionDiagnostic.toString(), isNot(contains('PRIVATE_DRAFT_SENTINEL')));
    await stream.close();
    await send;
  });
  test('stale response cannot overwrite newer adopted metadata', () async {
    final old = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var calls = 0;
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => ++calls == 1
          ? await old.future
          : EllaServiceResult.success(
              EllaChatHistoryPage(
                messages: [row(2)],
                hasMore: false,
                isKeyset: true,
                paginationVersion: 1,
                rawRowCount: 1,
              ),
            ),
    );
    addTearDown(p.dispose);
    final first = p.refreshMessages();
    await p.refreshMessages();
    final before = Map.of(p.historyAdmissionDiagnostic);
    old.complete(const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse)));
    await first;
    expect(p.historyAdmissionDiagnostic, before);
  });
  test('invalid response reports closed rejection without adoption', () async {
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse)),
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(p.historyAdmissionDiagnostic['adopted'], false);
    expect(p.historyAdmissionDiagnostic['response_valid'], false);
    expect(p.historyAdmissionDiagnostic['version'], null);
    expect(p.historyAdmissionDiagnostic['disposition'], 'rejected_response');
  });
  for (final voice in [false, true]) {
    for (final failed in [false, true]) {
      for (final pendingFirst in [false, true]) {
        for (final pageDuringSend in pendingFirst ? [false, true] : [false]) {
          test(
            'real send owns indices voice=$voice failure=$failed pendingOlder=$pendingFirst pageDuringSend=$pageDuringSend',
            () async {
              final stream = StreamController<ServerMessageChunk>();
              final entered = Completer<void>();
              final older = Completer<EllaServiceResult<EllaChatHistoryPage>>();
              final file = File('${Directory.systemTemp.path}/keyset-send-race.bin');
              addTearDown(() {
                if (file.existsSync()) file.deleteSync();
              });
              var olderCalls = 0;
              final p = MessageProvider(
                historyPaginationEnabled: true,
                activeAuthority: () => _Authority('owner-a', () => true),
                aiConsentEnsurer: () async => true,
                ellaChatKeysetFetcher: ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) {
                  if (cursor == null) return Future.value(page([row(2)], cursor: 'first.token'));
                  olderCalls++;
                  return older.future;
                },
                ellaChatStreamSender: (
                  text, {
                  required clientMessageId,
                  required clientSentAt,
                  expectedAuthenticatedUid,
                  exactAuthority,
                }) {
                  entered.complete();
                  return stream.stream;
                },
                voiceTempFileSaver: (bytes, startTime, frameSize) async => file..writeAsBytesSync([0, 0]),
                voiceChatStreamSender: (files, {expectedAuthenticatedUid, exactAuthority}) {
                  entered.complete();
                  return stream.stream;
                },
              );
              addTearDown(p.dispose);
              await p.refreshMessages();
              Future<bool>? loading;
              if (pendingFirst) loading = p.loadEarlierMessages();
              final send = voice
                  ? p.sendVoiceMessageStreamToServer([
                      [0, 0],
                    ])
                  : p.sendMessageStreamToServer('Synthetic held user turn');
              await entered.future.timeout(const Duration(seconds: 3));
              final outgoing = voice ? null : p.messages.firstWhere((m) => m.text == 'Synthetic held user turn');
              final placeholder = p.messages.last;
              if (!pendingFirst) {
                expect(await p.loadEarlierMessages(), false);
                expect(olderCalls, 0);
              }
              if (pendingFirst && pageDuringSend) {
                older.complete(page([row(1, day: 11)]));
                expect(await loading!, false);
                expect(p.canLoadEarlierHistory, false);
              }
              if (failed) {
                stream.add(ServerMessageChunk('terminal', '', MessageChunkType.error));
              } else {
                stream.add(
                  ServerMessageChunk(
                    'terminal',
                    '',
                    MessageChunkType.done,
                    message: ServerMessage(
                      'terminal',
                      DateTime.now(),
                      'Synthetic terminal answer',
                      MessageSender.ai,
                      MessageType.text,
                      null,
                      false,
                      [],
                      [],
                      [],
                    ),
                  ),
                );
              }
              await stream.close();
              await send;
              // Even after completion has cleared busy flags, the pending page is stale.
              if (pendingFirst && !pageDuringSend) {
                older.complete(page([row(1, day: 11)]));
                expect(await loading!, false);
              }
              expect(p.messages.where((m) => m.id == 'row-2').length, 1);
              expect(p.messages.any((m) => m.id == 'row-1'), false);
              if (outgoing != null) expect(p.messages, contains(outgoing));
              if (!failed) {
                expect(p.messages, isNot(contains(placeholder)));
                expect(p.messages.where((m) => m.text == 'Synthetic terminal answer').length, 1);
              }
              expect(p.showTypingIndicator, false);
              expect(p.sendingMessage, false);
              expect(p.isLoadingEarlierHistory, false);
              expect(p.canLoadEarlierHistory, true);
              expect(p.earlierHistoryFailure, isNull);
            },
          );
        }
      }
    }
  }
  test('explicit opt in, one bounded request, exact-key dedup not legacy ID', () async {
    final calls = <String?>[];
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      calls.add(cursor);
      expect(expectedAuthenticatedUid, 'owner-a');
      expect(exactAuthority.isExactCurrent(), true);
      return cursor == null
          ? page([row(3, id: 'same'), row(4)], cursor: 'first.token')
          : page([row(1, day: 11), row(2, id: 'same'), row(3, id: 'same')]);
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(p.canLoadEarlierHistory, true);
    expect(await p.loadEarlierMessages(), true);
    expect(p.messages.length, 4);
    expect(p.messages.where((m) => m.id == 'same').length, 2);
    expect(p.canLoadEarlierHistory, false);
    expect(await p.loadEarlierMessages(), false);
    expect(calls, [null, 'first.token']);
    expect(
      SharedPreferencesUtil().cachedMessages.map((m) => m.canonicalEventKey),
      p.messages.map((m) => m.canonicalEventKey),
    );
  });
  test('sixty simultaneous events survive keyset boundary and fresh-page restart', () async {
    final cursors = <String?>[];
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      cursors.add(cursor);
      return page(
        List.generate(cursor == null ? 50 : 10, (i) => row(cursor == null ? i + 10 : i, id: 'same')),
        cursor: cursor == null ? 'boundary.token' : null,
      );
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), true);
    expect(p.messages.map((m) => m.canonicalEventKey).toSet().length, 60);
    expect(p.messages.map((m) => m.canonicalEventKey), List.generate(60, key));
    await p.refreshMessages();
    expect(p.messages.length, 60, reason: 'latest-page refresh retains already loaded older keys');
    expect(cursors, [null, 'boundary.token', null]);
  });
  test('same UID authority retirement rejects held page even without newer result', () async {
    var current = true;
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final p = MessageProvider(
      historyPaginationEnabled: true,
      activeAuthority: () => _Authority('owner-a', () => current),
      ellaChatKeysetFetcher: ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          cursor == null ? page([row(2)], cursor: 'first.token') : await pending.future,
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    final load = p.loadEarlierMessages();
    current = false;
    pending.complete(page([row(99)]));
    expect(await load, false);
    expect(p.messages.any((m) => m.id == 'row-99'), false);
  });
  test('409 restart explicitly requests fresh scope without replaying expired cursor', () async {
    final cursors = <String?>[];
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      cursors.add(cursor);
      return cursor == null
          ? page([row(2)], cursor: 'fresh.token')
          : const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.unavailable, statusCode: 409));
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), false);
    expect(await p.loadEarlierMessages(), false);
    await p.refreshMessages();
    expect(cursors, [null, 'fresh.token', null]);
    expect(p.historyCursorNeedsRefresh, false);
  });
  test('403 owner mismatch retires cursor without retry or row adoption', () async {
    var older = 0;
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      if (cursor == null) return page([row(2)], cursor: 'first.token');
      older++;
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged, statusCode: 403));
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), false);
    expect(await p.loadEarlierMessages(), false);
    expect(older, 1);
    expect(p.canLoadEarlierHistory, false);
    expect(p.messages.single.id, 'row-2');
  });
  test('dispose retires held page and closes its invalidation callback', () async {
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          cursor == null ? page([row(2)], cursor: 'first.token') : await pending.future,
    );
    await p.refreshMessages();
    final load = p.loadEarlierMessages();
    p.dispose();
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    pending.complete(page([row(99)]));
    expect(await load, false);
    expect(SharedPreferencesUtil().cachedMessages.any((m) => m.id == 'row-99'), false);
  });
  for (final sending in [false, true]) {
    test('latest cursor is not admitted when corresponding rows cannot commit sending=$sending', () async {
      final response = Completer<EllaServiceResult<EllaChatHistoryPage>>();
      final p = provider(
        ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => await response.future,
      );
      addTearDown(p.dispose);
      p.messages = [row(9, local: true)];
      p.sendingMessage = sending;
      final refresh = p.refreshMessages();
      if (!sending) p.messages.add(row(8, local: true));
      response.complete(page([row(2)], cursor: 'unadopted.token'));
      await refresh;
      expect(p.canLoadEarlierHistory, false);
      expect(p.messages.any((m) => m.id == 'row-2'), false);
    });
  }
  test('empty-but-continuing advances verbatim then exhaustion closes control', () async {
    final calls = <String?>[];
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      calls.add(cursor);
      return page(
        [],
        cursor: cursor == null
            ? 'first.token'
            : cursor == 'first.token'
                ? 'second.token'
                : null,
      );
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(p.showsEarlierHistoryControl, true);
    expect(await p.loadEarlierMessages(), true);
    expect(await p.loadEarlierMessages(), true);
    expect(calls, [null, 'first.token', 'second.token']);
    expect(p.showsEarlierHistoryControl, false);
  });
  for (final staleSuccess in [false, true]) {
    test('latest refresh retires in-flight page staleSuccess=$staleSuccess', () async {
      final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
      var latest = 0;
      final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
        if (cursor != null) return pending.future;
        return page([row(++latest)], cursor: 'new.token');
      });
      addTearDown(p.dispose);
      await p.refreshMessages();
      final request = p.loadEarlierMessages();
      expect(p.isLoadingEarlierHistory, true);
      await p.refreshMessages();
      final ids = p.messages.map((m) => m.canonicalEventKey).toList();
      pending.complete(
        staleSuccess
            ? page([row(99)])
            : const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)),
      );
      expect(await request, false);
      expect(p.messages.map((m) => m.canonicalEventKey), ids);
      expect(p.earlierHistoryFailure, isNull);
      expect(p.isLoadingEarlierHistory, false);
    });
    test('A-B-A reset cannot adopt old page staleSuccess=$staleSuccess', () async {
      final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
      final p = provider(
        ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
            cursor == null ? page([row(2)], cursor: 'new.token') : await pending.future,
      );
      addTearDown(p.dispose);
      await p.refreshMessages();
      final request = p.loadEarlierMessages();
      EllaAccountCommitBarrier.quiesceForAccountTransition();
      SharedPreferencesUtil().uid = 'owner-b';
      p.reset();
      SharedPreferencesUtil().uid = 'owner-a';
      await p.refreshMessages();
      pending.complete(
        staleSuccess
            ? page([row(99)])
            : const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.unavailable)),
      );
      expect(await request, false);
      expect(p.messages.map((m) => m.id), ['row-2']);
      expect(p.earlierHistoryFailure, isNull);
    });
  }
  test('parallel taps admit only one older request', () async {
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var older = 0;
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      if (cursor == null) return page([row(2)], cursor: 'first.token');
      older++;
      return pending.future;
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    final first = p.loadEarlierMessages();
    expect(await p.loadEarlierMessages(), false);
    pending.complete(page([row(1, day: 11)]));
    expect(await first, true);
    expect(older, 1);
  });
  test('reentrant reset before transport prevents dispatch', () async {
    var older = 0;
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      if (cursor != null) older++;
      return page([row(2)], cursor: 'first.token');
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    p.addListener(() {
      if (p.isLoadingEarlierHistory) p.reset();
    });
    expect(await p.loadEarlierMessages(), false);
    expect(older, 0);
  });
  test('legacy cache migration and local delivery/active placeholder remain separate', () async {
    SharedPreferencesUtil().cachedMessages = [row(2, local: true)];
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          cursor == null ? page([row(2)], cursor: 'first.token') : page([row(1, day: 11)]),
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(p.messages.length, 1);
    final local = row(9, local: true)..clientDeliveryState = ClientMessageDeliveryState.pending;
    p.messages.add(local);
    p.messages.add(row(10, local: true));
    p.sendingMessage = true;
    final draftEpoch = p.localDraftResetGeneration;
    expect(await p.loadEarlierMessages(), false);
    expect(p.messages, contains(local));
    expect(local.clientDeliveryState, ClientMessageDeliveryState.pending);
    expect(p.sendingMessage, true);
    expect(p.messages.any((m) => m.id == 'row-10'), true);
    expect(p.localDraftResetGeneration, draftEpoch);
  });
  for (final code in [409, 422, 503]) {
    test('typed page error$code preserves timeline and truthful explicit recovery', () async {
      final p = provider(
        ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => cursor == null
            ? page([row(2)], cursor: 'first.token')
            : EllaServiceResult.failure(
                ClientApiFailure(ClientApiFailureKind.unavailable, statusCode: code, retryable: code == 503),
              ),
      );
      addTearDown(p.dispose);
      await p.refreshMessages();
      expect(await p.loadEarlierMessages(), false);
      expect(p.messages.single.id, 'row-2');
      expect(p.isLoadingEarlierHistory, false);
      expect(p.historyCursorNeedsRefresh, code != 503);
      expect(p.canLoadEarlierHistory, code == 503);
      expect(p.lastHistoryFailure, isNull);
    });
  }
  test('nonprogressing continuation rejects without looping', () async {
    final p = provider(
      ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async =>
          page([row(2)], cursor: 'first.token'),
    );
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), false);
    expect(p.earlierHistoryFailure?.kind, ClientApiFailureKind.invalidResponse);
    expect(p.messages.length, 1);
  });
  test('exception is closed and loading clears', () async {
    final p = provider(({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async {
      if (cursor != null) throw StateError('private sentinel');
      return page([row(2)], cursor: 'first.token');
    });
    addTearDown(p.dispose);
    await p.refreshMessages();
    expect(await p.loadEarlierMessages(), false);
    expect(p.isLoadingEarlierHistory, false);
    expect(p.earlierHistoryFailure?.kind, ClientApiFailureKind.unavailable);
  });
}

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}
