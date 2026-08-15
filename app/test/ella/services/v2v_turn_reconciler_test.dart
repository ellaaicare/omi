import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_turn_reconciler.dart';

void main() {
  group('V2VTurnReconciler', () {
    for (final order in ['user-assistant-done', 'assistant-done-user', 'assistant-user-done']) {
      test('persists one complete turn for $order ordering', () async {
        final writes = <V2VTranscriptTurn>[];
        final reconciler = V2VTurnReconciler(
          writer: (turn) async {
            writes.add(turn);
            return true;
          },
        );
        expect(reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true), isTrue);
        reconciler.beginUserTurn('session-1');

        switch (order) {
          case 'user-assistant-done':
            reconciler.addUserTerminal('session-1', 'Question');
            reconciler.addAssistantFragment('session-1', 'Answer');
            reconciler.markAssistantTerminal('session-1');
            break;
          case 'assistant-done-user':
            reconciler.addAssistantFragment('session-1', 'Ans');
            reconciler.markAssistantTerminal('session-1', finalText: 'Answer');
            reconciler.addUserTerminal('session-1', 'Question');
            break;
          case 'assistant-user-done':
            reconciler.addAssistantFragment('session-1', 'Answer');
            reconciler.addUserTerminal('session-1', 'Question');
            reconciler.markAssistantTerminal('session-1');
            break;
        }
        await reconciler.settle();

        expect(writes, hasLength(1));
        expect(writes.single.sessionId, 'session-1');
        expect(writes.single.turnId, 'turn-000001');
        expect(writes.single.userTranscript, 'Question');
        expect(writes.single.assistantTranscript, 'Answer');
      });
    }

    test('duplicates do not create another semantic write', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.beginUserTurn('session-1');
      reconciler.addAssistantFragment('session-1', 'Answer');
      reconciler.markAssistantTerminal('session-1', eventId: 'assistant-terminal-1');
      reconciler.markAssistantTerminal('session-1', eventId: 'assistant-terminal-1');
      // The current first-party proxy's custom user_transcript has no event id.
      reconciler.addUserTerminal('session-1', 'Question');
      reconciler.addUserTerminal('session-1', 'Question');
      await reconciler.settle();

      expect(writes, hasLength(1));
      expect(writes.single.assistantTranscript, 'Answer');
    });

    test('reconnect isolates late events and gives each session a stable namespace', () async {
      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.beginUserTurn('session-1');
      reconciler.addAssistantFragment('session-1', 'First answer');
      reconciler.markAssistantTerminal('session-1');
      reconciler.addUserTerminal('session-1', 'First question');
      await reconciler.settle();
      reconciler.endSession('session-1');

      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-2', isAuthorityCurrent: () => true);
      reconciler.addUserTerminal('session-1', 'Late cross-session transcript');
      reconciler.beginUserTurn('session-2');
      reconciler.addUserTerminal('session-2', 'Second question');
      reconciler.addAssistantFragment('session-2', 'Second answer');
      reconciler.markAssistantTerminal('session-2');
      await reconciler.settle();

      expect(writes, hasLength(2));
      expect(writes.map((turn) => '${turn.sessionId}:${turn.turnId}'), {
        'session-1:turn-000001',
        'session-2:turn-000001',
      });
    });

    test('partial and nonterminal input has zero writes', () async {
      var writes = 0;
      final reconciler = V2VTurnReconciler(
        writer: (_) async {
          writes++;
          return true;
        },
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.beginUserTurn('session-1');
      reconciler.addAssistantFragment('session-1', 'Partial assistant');
      reconciler.addUserTerminal('session-1', 'Terminal user');
      await reconciler.settle();
      expect(writes, 0);

      reconciler.discardNonterminalAssistant('session-1');
      reconciler.markAssistantTerminal('session-1');
      await reconciler.settle();
      expect(writes, 0);
    });

    test('authority loss rejects the turn with zero writer side effects', () async {
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
      reconciler.beginUserTurn('session-1');
      reconciler.addUserTerminal('session-1', 'Wrong authority');
      reconciler.addAssistantFragment('session-1', 'Must not persist');
      reconciler.markAssistantTerminal('session-1');
      await reconciler.settle();

      expect(writes, 0);
    });

    test('failed backend attempt is not marked successful and retries idempotently on signal', () async {
      var attempts = 0;
      var failures = 0;
      final committedKeys = <String>{};
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          attempts++;
          if (attempts == 1) return false;
          committedKeys.add('${turn.sessionId}:${turn.turnId}');
          return true;
        },
        onWriteFailure: () => failures++,
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.beginUserTurn('session-1');
      reconciler.addUserTerminal('session-1', 'Question');
      reconciler.addAssistantFragment('session-1', 'Answer');
      reconciler.markAssistantTerminal('session-1');
      await reconciler.settle();

      expect(attempts, 1);
      expect(failures, 1);
      expect(committedKeys, isEmpty);

      reconciler.retryAuthorizedPending();
      await reconciler.settle();
      reconciler.retryAuthorizedPending();
      await reconciler.settle();
      expect(attempts, 2);
      expect(committedKeys, {'session-1:turn-000001'});
    });

    test('retry signal during an in-flight failure is not lost', () async {
      final firstAttemptStarted = Completer<void>();
      final releaseFirstAttempt = Completer<void>();
      final attemptedKeys = <String>[];
      var attempts = 0;
      final reconciler = V2VTurnReconciler(
        writer: (turn) async {
          attempts++;
          attemptedKeys.add('${turn.sessionId}:${turn.turnId}');
          if (attempts == 1) {
            firstAttemptStarted.complete();
            await releaseFirstAttempt.future;
            return false;
          }
          return true;
        },
      );
      reconciler.beginSession(uid: 'uid-a', sessionId: 'session-1', isAuthorityCurrent: () => true);
      reconciler.beginUserTurn('session-1');
      reconciler.addUserTerminal('session-1', 'Question');
      reconciler.addAssistantFragment('session-1', 'Answer');
      reconciler.markAssistantTerminal('session-1');

      await firstAttemptStarted.future;
      reconciler.retryAuthorizedPending();
      releaseFirstAttempt.complete();
      await reconciler.settle();

      expect(attempts, 2);
      expect(attemptedKeys.toSet(), {'session-1:turn-000001'});
    });
  });
}
