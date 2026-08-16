import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_turn_reconciler.dart';
import 'package:omi/services/wals/wal.dart';

const ownerNamespaceA = 'aaaaaaaaaaaaaaaaaaaaaaaa';
const ownerNamespaceB = 'bbbbbbbbbbbbbbbbbbbbbbbb';
const authorityFingerprintA = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const authorityFingerprintB = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

bool authorityIsCurrent() => true;

Future<bool> beginSession(
  V2VTurnReconciler reconciler, {
  String uid = 'uid-a',
  String ownerNamespace = ownerNamespaceA,
  String authorityFingerprint = authorityFingerprintA,
  required String sessionId,
  required bool Function() isAuthorityCurrent,
  int authorityGenerationAtCapture = 0,
}) =>
    reconciler.beginSession(
      uid: uid,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
      sessionId: sessionId,
      isAuthorityCurrent: isAuthorityCurrent,
      authorityGenerationAtCapture: authorityGenerationAtCapture,
    );

V2VTerminalTranscriptTurn terminalTurn({
  required String sessionId,
  required int ordinal,
  String user = 'Question',
  String assistant = 'Answer',
}) {
  final turnId = 'v2v-turn-${ordinal.toRadixString(16).padLeft(32, '0')}';
  return V2VTerminalTranscriptTurn(
    sessionId: sessionId,
    turnId: turnId,
    userEventId: '$turnId:user',
    assistantEventId: '$turnId:assistant',
    userTranscript: user,
    assistantTranscript: assistant,
    startedAt: DateTime.utc(2026, 8, 15, 20, 0, ordinal),
    completedAt: DateTime.utc(2026, 8, 15, 20, 0, ordinal + 1),
  );
}

V2VTranscriptTurn ownedTurn({
  required String sessionId,
  required int ordinal,
  String uid = 'uid-a',
  String ownerNamespace = ownerNamespaceA,
  String authorityFingerprint = authorityFingerprintA,
}) {
  final terminal = terminalTurn(sessionId: sessionId, ordinal: ordinal);
  return V2VTranscriptTurn(
    uid: uid,
    ownerNamespace: ownerNamespace,
    authorityFingerprint: authorityFingerprint,
    sessionId: terminal.sessionId,
    turnId: terminal.turnId,
    userEventId: terminal.userEventId,
    assistantEventId: terminal.assistantEventId,
    userTranscript: terminal.userTranscript,
    assistantTranscript: terminal.assistantTranscript,
    startedAt: terminal.startedAt,
    completedAt: terminal.completedAt,
  );
}

class _SlowHydrationStore implements V2VTurnDurableStore {
  _SlowHydrationStore(this.delegate, this.releaseHydration);

  final V2VTurnDurableStore delegate;
  final Future<void> releaseHydration;

  @override
  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) =>
      delegate.clearOwner(
        uid: uid,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
        isAuthorityCurrent: isAuthorityCurrent,
      );

  @override
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    await releaseHydration;
    return delegate.load(
      uid: uid,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  @override
  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      delegate.put(turn, isAuthorityCurrent: isAuthorityCurrent);

  @override
  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      delegate.remove(turn, isAuthorityCurrent: isAuthorityCurrent);
}

class _BlockingPutStore implements V2VTurnDurableStore {
  _BlockingPutStore(this.releasePut);

  final Future<void> releasePut;
  final MemoryV2VTurnDurableStore delegate = MemoryV2VTurnDurableStore();
  final List<(String, String, String)> clearedOwners = [];

  @override
  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    clearedOwners.add((uid, ownerNamespace, authorityFingerprint));
    await delegate.clearOwner(
      uid: uid,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  @override
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) =>
      delegate.load(
        uid: uid,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
        isAuthorityCurrent: isAuthorityCurrent,
      );

  @override
  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) async {
    await releasePut;
    await delegate.put(turn, isAuthorityCurrent: isAuthorityCurrent);
  }

  @override
  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      delegate.remove(turn, isAuthorityCurrent: isAuthorityCurrent);
}

class _RetryInterleavingStore implements V2VTurnDurableStore {
  final MemoryV2VTurnDurableStore delegate = MemoryV2VTurnDurableStore();
  final Completer<void> staleRetryEntered = Completer<void>();
  final Completer<void> releaseStaleRetry = Completer<void>();
  int clearAttempts = 0;

  @override
  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    clearAttempts++;
    if (clearAttempts == 1) throw const FileSystemException('injected transient cleanup failure');
    if (!staleRetryEntered.isCompleted) staleRetryEntered.complete();
    await releaseStaleRetry.future;
    await delegate.clearOwner(
      uid: uid,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  @override
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) =>
      delegate.load(
        uid: uid,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
        isAuthorityCurrent: isAuthorityCurrent,
      );

  @override
  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      delegate.put(turn, isAuthorityCurrent: isAuthorityCurrent);

  @override
  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      delegate.remove(turn, isAuthorityCurrent: isAuthorityCurrent);
}

void main() {
  group('V2VTurnReconciler', () {
    test('persists only one complete proxy-owned terminal turn', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      expect(await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true), isTrue);

      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await reconciler.settle();

      expect(writes, hasLength(1));
      expect(writes.single.sessionId, 'session-1');
      expect(writes.single.turnId, 'v2v-turn-00000000000000000000000000000001');
      expect(writes.single.userEventId, '${writes.single.turnId}:user');
      expect(writes.single.assistantEventId, '${writes.single.turnId}:assistant');
      expect(writes.single.userTranscript, 'Question');
      expect(writes.single.assistantTranscript, 'Answer');
    });

    test('assigns and persists chronology independent of reverse-lexical production turn ids', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-chronology-');
      try {
        final writes = <V2VTranscriptTurn>[];
        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        final reconciler = V2VTurnReconciler(
          durableStore: store,
          writer: (turn) async {
            writes.add(turn);
            return false;
          },
        );
        await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
        final timestamp = DateTime.utc(2026, 8, 15, 20);
        V2VTerminalTranscriptTurn turn(String turnId) => V2VTerminalTranscriptTurn(
              sessionId: 'session-1',
              turnId: turnId,
              userEventId: '$turnId:user',
              assistantEventId: '$turnId:assistant',
              userTranscript: 'Question',
              assistantTranscript: 'Answer',
              startedAt: timestamp,
              completedAt: timestamp,
            );
        const firstTurn = 'v2v-turn-ffffffffffffffffffffffffffffffff';
        const secondTurn = 'v2v-turn-00000000000000000000000000000000';

        expect(await reconciler.addTerminalTurn('session-1', turn(firstTurn)), isTrue);
        expect(await reconciler.addTerminalTurn('session-1', turn(secondTurn)), isTrue);
        await reconciler.settle();
        final durable = await FileV2VTurnDurableStore(baseDirectory: directory).load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        );

        expect(durable.map((turn) => turn.turnId), [firstTurn, secondTurn]);
        expect(durable.map((turn) => turn.turnOrdinal), [0, 1]);
        expect(writes, isNotEmpty);
        expect(writes.every((turn) => turn.turnOrdinal == 0), isTrue);
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('preserves two legitimate identical consecutive utterances by stable turn identity', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);

      reconciler.addTerminalTurn(
        'session-1',
        terminalTurn(sessionId: 'session-1', ordinal: 1, user: 'yes', assistant: 'First answer'),
      );
      reconciler.addTerminalTurn(
        'session-1',
        terminalTurn(sessionId: 'session-1', ordinal: 2, user: 'yes', assistant: 'Second answer'),
      );
      await reconciler.settle();

      expect(writes.map((turn) => turn.userTranscript), ['yes', 'yes']);
      expect(writes.map((turn) => turn.assistantTranscript), ['First answer', 'Second answer']);
      expect(writes.map((turn) => turn.turnId).toSet(), hasLength(2));
    });

    test('suppresses an exact repeated terminal frame by stable identities', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
      final terminal = terminalTurn(sessionId: 'session-1', ordinal: 1);

      reconciler.addTerminalTurn('session-1', terminal);
      reconciler.addTerminalTurn('session-1', terminal);
      await reconciler.settle();

      expect(writes, hasLength(1));
    });

    test('reconnect isolates late events and retries an ended authorized session safely', () async {
      var attempts = 0;
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          attempts++;
          if (attempts == 1) return false;
          writes.add(turn);
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await reconciler.settle();
      reconciler.endSession('session-1');

      expect(reconciler.sessionCountForTesting, 1);
      expect(reconciler.pendingTurnCountForTesting('session-1'), 1);
      expect(await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true), isTrue);
      await reconciler.settle();
      reconciler.endSession('session-1');

      expect(attempts, 2);
      expect(writes, hasLength(1));
      expect(reconciler.sessionCountForTesting, 0);

      await beginSession(reconciler, sessionId: 'session-2', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 2));
      reconciler.addTerminalTurn('session-2', terminalTurn(sessionId: 'session-2', ordinal: 1));
      await reconciler.settle();
      expect(writes.map((turn) => turn.sessionId), ['session-1', 'session-2']);
    });

    test('authority loss rejects a new terminal turn with zero writer side effects', () async {
      var authorityCurrent = true;
      var writes = 0;
      final reconciler = V2VTurnReconciler(
        writer: (_) async {
          writes++;
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
      authorityCurrent = false;

      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      reconciler.retryAuthorizedPending();
      await reconciler.settle();

      expect(writes, 0);
      expect(reconciler.sessionCountForTesting, 0);
    });

    test('failed backend attempt retries the same stable turn on an authorized signal', () async {
      var attempts = 0;
      var failures = 0;
      final attemptedKeys = <String>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          attempts++;
          attemptedKeys.add('${turn.sessionId}:${turn.turnId}');
          return attempts > 1;
        },
        onWriteFailure: () => failures++,
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await reconciler.settle();

      expect(attempts, 1);
      expect(failures, 1);
      reconciler.retryAuthorizedPending();
      await reconciler.settle();

      expect(attempts, 2);
      expect(attemptedKeys.toSet(), {'session-1:v2v-turn-00000000000000000000000000000001'});
    });

    test('retry signal during an in-flight failure is not lost', () async {
      final firstAttemptStarted = Completer<void>();
      final releaseFirstAttempt = Completer<void>();
      var attempts = 0;
      final reconciler = V2VTurnReconciler(
        writer: (_) async {
          attempts++;
          if (attempts == 1) {
            firstAttemptStarted.complete();
            await releaseFirstAttempt.future;
            return false;
          }
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));

      await firstAttemptStarted.future;
      reconciler.retryAuthorizedPending();
      releaseFirstAttempt.complete();
      await reconciler.settle();

      expect(attempts, 2);
    });

    test('later accepted turns reach the durable outbox before an in-flight canonical write finishes', () async {
      final store = MemoryV2VTurnDurableStore();
      final firstWriteStarted = Completer<void>();
      final releaseFirstWrite = Completer<void>();
      final firstPage = V2VTurnReconciler(
        durableStore: store,
        writer: (_) async {
          firstWriteStarted.complete();
          await releaseFirstWrite.future;
          return false;
        },
      );
      await beginSession(firstPage, sessionId: 'session-1', isAuthorityCurrent: () => true);
      expect(
        await firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1)),
        isTrue,
      );
      await firstWriteStarted.future;

      final laterAcceptances = await Future.wait([
        firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 2)),
        firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 3)),
      ]);

      expect(laterAcceptances, everyElement(isTrue));
      expect(
        (await store.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        ))
            .map((turn) => turn.turnId),
        [
          'v2v-turn-00000000000000000000000000000001',
          'v2v-turn-00000000000000000000000000000002',
          'v2v-turn-00000000000000000000000000000003',
        ],
      );

      firstPage.endSession('session-1');
      releaseFirstWrite.complete();
      await firstPage.settle();

      final relaunchedWrites = <V2VTranscriptTurn>[];
      final relaunchedPage = V2VTurnReconciler(
        durableStore: store,
        writer: (turn) async {
          relaunchedWrites.add(turn);
          return true;
        },
      );
      await beginSession(relaunchedPage, sessionId: 'session-2', isAuthorityCurrent: () => true);
      await relaunchedPage.settle();

      expect(relaunchedWrites.map((turn) => turn.turnId), [
        'v2v-turn-00000000000000000000000000000001',
        'v2v-turn-00000000000000000000000000000002',
        'v2v-turn-00000000000000000000000000000003',
      ]);
      expect(
        await store.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        ),
        isEmpty,
      );
    });

    test('listener and microphone wait for slow large outbox hydration and session arming', () async {
      final backingStore = MemoryV2VTurnDurableStore();
      for (var ordinal = 1; ordinal <= 200; ordinal++) {
        await backingStore.put(
          ownedTurn(sessionId: 'restored-session', ordinal: ordinal),
          isAuthorityCurrent: authorityIsCurrent,
        );
      }
      final hydrationGate = Completer<void>();
      final reconciler = V2VTurnReconciler(
        durableStore: _SlowHydrationStore(backingStore, hydrationGate.future),
        writer: (_) async => true,
      );
      final order = <String>[];

      final activation = activateV2VTransportInOrder(
        armSession: () async {
          order.add('hydrate-started');
          final armed = await beginSession(
            reconciler,
            sessionId: 'active-session',
            isAuthorityCurrent: () => true,
          );
          order.add('session-armed');
          return armed;
        },
        attachListener: () => order.add('listener-attached'),
        startMicrophone: () async {
          order.add('microphone-started');
          return true;
        },
      );
      await Future<void>.delayed(Duration.zero);

      expect(order, ['hydrate-started']);
      hydrationGate.complete();
      expect(await activation, isTrue);
      expect(order, ['hydrate-started', 'session-armed', 'listener-attached', 'microphone-started']);

      await reconciler.settle();
      expect(
        await backingStore.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        ),
        isEmpty,
      );
    });

    test('failed session arming attaches neither listener nor microphone', () async {
      final order = <String>[];

      expect(
        await activateV2VTransportInOrder(
          armSession: () async {
            order.add('arming-rejected');
            return false;
          },
          attachListener: () => order.add('listener-attached'),
          startMicrophone: () async {
            order.add('microphone-started');
            return true;
          },
        ),
        isFalse,
      );
      expect(order, ['arming-rejected']);
    });

    test('authority loss waits for an in-flight enqueue then clears only the exact owner', () async {
      var authorityCurrent = true;
      final releasePut = Completer<void>();
      final store = _BlockingPutStore(releasePut.future);
      var writes = 0;
      final reconciler = V2VTurnReconciler(
        durableStore: store,
        writer: (_) async {
          writes++;
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
      final acceptance = reconciler.addTerminalTurn(
        'session-1',
        terminalTurn(sessionId: 'session-1', ordinal: 1),
      );

      authorityCurrent = false;
      reconciler.retryAuthorizedPending();
      expect(store.clearedOwners, isEmpty);
      releasePut.complete();

      expect(await acceptance, isFalse);
      await reconciler.settle();
      expect(writes, 0);
      expect(store.clearedOwners, [('uid-a', ownerNamespaceA, authorityFingerprintA)]);
      expect(
        await store.delegate.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        ),
        isEmpty,
      );
      expect(reconciler.sessionCountForTesting, 0);
    });

    test('ended session is evicted only after its in-flight write settles', () async {
      final writeStarted = Completer<void>();
      final releaseWrite = Completer<void>();
      final reconciler = V2VTurnReconciler(
        writer: (_) async {
          writeStarted.complete();
          await releaseWrite.future;
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await writeStarted.future;

      reconciler.endSession('session-1');
      expect(reconciler.sessionCountForTesting, 1);
      releaseWrite.complete();
      await reconciler.settle();

      expect(reconciler.sessionCountForTesting, 0);
    });

    test('unauthorized session is evicted only after its in-flight write settles', () async {
      var authorityCurrent = true;
      final writeStarted = Completer<void>();
      final releaseWrite = Completer<void>();
      final reconciler = V2VTurnReconciler(
        writer: (_) async {
          writeStarted.complete();
          await releaseWrite.future;
          return true;
        },
      );
      await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await writeStarted.future;

      authorityCurrent = false;
      reconciler.endSession('session-1');
      expect(reconciler.sessionCountForTesting, 1);
      releaseWrite.complete();
      await reconciler.settle();

      expect(reconciler.sessionCountForTesting, 0);
    });

    test('failed turn survives disposed page ownership and retries after page recreation', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-page-recreate-');
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        final writeStarted = Completer<void>();
        final finishFailedWrite = Completer<void>();
        final firstPage = V2VTurnReconciler(
          durableStore: store,
          writer: (_) async {
            writeStarted.complete();
            await finishFailedWrite.future;
            return false;
          },
        );
        await beginSession(firstPage, sessionId: 'session-1', isAuthorityCurrent: () => true);
        firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
        await writeStarted.future;
        firstPage.endSession('session-1');
        finishFailedWrite.complete();
        await firstPage.settle();

        final retried = <V2VTranscriptTurn>[];
        final recreatedPage = V2VTurnReconciler(
          durableStore: store,
          writer: (turn) async {
            retried.add(turn);
            return true;
          },
        );
        await beginSession(recreatedPage, sessionId: 'session-2', isAuthorityCurrent: () => true);
        await recreatedPage.settle();

        expect(retried.map((turn) => '${turn.sessionId}:${turn.turnId}'), [
          'session-1:v2v-turn-00000000000000000000000000000001',
        ]);
        expect(
          await store.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('restart-style store instance hydrates the same failed stable turn from disk', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-outbox-');
      try {
        final failedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final attemptedTurnIds = <String>[];
        final firstPage = V2VTurnReconciler(
          durableStore: failedStore,
          writer: (turn) async {
            attemptedTurnIds.add(turn.turnId);
            return false;
          },
        );
        await beginSession(firstPage, sessionId: 'session-1', isAuthorityCurrent: () => true);
        firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
        await firstPage.settle();
        firstPage.endSession('session-1');

        final durableAfterFailure = await failedStore.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        );
        expect(durableAfterFailure.map((turn) => turn.turnId), [
          'v2v-turn-00000000000000000000000000000001',
        ]);

        final committed = <V2VTranscriptTurn>[];
        final nextPageStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final nextPage = V2VTurnReconciler(
          durableStore: nextPageStore,
          writer: (turn) async {
            committed.add(turn);
            return true;
          },
        );
        await beginSession(nextPage, sessionId: 'session-2', isAuthorityCurrent: () => true);
        await nextPage.settle();

        expect(attemptedTurnIds, ['v2v-turn-00000000000000000000000000000001']);
        expect(committed.map((turn) => '${turn.sessionId}:${turn.turnId}'), [
          'session-1:v2v-turn-00000000000000000000000000000001',
        ]);
        expect(
          await nextPageStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('second file-store instance recovers while the first reconciler network write is still in flight', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-process-death-');
      try {
        final firstWriteStarted = Completer<void>();
        final releaseFirstWrite = Completer<void>();
        final firstPage = V2VTurnReconciler(
          durableStore: FileV2VTurnDurableStore(baseDirectory: directory),
          writer: (_) async {
            firstWriteStarted.complete();
            await releaseFirstWrite.future;
            return false;
          },
        );
        await beginSession(firstPage, sessionId: 'session-1', isAuthorityCurrent: () => true);
        expect(
          await firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1)),
          isTrue,
        );
        await firstWriteStarted.future;

        final recovered = <V2VTranscriptTurn>[];
        final relaunchedPage = V2VTurnReconciler(
          durableStore: FileV2VTurnDurableStore(baseDirectory: directory),
          writer: (turn) async {
            recovered.add(turn);
            return true;
          },
        );
        await beginSession(relaunchedPage, sessionId: 'session-2', isAuthorityCurrent: () => true);
        await relaunchedPage.settle();

        expect(recovered.map((turn) => turn.turnId), [
          'v2v-turn-00000000000000000000000000000001',
        ]);
        final restartedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        expect(
          await restartedStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );

        releaseFirstWrite.complete();
        await firstPage.settle();
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('cold start sweeps an unmarked obsolete authority without crossing owners or deleting current', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-authority-generation-');
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        await store.put(ownedTurn(sessionId: 'authority-a', ordinal: 1), isAuthorityCurrent: authorityIsCurrent);
        await store.put(
          ownedTurn(
            sessionId: 'authority-b',
            ordinal: 2,
            authorityFingerprint: authorityFingerprintB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );
        final currentCleanupMarker = File(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintB/cleanup_required.json',
        );
        await currentCleanupMarker.writeAsString(
          jsonEncode({
            'version': 2,
            'uid': 'uid-a',
            'owner_namespace': ownerNamespaceA,
            'authority_fingerprint': authorityFingerprintB,
            'status': 'cleanup_required',
          }),
          flush: true,
        );
        await store.put(
          ownedTurn(
            sessionId: 'other-owner',
            ordinal: 3,
            uid: 'uid-b',
            ownerNamespace: ownerNamespaceB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );

        final restartedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final currentTurns = await restartedStore.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintB,
          isAuthorityCurrent: authorityIsCurrent,
        );

        expect(currentTurns.map((turn) => turn.sessionId), ['authority-b']);
        expect(
          Directory('${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA').existsSync(),
          isFalse,
        );
        expect(
          Directory('${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintB').existsSync(),
          isTrue,
        );
        expect(currentCleanupMarker.existsSync(), isTrue);
        expect(
          (await restartedStore.load(
            uid: 'uid-b',
            ownerNamespace: ownerNamespaceB,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ))
              .map((turn) => turn.sessionId),
          ['other-owner'],
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('authority replacement after obsolete marker write aborts deletion and preserves the new authority', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-authority-switch-');
      try {
        final seedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        await seedStore.put(
          ownedTurn(sessionId: 'authority-a', ordinal: 1),
          isAuthorityCurrent: authorityIsCurrent,
        );
        await seedStore.put(
          ownedTurn(
            sessionId: 'authority-b',
            ordinal: 2,
            authorityFingerprint: authorityFingerprintB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );
        var authorityAIsCurrent = true;
        var authorityBIsCurrent = false;
        final sweepingStore = FileV2VTurnDurableStore(
          baseDirectory: directory,
          afterCleanupMarkerWriteForTesting: (markedDirectory) async {
            expect(markedDirectory.path, endsWith(authorityFingerprintB));
            authorityAIsCurrent = false;
            authorityBIsCurrent = true;
          },
        );

        await expectLater(
          sweepingStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: () => authorityAIsCurrent,
          ),
          throwsA(isA<V2VAuthorityChangedException>()),
        );

        final authorityBDirectory = Directory(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintB',
        );
        expect(authorityBDirectory.existsSync(), isTrue);
        final authorityBTurns = await FileV2VTurnDurableStore(baseDirectory: directory).load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintB,
          isAuthorityCurrent: () => authorityBIsCurrent,
        );
        expect(authorityBTurns.map((turn) => turn.sessionId), ['authority-b']);
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('clearOwner revalidates its cleanup lease after marker write and leaves replacement authority untouched',
        () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-clear-authority-switch-');
      try {
        final seedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        await seedStore.put(
          ownedTurn(sessionId: 'authority-a', ordinal: 1),
          isAuthorityCurrent: authorityIsCurrent,
        );
        await seedStore.put(
          ownedTurn(
            sessionId: 'authority-b',
            ordinal: 2,
            authorityFingerprint: authorityFingerprintB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );
        var cleanupLeaseIsCurrent = true;
        final clearingStore = FileV2VTurnDurableStore(
          baseDirectory: directory,
          afterCleanupMarkerWriteForTesting: (_) async {
            cleanupLeaseIsCurrent = false;
          },
        );

        await expectLater(
          clearingStore.clearOwner(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: () => cleanupLeaseIsCurrent,
          ),
          throwsA(isA<V2VAuthorityChangedException>()),
        );

        expect(
          File(
            '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA/pending_turns.json',
          ).existsSync(),
          isTrue,
        );
        expect(
          File(
            '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintB/pending_turns.json',
          ).existsSync(),
          isTrue,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('symlinked configured root is rejected without touching its target', () async {
      final parent = await Directory.systemTemp.createTemp('ella-v2v-turn-symlink-root-');
      final external = Directory('${parent.path}/external')..createSync();
      final sentinel = File('${external.path}/sentinel')..writeAsStringSync('outside');
      final linkedRoot = Link('${parent.path}/linked-root')..createSync(external.path);
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: Directory(linkedRoot.path));
        await expectLater(
          store.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FileSystemException>()),
        );
        expect(sentinel.readAsStringSync(), 'outside');
      } finally {
        await linkedRoot.delete();
        await parent.delete(recursive: true);
      }
    });

    test('symlinked owner is rejected without touching an external authority tree', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-symlink-owner-');
      final external = await Directory.systemTemp.createTemp('ella-v2v-turn-external-owner-');
      final sentinel = File('${external.path}/sentinel')..writeAsStringSync('outside');
      final outbox = Directory('${directory.path}/ella_v2v_turn_outbox')..createSync();
      final linkedOwner = Link('${outbox.path}/$ownerNamespaceA')..createSync(external.path);
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        await expectLater(
          store.clearOwner(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FileSystemException>()),
        );
        expect(sentinel.readAsStringSync(), 'outside');
      } finally {
        await linkedOwner.delete();
        await directory.delete(recursive: true);
        await external.delete(recursive: true);
      }
    });

    test('owner ancestor swap before deletion aborts without touching the replacement target', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-ancestor-swap-');
      final external = await Directory.systemTemp.createTemp('ella-v2v-turn-external-swap-');
      final sentinel = File('${external.path}/sentinel')..writeAsStringSync('outside');
      final ownerPath = '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA';
      final parkedOwner = Directory('$ownerPath.parked');
      Link? replacement;
      try {
        final seedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        await seedStore.put(
          ownedTurn(sessionId: 'authority-a', ordinal: 1),
          isAuthorityCurrent: authorityIsCurrent,
        );
        final swappingStore = FileV2VTurnDurableStore(
          baseDirectory: directory,
          beforeCleanupDeleteForTesting: (_) async {
            await Directory(ownerPath).rename(parkedOwner.path);
            replacement = Link(ownerPath)..createSync(external.path);
          },
        );

        await expectLater(
          swappingStore.clearOwner(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FileSystemException>()),
        );

        expect(sentinel.readAsStringSync(), 'outside');
        expect(
          File('${parkedOwner.path}/$authorityFingerprintA/pending_turns.json').existsSync(),
          isTrue,
        );
      } finally {
        if (replacement?.existsSync() == true) await replacement!.delete();
        await directory.delete(recursive: true);
        await external.delete(recursive: true);
      }
    });

    test('manifest and every record persist and verify the exact authority fingerprint', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-fingerprint-');
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        await store.put(ownedTurn(sessionId: 'session-1', ordinal: 1), isAuthorityCurrent: authorityIsCurrent);
        final manifest = File(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA/pending_turns.json',
        );
        final decoded = Map<String, dynamic>.from(jsonDecode(await manifest.readAsString()) as Map);
        expect(decoded['version'], 2);
        expect(decoded['authority_fingerprint'], authorityFingerprintA);
        final records = decoded['turns'] as List;
        expect(Map<String, dynamic>.from(records.single as Map)['authority_fingerprint'], authorityFingerprintA);

        final tamperedRecord = Map<String, dynamic>.from(records.single as Map)
          ..['authority_fingerprint'] = authorityFingerprintB;
        decoded['turns'] = [tamperedRecord];
        await manifest.writeAsString(jsonEncode(decoded), flush: true);

        await expectLater(
          store.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FormatException>()),
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('legacy coarse manifest is deleted without replay under an exact authority', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-legacy-');
      try {
        final ownerDirectory = Directory('${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA')
          ..createSync(recursive: true);
        final legacyManifest = File('${ownerDirectory.path}/pending_turns.json');
        legacyManifest.writeAsStringSync(jsonEncode({
          'version': 1,
          'uid': 'uid-a',
          'owner_namespace': ownerNamespaceA,
          'turns': [ownedTurn(sessionId: 'legacy-session', ordinal: 1).toJson()..remove('authority_fingerprint')],
        }));

        final store = FileV2VTurnDurableStore(baseDirectory: directory);
        expect(
          await store.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintB,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );
        expect(legacyManifest.existsSync(), isFalse);
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('failed unauthorized deletion retries autonomously with bounded backoff', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-cleanup-retry-');
      try {
        var authorityCurrent = true;
        var deleteAttempts = 0;
        final store = FileV2VTurnDurableStore(
          baseDirectory: directory,
          beforeCleanupDeleteForTesting: (_) async {
            deleteAttempts++;
            if (deleteAttempts < 3) throw const FileSystemException('injected cleanup failure');
          },
        );
        final reconciler = V2VTurnReconciler(
          durableStore: store,
          writer: (_) async => false,
          unauthorizedCleanupRetryDelays: const [Duration.zero, Duration.zero, Duration.zero],
        );
        await beginSession(reconciler, sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
        await reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
        await reconciler.settle();

        authorityCurrent = false;
        reconciler.retryAuthorizedPending();
        await reconciler.settle();

        expect(deleteAttempts, 3);
        expect(reconciler.sessionCountForTesting, 0);
        expect(
          await FileV2VTurnDurableStore(baseDirectory: directory).load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('stale generation cleanup retry cannot delete replacement generation pending state', () async {
      var authorityAIsCurrent = true;
      var authorityBIsCurrent = true;
      const authorityA = WalOwner(
        uid: 'uid-a',
        profileBindingId: 'binding-a',
        bindingRevision: 7,
        consentReceiptId: 'receipt-a',
        authorityGenerationAtCapture: 41,
      );
      const authorityB = WalOwner(
        uid: 'uid-a',
        profileBindingId: 'binding-a',
        bindingRevision: 7,
        consentReceiptId: 'receipt-a',
        authorityGenerationAtCapture: 42,
      );
      expect(authorityB.storageNamespace, authorityA.storageNamespace);
      expect(authorityB.authorityFingerprint, authorityA.authorityFingerprint);
      final store = _RetryInterleavingStore();
      final reconcilerA = V2VTurnReconciler(
        durableStore: store,
        writer: (_) async => false,
        unauthorizedCleanupRetryDelays: const [Duration.zero],
      );
      await beginSession(
        reconcilerA,
        uid: authorityA.uid,
        ownerNamespace: authorityA.storageNamespace,
        authorityFingerprint: authorityA.authorityFingerprint,
        sessionId: 'generation-a',
        isAuthorityCurrent: () => authorityAIsCurrent,
        authorityGenerationAtCapture: authorityA.authorityGenerationAtCapture,
      );
      expect(
        await reconcilerA.addTerminalTurn('generation-a', terminalTurn(sessionId: 'generation-a', ordinal: 1)),
        isTrue,
      );
      await reconcilerA.settle();

      authorityAIsCurrent = false;
      reconcilerA.retryAuthorizedPending();
      await store.staleRetryEntered.future;
      expect(store.clearAttempts, 2);

      final reconcilerB = V2VTurnReconciler(durableStore: store, writer: (_) async => false);
      expect(
        await beginSession(
          reconcilerB,
          uid: authorityB.uid,
          ownerNamespace: authorityB.storageNamespace,
          authorityFingerprint: authorityB.authorityFingerprint,
          sessionId: 'generation-b',
          isAuthorityCurrent: () => authorityBIsCurrent,
          authorityGenerationAtCapture: authorityB.authorityGenerationAtCapture,
        ),
        isTrue,
      );
      expect(
        await reconcilerB.addTerminalTurn('generation-b', terminalTurn(sessionId: 'generation-b', ordinal: 2)),
        isTrue,
      );
      expect(reconcilerB.pendingTurnCountForTesting('generation-b'), 1);

      store.releaseStaleRetry.complete();
      await Future.wait([reconcilerA.settle(), reconcilerB.settle()]);
      final durable = await store.delegate.load(
        uid: authorityB.uid,
        ownerNamespace: authorityB.storageNamespace,
        authorityFingerprint: authorityB.authorityFingerprint,
        isAuthorityCurrent: () => authorityBIsCurrent,
      );

      expect(durable.map((turn) => turn.sessionId), contains('generation-b'));
      expect(reconcilerB.pendingTurnCountForTesting('generation-b'), 1);
      expect(authorityBIsCurrent, isTrue);
    });

    test('cleanup marker survives failure and a new store instance clears only that exact authority', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-cleanup-restart-');
      try {
        final failingStore = FileV2VTurnDurableStore(
          baseDirectory: directory,
          beforeCleanupDeleteForTesting: (_) async => throw const FileSystemException('injected cleanup failure'),
        );
        await failingStore.put(
          ownedTurn(sessionId: 'authority-a', ordinal: 1),
          isAuthorityCurrent: authorityIsCurrent,
        );
        await failingStore.put(
          ownedTurn(
            sessionId: 'authority-b',
            ordinal: 2,
            authorityFingerprint: authorityFingerprintB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );
        await expectLater(
          failingStore.clearOwner(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FileSystemException>()),
        );
        final cleanupMarker = File(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA/cleanup_required.json',
        );
        expect(cleanupMarker.existsSync(), isTrue);
        final markerJson = Map<String, dynamic>.from(jsonDecode(await cleanupMarker.readAsString()) as Map);
        expect(markerJson['authority_fingerprint'], authorityFingerprintA);
        expect(markerJson['status'], 'cleanup_required');
        expect(markerJson.containsKey('turns'), isFalse);

        final restartedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final authorityBTurns = await restartedStore.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintB,
          isAuthorityCurrent: authorityIsCurrent,
        );
        expect(authorityBTurns.map((turn) => turn.sessionId), ['authority-b']);
        final obsoleteDirectory = Directory(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA',
        );
        expect(obsoleteDirectory.existsSync(), isFalse);
        expect(
          await FileV2VTurnDurableStore(baseDirectory: directory).load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintB,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          hasLength(1),
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('malformed manifest cleanup is marker-first and survives a restart before deletion', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-cleanup-marker-first-');
      try {
        final failingStore = FileV2VTurnDurableStore(
          baseDirectory: directory,
          afterCleanupMarkerWriteForTesting: (_) async => throw const FileSystemException('injected process death'),
        );
        await failingStore.put(
          ownedTurn(sessionId: 'authority-a', ordinal: 1),
          isAuthorityCurrent: authorityIsCurrent,
        );
        await failingStore.put(
          ownedTurn(
            sessionId: 'authority-b',
            ordinal: 2,
            authorityFingerprint: authorityFingerprintB,
          ),
          isAuthorityCurrent: authorityIsCurrent,
        );
        final obsoleteManifest = File(
          '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA/pending_turns.json',
        );
        final obsoleteBackup = Directory('${obsoleteManifest.path}.bak');
        await obsoleteManifest.writeAsString('{malformed private payload', flush: true);
        await obsoleteBackup.create();
        await File('${obsoleteBackup.path}/unreadable-private-turns').writeAsString('must not be opened', flush: true);
        await expectLater(
          failingStore.clearOwner(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          throwsA(isA<FileSystemException>()),
        );
        expect(
          File(
            '${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA/cleanup_required.json',
          ).existsSync(),
          isTrue,
        );

        final restartedStore = FileV2VTurnDurableStore(baseDirectory: directory);
        expect(
          (await restartedStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintB,
            isAuthorityCurrent: authorityIsCurrent,
          ))
              .map((turn) => turn.sessionId),
          ['authority-b'],
        );
        expect(
          Directory('${directory.path}/ella_v2v_turn_outbox/$ownerNamespaceA/$authorityFingerprintA').existsSync(),
          isFalse,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('different owner cannot retry a turn and terminal authority loss clears it', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-owner-');
      try {
        var authorityCurrent = true;
        final ownerAStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final firstPage = V2VTurnReconciler(
          durableStore: ownerAStore,
          writer: (_) async => false,
        );
        await beginSession(firstPage, sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
        firstPage.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
        await firstPage.settle();

        final crossOwnerWrites = <V2VTranscriptTurn>[];
        final ownerBStore = FileV2VTurnDurableStore(baseDirectory: directory);
        final ownerBPage = V2VTurnReconciler(
          durableStore: ownerBStore,
          writer: (turn) async {
            crossOwnerWrites.add(turn);
            return true;
          },
        );
        await beginSession(
          ownerBPage,
          uid: 'uid-b',
          ownerNamespace: ownerNamespaceB,
          authorityFingerprint: authorityFingerprintB,
          sessionId: 'session-2',
          isAuthorityCurrent: () => true,
        );
        await ownerBPage.settle();

        expect(crossOwnerWrites, isEmpty);
        expect(
          await ownerAStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          hasLength(1),
        );
        expect(
          await ownerBStore.load(
            uid: 'uid-b',
            ownerNamespace: ownerNamespaceB,
            authorityFingerprint: authorityFingerprintB,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );

        authorityCurrent = false;
        firstPage.retryAuthorizedPending();
        await firstPage.settle();
        expect(firstPage.sessionCountForTesting, 0);
        expect(
          await ownerAStore.load(
            uid: 'uid-a',
            ownerNamespace: ownerNamespaceA,
            authorityFingerprint: authorityFingerprintA,
            isAuthorityCurrent: authorityIsCurrent,
          ),
          isEmpty,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('durable outbox rejects overflow without evicting its stable retained turn', () async {
      final directory = await Directory.systemTemp.createTemp('ella-v2v-turn-bounds-');
      try {
        final store = FileV2VTurnDurableStore(baseDirectory: directory, maxPendingTurns: 1);
        final first = ownedTurn(sessionId: 'session-1', ordinal: 1);
        final overflow = ownedTurn(sessionId: 'session-1', ordinal: 2);

        await store.put(first, isAuthorityCurrent: authorityIsCurrent);
        await expectLater(store.put(overflow, isAuthorityCurrent: authorityIsCurrent), throwsStateError);

        final retained = await store.load(
          uid: 'uid-a',
          ownerNamespace: ownerNamespaceA,
          authorityFingerprint: authorityFingerprintA,
          isAuthorityCurrent: authorityIsCurrent,
        );
        expect(retained.map((turn) => turn.turnId), [first.turnId]);
      } finally {
        await directory.delete(recursive: true);
      }
    });

    test('completed reconnect sessions do not accumulate retained transcript state', () async {
      final reconciler = V2VTurnReconciler(writer: (_) async => true);

      for (var index = 1; index <= 25; index++) {
        final sessionId = 'session-$index';
        await beginSession(reconciler, sessionId: sessionId, isAuthorityCurrent: () => true);
        reconciler.addTerminalTurn(sessionId, terminalTurn(sessionId: sessionId, ordinal: index));
        await reconciler.settle();
        reconciler.endSession(sessionId);
      }

      expect(reconciler.sessionCountForTesting, 0);
    });
  });
}
