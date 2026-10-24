import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

void main() {
  test('queued exact successor is reserved before await and admits only after old physical stop', () async {
    final owners = AudioCaptureOwnership();
    final first = owners.reserve(source: 'old-call', hasAuthority: () => true)!;
    final nativeStop = Completer<void>();
    await first.start(start: () async {}, stopAndConfirm: () => nativeStop.future);
    final next = first.reserveSuccessor(source: 'new-call', hasAuthority: () => true)!;
    var admitted = false;
    final ready = next.admitted.then((value) => admitted = value);
    final stopping = first.stop();
    expect(next.isCurrent, isFalse);
    expect(owners.reserve(source: 'dictation', hasAuthority: () => true), isNull);
    await Future<void>.delayed(Duration.zero);
    expect(admitted, isFalse);
    nativeStop.complete();
    await stopping;
    await ready;
    expect(next.isCurrent, isTrue);
    await first.stop();
    expect(next.isCurrent, isTrue);
    await next.stop();
  });
  test('failed outgoing stop retains owner and pending successor through exact stop retry', () async {
    final owners = AudioCaptureOwnership();
    final first = owners.reserve(source: 'old-call', hasAuthority: () => true)!;
    var failed = true;
    await first.start(
        start: () async {},
        stopAndConfirm: () async {
          if (failed) throw StateError('unknown physical state');
        });
    final next = first.reserveSuccessor(source: 'new-call', hasAuthority: () => true)!;
    await expectLater(first.stop(), throwsStateError);
    expect(next.isCurrent, isFalse);
    expect(owners.reserve(source: 'phone', hasAuthority: () => true), isNull);
    failed = false;
    await first.stop();
    expect(await next.admitted, isTrue);
    await next.stop();
  });
  test('account invalidation cancels queued successor before awaiting old physical silence', () async {
    final owners = AudioCaptureOwnership();
    final first = owners.reserve(source: 'old-call', hasAuthority: () => true)!;
    final nativeStop = Completer<void>();
    await first.start(start: () async {}, stopAndConfirm: () => nativeStop.future);
    final next = first.reserveSuccessor(source: 'new-call', hasAuthority: () => true)!;
    final transition = owners.quiesceForAccountTransition();
    expect(await next.admitted, isFalse);
    expect(owners.isReserved, isTrue);
    nativeStop.complete();
    await transition;
    expect(owners.isReserved, isFalse);
  });

  test('logical call ownership survives stopped microphone and playback before a new exact child', () async {
    final owners = AudioCaptureOwnership();
    final call = owners.reserve(source: 'v2v-call', hasAuthority: () => true)!;
    final first = call.createTransport();
    await first.start(start: () async {}, stopAndConfirm: () async {});
    await first.stop();
    expect(first.isCurrent, isFalse);
    expect(call.isCurrent, isTrue);
    expect(owners.reserve(source: 'dictation', hasAuthority: () => true), isNull);
    final replacement = call.createTransport();
    await replacement.start(start: () async {}, stopAndConfirm: () async {});
    await first.stop();
    expect(replacement.isCurrent, isTrue);
    await call.stop();
    expect(replacement.isCurrent, isFalse);
    expect(owners.isReserved, isFalse);
  });
  test('failed child teardown blocks restart and parent release until exact retry confirms stop', () async {
    final owners = AudioCaptureOwnership();
    final call = owners.reserve(source: 'standard-call', hasAuthority: () => true)!;
    final child = call.createTransport();
    var fail = true;
    await child.start(
        start: () async {},
        stopAndConfirm: () async {
          if (fail) throw StateError('physical child still unknown');
        });
    await expectLater(child.stop(), throwsStateError);
    expect(call.createTransport, throwsStateError);
    await expectLater(call.stop(), throwsStateError);
    expect(owners.isReserved, isTrue);
    fail = false;
    await call.stop();
    expect(owners.isReserved, isFalse);
  });
  test('account transition invalidates child callbacks while child native start and stop remain held', () async {
    final owners = AudioCaptureOwnership();
    final call = owners.reserve(source: 'call', hasAuthority: () => true)!;
    final child = call.createTransport();
    final nativeStart = Completer<void>();
    final nativeStop = Completer<void>();
    var stops = 0;
    final starting = child.start(
        start: () => nativeStart.future,
        stopAndConfirm: () {
          stops++;
          return nativeStop.future;
        });
    final transition = owners.quiesceForAccountTransition();
    expect(child.isCurrent, isFalse);
    expect(stops, 0);
    nativeStart.complete();
    await starting;
    await Future<void>.delayed(Duration.zero);
    expect(stops, 1);
    expect(owners.isReserved, isTrue);
    nativeStop.complete();
    await transition;
    expect(owners.isReserved, isFalse);
  });

  test('all capture/call/dictation sources contend while permission is held', () async {
    final owners = AudioCaptureOwnership();
    final permission = Completer<void>();
    final owner = owners.reserve(source: 'dictation', hasAuthority: () => true)!;
    final waiting = permission.future.then((_) => owner.isCurrent);
    for (final source in ['phone', 'device', 'standard-call', 'v2v', 'speech-profile']) {
      expect(owners.reserve(source: source, hasAuthority: () => true), isNull);
    }
    await owner.stop();
    final replacement = owners.reserve(source: 'phone', hasAuthority: () => true)!;
    permission.complete();
    expect(await waiting, isFalse);
    expect(replacement.isCurrent, isTrue);
    await replacement.stop();
  });
  test('held native start and held stop keep exact ownership', () async {
    final owners = AudioCaptureOwnership();
    final nativeStart = Completer<void>();
    final nativeStop = Completer<void>();
    var stopCalls = 0;
    final owner = owners.reserve(source: 'phone', hasAuthority: () => true)!;
    final starting = owner.start(
        start: () => nativeStart.future,
        stopAndConfirm: () {
          stopCalls++;
          return nativeStop.future;
        });
    final stopping = owner.stop();
    expect(owner.isCurrent, isFalse);
    expect(stopCalls, 0);
    expect(owners.reserve(source: 'call', hasAuthority: () => true), isNull);
    nativeStart.complete();
    await starting;
    await Future<void>.delayed(Duration.zero);
    expect(stopCalls, 1);
    expect(owners.isReserved, isTrue);
    nativeStop.complete();
    await stopping;
    expect(owners.isReserved, isFalse);
  });
  test('ambiguous start and failed stop retain ownership until successful retry', () async {
    final owners = AudioCaptureOwnership();
    final owner = owners.reserve(source: 'phone', hasAuthority: () => true)!;
    var stopCalls = 0;
    await expectLater(
        owner.start(
            start: () async => throw StateError('native start failed'),
            stopAndConfirm: () async {
              if (++stopCalls == 1) throw StateError('stop acknowledgement missing');
            }),
        throwsStateError);
    expect(owners.isReserved, isTrue);
    await expectLater(owner.stop(), throwsStateError);
    expect(owners.reserve(source: 'dictation', hasAuthority: () => true), isNull);
    await owner.stop();
    expect(owners.isReserved, isFalse);
  });
  test('account invalidation gates callbacks immediately and holds through stop receipt', () async {
    final owners = AudioCaptureOwnership();
    final nativeStop = Completer<void>();
    final owner = owners.reserve(source: 'account-a-phone', hasAuthority: () => true)!;
    await owner.start(start: () async {}, stopAndConfirm: () => nativeStop.future);
    final transition = owners.quiesceForAccountTransition();
    expect(owner.isCurrent, isFalse);
    expect(owners.reserve(source: 'account-b-phone', hasAuthority: () => true), isNull);
    nativeStop.complete();
    await transition;
    final replacement = owners.reserve(source: 'account-b-phone', hasAuthority: () => true)!;
    await owner.stop();
    expect(replacement.isCurrent, isTrue);
    await replacement.stop();
  });
  test('authority failure and duplicate native start cannot enter transport', () async {
    final owners = AudioCaptureOwnership();
    expect(owners.reserve(source: 'stale', hasAuthority: () => false), isNull);
    expect(owners.reserve(source: 'throwing', hasAuthority: () => throw StateError('stale account')), isNull);
    var current = true;
    final owner = owners.reserve(source: 'phone', hasAuthority: () => current)!;
    var starts = 0;
    await owner.start(start: () async => starts++, stopAndConfirm: () async {});
    await expectLater(owner.start(start: () async => starts++, stopAndConfirm: () async {}), throwsStateError);
    current = false;
    expect(owner.isCurrent, isFalse);
    expect(starts, 1);
    await owner.stop();
  });
  test('session confirmations are exact, retry failed hooks, and retain successor until all acknowledge', () async {
    final owners = AudioCaptureOwnership();
    final lease = owners.reserve(source: 'call', hasAuthority: () => true)!;
    var completedCalls = 0, retryCalls = 0, fail = true;
    final held = Completer<void>();
    lease.addStopAndConfirm(() async {
      completedCalls++;
    });
    lease.addStopAndConfirm(() async {
      retryCalls++;
      if (fail) throw StateError('physical output unresolved');
      await held.future;
    });
    final successor = lease.reserveSuccessor(source: 'replacement', hasAuthority: () => true)!;
    await expectLater(lease.stop(), throwsStateError);
    expect(lease.isOwner, isTrue);
    expect(() => lease.addStopAndConfirm(() async {}), throwsStateError);
    fail = false;
    final retry = lease.stop();
    await Future<void>.delayed(Duration.zero);
    expect(lease.isOwner, isTrue);
    expect(completedCalls, 1);
    expect(retryCalls, 2);
    held.complete();
    await retry;
    expect(await successor.admitted, isTrue);
    await lease.stop();
    expect(successor.isCurrent, isTrue);
    await successor.stop();
  });
}
