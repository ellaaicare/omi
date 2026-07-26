import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/voice_session_startup_guard.dart';

void main() {
  group('VoiceSessionStartupGuard', () {
    test('End during delayed capture shutdown prevents connect and UI activation', () async {
      final guard = VoiceSessionStartupGuard();
      final captureStopped = Completer<void>();
      final generation = guard.begin();
      var connectCalls = 0;
      var activationCalls = 0;

      final startup = () async {
        await captureStopped.future;
        if (!guard.isCurrent(generation)) return;
        connectCalls += 1;
        if (!guard.isCurrent(generation)) return;
        activationCalls += 1;
      }();

      expect(guard.isStarting, isTrue);
      guard.cancel();
      captureStopped.complete();
      await startup;

      expect(guard.isStarting, isFalse);
      expect(connectCalls, 0);
      expect(activationCalls, 0);
    });

    test('dispose during delayed connect disconnects the stale client and never activates UI', () async {
      final guard = VoiceSessionStartupGuard();
      final connectedClient = Completer<String>();
      final generation = guard.begin();
      var disconnectCalls = 0;
      var activationCalls = 0;

      final startup = () async {
        await connectedClient.future;
        if (!guard.isCurrent(generation)) {
          disconnectCalls += 1;
          return;
        }
        activationCalls += 1;
      }();

      guard.dispose();
      connectedClient.complete('client');
      await startup;

      expect(disconnectCalls, 1);
      expect(activationCalls, 0);
    });

    test('only the current generation can complete startup', () {
      final guard = VoiceSessionStartupGuard();
      final first = guard.begin();
      final second = guard.begin();

      guard.complete(first);
      expect(guard.isStarting, isTrue);

      guard.complete(second);
      expect(guard.isStarting, isFalse);
    });
  });
}
