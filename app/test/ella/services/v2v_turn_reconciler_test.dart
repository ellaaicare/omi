import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_turn_reconciler.dart';

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
      expect(reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true), isTrue);

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

    test('preserves two legitimate identical consecutive utterances by stable turn identity', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);

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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await reconciler.settle();
      reconciler.endSession('session-1');

      expect(reconciler.sessionCountForTesting, 1);
      expect(reconciler.pendingTurnCountForTesting('session-1'), 1);
      expect(reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true), isTrue);
      await reconciler.settle();
      reconciler.endSession('session-1');

      expect(attempts, 2);
      expect(writes, hasLength(1));
      expect(reconciler.sessionCountForTesting, 0);

      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-2', isAuthorityCurrent: () => true);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));

      await firstAttemptStarted.future;
      reconciler.retryAuthorizedPending();
      releaseFirstAttempt.complete();
      await reconciler.settle();

      expect(attempts, 2);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
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
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => authorityCurrent);
      reconciler.addTerminalTurn('session-1', terminalTurn(sessionId: 'session-1', ordinal: 1));
      await writeStarted.future;

      authorityCurrent = false;
      reconciler.endSession('session-1');
      expect(reconciler.sessionCountForTesting, 1);
      releaseWrite.complete();
      await reconciler.settle();

      expect(reconciler.sessionCountForTesting, 0);
    });

    test('completed reconnect sessions do not accumulate retained transcript state', () async {
      final reconciler = V2VTurnReconciler(writer: (_) async => true);

      for (var index = 1; index <= 25; index++) {
        final sessionId = 'session-$index';
        reconciler.beginSession(uid: 'uid-a', sessionId: sessionId, isAuthorityCurrent: () => true);
        reconciler.addTerminalTurn(sessionId, terminalTurn(sessionId: sessionId, ordinal: index));
        await reconciler.settle();
        reconciler.endSession(sessionId);
      }

      expect(reconciler.sessionCountForTesting, 0);
    });
  });
}
