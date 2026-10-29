import 'dart:async';

import 'package:clock/clock.dart';
import 'package:fake_async/fake_async.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/dev/base_timed_capture.dart';
import 'package:omi/dev/dev_mic_token.dart';

// v4 fake-clock regressions for the BaseTimedCapture state machine. _FakeNative mirrors PhoneMicDevTokenPolicy /
// PhoneMicDevTokenRegistry / PhoneMicController.devCutoff (Swift) decision for decision. _FakeCapture mirrors the
// CaptureController dev seam (devStreamRecordingOwned refuses when busy, devStopOwned only stops its own session,
// the owned session's restarts are armed with its token) and mints through the REAL DevMicToken. The real
// CaptureController wiring is covered by dev_owned_capture_integration_test.dart; the real PhoneMicController by
// ios/test/dev_timed_capture_controller_test. Invariant on every terminal path: no live session carries the
// test token, and ordinary sessions are never stopped by the test.

enum StartMode { ok, throws, partial, hang, late }

enum StopMode { ok, falseClosed, falseOpen, throws, hang }

enum CutoffMode { works, wedged, hangs }

class _Evidence implements TimedCaptureEvidence {
  final events = <Map<String, Object?>>[];
  List<String> get names => events.map((e) => e['event'] as String).toList();

  @override
  Future<void> record(Map<String, Object?> event) async => events.add(event);
}

class _FakeNative {
  final expiry = <int, DateTime>{};
  final revoked = <int>{};
  final refusals = <String>[];
  int? currentId; // live native session id, null when idle
  bool micStateUnknown = false;
  CutoffMode cutoffMode = CutoffMode.works;
  int cutoffs = 0;

  bool valid(int token) =>
      expiry[token] != null && !revoked.contains(token) && clock.now().isBefore(expiry[token]!);

  bool grant(int token, Duration ttl) {
    final now = clock.now();
    if (expiry.containsKey(token) || revoked.contains(token)) return false;
    if (expiry.entries.any((e) => !revoked.contains(e.key) && now.isBefore(e.value))) return false;
    expiry[token] = now.add(ttl);
    // Native expiry, independent of Dart (BaseTimedCaptureDispatcher.expire).
    Timer(ttl, () {
      revoked.add(token);
      cutoff(token);
    });
    return true;
  }

  /// PhoneMicDevTokenPolicy.startDecision + attemptBringUp gate. Returns null when the mic opens.
  String? activate(int incoming) {
    final t = DevMicToken.tokenOf(incoming);
    if (t != null) {
      if (!valid(t)) return _refuse('invalid');
      if (currentId != null && DevMicToken.tokenOf(currentId!) != t) return _refuse('would_adopt_other_session');
    }
    currentId = incoming;
    return null;
  }

  String _refuse(String reason) {
    refusals.add(reason);
    return reason;
  }

  bool? open(int token) =>
      micStateUnknown ? null : currentId != null && DevMicToken.tokenOf(currentId!) == token;

  /// PhoneMicController.devCutoff + dispatcher verify.
  String cutoff(int token) {
    cutoffs++;
    revoked.add(token);
    if (cutoffMode == CutoffMode.wedged) return 'unverified';
    if (currentId == null) return 'idle';
    if (DevMicToken.tokenOf(currentId!) != token) return 'not_owned';
    currentId = null;
    return 'closed';
  }
}

class _FakeCapture {
  _FakeCapture(this.native);
  final _FakeNative native;
  int ordinarySeq = 0;
  bool live = false; // what the Dart capture controller believes
  int? ownedToken; // the test token that owns the current capture session, if any
  StartMode startMode = StartMode.ok;
  final stopModes = <StopMode>[];
  Completer<void>? lateStart;
  int starts = 0;
  int stops = 0;
  final mintedIds = <int>[];

  /// CaptureController._devOwnedStart: arm only around an owned session's own start.
  int mint([int? token]) {
    if (token != null) DevMicToken.armNext(token);
    final id = DevMicToken.mint(() => ++ordinarySeq);
    DevMicToken.disarm();
    mintedIds.add(id);
    return id;
  }

  void _activateOrThrow(int id) {
    final refusal = native.activate(id);
    if (refusal != null) throw PlatformException(code: 'dev_token_$refusal');
  }

  /// devStreamRecordingOwned: refused (never preempting) when a capture is live; otherwise owns the new session.
  Future<void> start(int token) async {
    starts++;
    if (live) throw StateError('dev owned phone start refused');
    ownedToken = token;
    switch (startMode) {
      case StartMode.ok:
        _activateOrThrow(mint(token));
        live = true;
      case StartMode.throws:
        mint(token);
        throw PlatformException(code: 'engine_start_failed', message: 'no input');
      case StartMode.partial:
        _activateOrThrow(mint(token));
        live = true;
        throw PlatformException(code: 'socket_failed', message: 'mic opened, socket failed');
      case StartMode.hang:
        mint(token);
        await Completer<void>().future;
      case StartMode.late:
        final id = mint(token); // minted at call time, like NativeMicRecorderService.start
        lateStart = Completer<void>();
        await lateStart!.future;
        _activateOrThrow(id); // native activation happens late
        live = true;
    }
  }

  /// devStopOwned: only the session [token] started; anything else is a no-op false.
  Future<bool> stop(int token) async {
    stops++;
    if (ownedToken != token) return false;
    final mode = stopModes.isEmpty ? StopMode.ok : stopModes.removeAt(0);
    switch (mode) {
      case StopMode.ok:
        native.currentId = null;
        live = false;
        return true;
      case StopMode.falseClosed:
        native.currentId = null;
        live = false;
        return false;
      case StopMode.falseOpen:
        return false;
      case StopMode.throws:
        throw StateError('stop failed');
      case StopMode.hang:
        return Completer<bool>().future;
    }
  }

  /// Dart stall watchdog: stop()+start() inside the same capture session; an owned session restarts armed with
  /// its own token.
  String? stallRestart() {
    native.currentId = null;
    return native.activate(mint(live ? ownedToken : null));
  }

  bool ownsCurrent(int token) => live && ownedToken == token;

  /// An unrelated user recording with an ordinary id (no lease involvement).
  String? ordinaryStart() => native.activate(++ordinarySeq);
}

class _Rig {
  _Rig() {
    capture = _FakeCapture(native);
  }

  final native = _FakeNative();
  late final _FakeCapture capture;
  String? uid = 'uid-heron';
  bool granted = true;
  String speech = 'ready';
  bool extraBusy = false;
  Completer<bool>? grantedGate;
  int nextToken = 0x2A5A5;
  final terminals = <String>[];


  TimedCapturePorts get ports => TimedCapturePorts(
        start: capture.start,
        stop: capture.stop,
        ownsCurrent: capture.ownsCurrent,
        busy: () => capture.live || extraBusy,
        micGranted: () => grantedGate?.future ?? Future.value(granted),
        speechReadiness: () async => speech,
        uid: () => uid,
        newToken: () => nextToken++,
        grantToken: (token, ttl) async => native.grant(token, ttl),
        revokeToken: (token) async => native.revoked.add(token),
        micOpen: (token) async => native.open(token),
        nativeCutoff: (token) {
          if (native.cutoffMode == CutoffMode.hangs) return Completer<String>().future;
          return Future.value(native.cutoff(token));
        },
        reportTerminal: (status) async => terminals.add(status),
      );

  /// No live native session carries [token].
  bool tokenGone(int token) => native.open(token) == false;
}

final _t0 = DateTime.utc(2026, 10, 27, 16);

void _run(void Function(FakeAsync async, BaseTimedCapture c, _Rig r, _Evidence ev) body,
    {TimedCaptureConfig config = const TimedCaptureConfig(), bool enabled = true, bool dispose = true}) {
  fakeAsync((async) {
    withClock(async.getClock(_t0), () {
      final r = _Rig();
      final ev = _Evidence();
      final c = BaseTimedCapture(ports: r.ports, evidence: ev, config: config, enabled: enabled);
      try {
        body(async, c, r, ev);
      } finally {
        if (dispose) c.dispose();
        expect(DevMicToken.armed, isNull, reason: 'an armed token never outlives its start');
        DevMicToken.disarm();
      }
    });
  }, initialTime: _t0);
}

T _await<T>(FakeAsync async, Future<T> future) {
  T? value;
  Object? error;
  var done = false;
  future.then((v) {
    value = v;
    done = true;
  }, onError: (Object e) {
    error = e;
    done = true;
  });
  async.flushMicrotasks();
  if (error != null) throw error!;
  expect(done, isTrue, reason: 'future did not complete without elapsing time');
  return value as T;
}

/// Arms at t0 and triggers at t0+45s; returns the trigger outcome.
String _armAndTrigger(FakeAsync async, BaseTimedCapture c) {
  expect(_await(async, c.arm()), isNull);
  async.elapse(const Duration(seconds: 45));
  return _await(async, c.runFromSystemTrigger());
}

const _token0 = 0x2A5A5;

void _expectTokenGone(_Rig r, BaseTimedCapture c, {int token = _token0}) {
  expect(r.tokenGone(token), isTrue, reason: 'a live session still carries the test token (phase ${c.phase})');
  expect(
    c.phase,
    isIn([TimedCapturePhase.done, TimedCapturePhase.failed, TimedCapturePhase.cancelled, TimedCapturePhase.idle]),
  );
}

void main() {
  test('default build is disabled: no arm, no trigger, no capture', () {
    _run(enabled: false, (async, c, r, ev) {
      expect(_await(async, c.arm()), 'disabled');
      expect(_await(async, c.runFromSystemTrigger()), 'disabled');
      expect(r.capture.starts, 0);
      expect(ev.events, isEmpty);
    });
    expect(kBaseDevTimedCapture, isFalse, reason: 'tests run without the dev define');
  });

  group('arm refuses without opening the mic', () {
    for (final (name, setup, reason) in <(String, void Function(_Rig), String)>[
      ('signed out', (r) => r.uid = null, 'signed_out'),
      ('recorder busy', (r) => r.extraBusy = true, 'capture_busy'),
      ('mic not granted', (r) => r.granted = false, 'mic_not_granted'),
      ('speech needs permission', (r) => r.speech = 'permission_required', 'speech_permission_required'),
    ]) {
      test(name, () {
        _run((async, c, r, ev) {
          setup(r);
          expect(_await(async, c.arm()), reason);
          expect(c.phase, TimedCapturePhase.idle);
          expect(r.capture.starts, 0);
          expect(r.native.expiry, isEmpty, reason: 'no token is granted by arming');
        });
      });
    }
  });

  test('armed state never records or grants, through the whole window', () {
    _run((async, c, r, ev) {
      expect(_await(async, c.arm()), isNull);
      async.elapse(const Duration(minutes: 9, seconds: 59));
      expect(r.capture.starts, 0);
      expect(r.native.expiry, isEmpty);
      expect(r.native.currentId, isNull);
      expect(c.phase, TimedCapturePhase.armed);
    });
  });

  test('happy path: token-tagged owned session, timed owned stop, token revoked', () {
    _run((async, c, r, ev) {
      expect(_armAndTrigger(async, c), 'recording:20');
      expect(r.capture.ownsCurrent(_token0), isTrue);
      expect(DevMicToken.tokenOf(r.native.currentId!), _token0);
      expect(r.native.valid(_token0), isTrue);
      async.elapse(const Duration(seconds: 20));
      expect(c.phase, TimedCapturePhase.done);
      expect(r.native.revoked, contains(_token0));
      expect(r.native.cutoffs, 0);
      expect(r.capture.ownsCurrent(_token0), isFalse);
      expect(r.terminals, ['done']);
      _expectTokenGone(r, c);
    });
  });

  group('exact ownership (native fakes mirror PhoneMicDevTokenPolicy)', () {
    test('unrelated ordinary session inside the old 85 s window stays untouched by native expiry', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20));
        expect(c.phase, TimedCapturePhase.done);
        expect(r.capture.ordinaryStart(), isNull, reason: 'ordinary start is stock behavior');
        final ordinary = r.native.currentId;
        expect(DevMicToken.isTest(ordinary!), isFalse);
        async.elapse(const Duration(seconds: 30)); // past the token's native expiry (45 s)
        expect(r.native.cutoffs, 1, reason: 'native expiry ran its cutoff');
        expect(r.native.currentId, ordinary, reason: 'not_owned: the ordinary session keeps running');
      });
    });

    test('late start completing after the native deadline is refused before activation', () {
      _run((async, c, r, ev) {
        r.capture.startMode = StartMode.late;
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        c.runFromSystemTrigger();
        async.elapse(const Duration(seconds: 15)); // start_timeout: token revoked first
        expect(c.phase, TimedCapturePhase.failed);
        async.elapse(const Duration(seconds: 120)); // also past the grant's own expiry
        r.capture.lateStart!.complete();
        async.flushMicrotasks();
        expect(r.native.refusals, contains('invalid'));
        expect(r.native.currentId, isNull, reason: 'the late start never opened the mic');
        _expectTokenGone(r, c);
      });
    });

    test('account change revokes the token: a start in flight is refused natively', () {
      _run((async, c, r, ev) {
        r.capture.startMode = StartMode.late;
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        final run = c.runFromSystemTrigger();
        async.flushMicrotasks();
        expect(c.phase, TimedCapturePhase.starting);
        r.uid = 'someone-else';
        _await(async, c.onAuthChanged('someone-else'));
        expect(r.native.revoked, contains(_token0));
        r.capture.lateStart!.complete();
        async.flushMicrotasks();
        expect(r.native.refusals, contains('invalid'));
        expect(r.native.currentId, isNull);
        expect(_await(async, run), 'start_failed');
        _expectTokenGone(r, c);
      });
    });

    test('cancelled/expired token: activation with it is refused (direct native gate)', () {
      _run((async, c, r, ev) {
        expect(r.native.grant(7, const Duration(seconds: 10)), isTrue);
        r.native.revoked.add(7);
        expect(r.native.activate(r.capture.mint(7)), 'invalid');
        expect(r.native.grant(8, const Duration(seconds: 10)), isTrue);
        async.elapse(const Duration(seconds: 11));
        expect(r.native.activate(r.capture.mint(8)), 'invalid');
        expect(r.native.currentId, isNull);
      });
    });

    test('owned stall restart keeps the same token and is stopped as owned', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        final first = r.native.currentId!;
        expect(r.capture.stallRestart(), isNull);
        final second = r.native.currentId!;
        expect(second, isNot(first));
        expect(DevMicToken.tokenOf(second), _token0);
        r.capture.stopModes.add(StopMode.falseOpen); // Dart stop does not close it
        async.elapse(const Duration(seconds: 20));
        expect(r.native.cutoffs, 1);
        expect(c.phase, TimedCapturePhase.done);
        _expectTokenGone(r, c);
        // The Dart capture still holds the owned session (its stop returned false): a further stall restart is
        // armed with the same, now revoked, token and is refused natively, never reopened as ordinary.
        expect(r.capture.ownsCurrent(_token0), isTrue);
        expect(r.capture.stallRestart(), 'invalid');
      });
    });

    test('token cutoff can never stop a new ordinary session', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20));
        r.capture.ordinaryStart();
        final ordinary = r.native.currentId;
        expect(r.native.cutoff(_token0), 'not_owned');
        expect(r.native.currentId, ordinary);
      });
    });

    test('a test-token start never adopts a running ordinary engine', () {
      _run((async, c, r, ev) {
        expect(r.native.grant(9, const Duration(seconds: 30)), isTrue);
        r.capture.ordinaryStart();
        final ordinary = r.native.currentId;
        expect(r.native.activate(r.capture.mint(9)), 'would_adopt_other_session');
        expect(r.native.currentId, ordinary);
      });
    });

    test('ordinary ids never carry the test bit, before, during and after the owned session', () {
      _run((async, c, r, ev) {
        final before = r.capture.mint();
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20));
        final after = r.capture.mint();
        final during = r.capture.mintedIds[1];
        expect(DevMicToken.isTest(before), isFalse);
        expect(before, 1);
        expect(DevMicToken.tokenOf(during), _token0);
        expect(DevMicToken.isTest(after), isFalse);
        expect(after, 2, reason: 'the ordinary counter never absorbs a test id');
      });
    });
  });

  group('single flight', () {
    test('two concurrent triggers: one claim, one grant, one start', () {
      _run((async, c, r, ev) {
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        r.grantedGate = Completer<bool>();
        final first = c.runFromSystemTrigger();
        final second = c.runFromSystemTrigger();
        async.flushMicrotasks();
        r.grantedGate!.complete(true);
        expect(_await(async, second), 'not_armed');
        expect(_await(async, first), 'recording:20');
        expect(r.capture.starts, 1);
        expect(r.native.expiry.length, 1);
        async.elapse(const Duration(seconds: 20));
        _expectTokenGone(r, c);
      });
    });

    test('account switch during the trigger await: no grant, no start', () {
      _run((async, c, r, ev) {
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        r.grantedGate = Completer<bool>();
        final run = c.runFromSystemTrigger();
        async.flushMicrotasks();
        r.uid = 'someone-else';
        r.grantedGate!.complete(true);
        expect(_await(async, run), 'account_changed');
        expect(r.native.expiry, isEmpty);
        expect(r.capture.starts, 0);
        expect(c.phase, TimedCapturePhase.cancelled);
      });
    });

    test('native grant refused (another live grant): cancelled, no start, token revoked', () {
      _run((async, c, r, ev) {
        expect(r.native.grant(99, const Duration(minutes: 5)), isTrue);
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        expect(_await(async, c.runFromSystemTrigger()), 'grant_refused');
        expect(r.capture.starts, 0);
        expect(r.native.revoked, contains(_token0));
        expect(r.capture.ownedToken, isNull, reason: 'no owned start was attempted');
      });
    });

    test('cancel during arm awaits supersedes it; a concurrent arm is refused', () {
      _run((async, c, r, ev) {
        r.grantedGate = Completer<bool>();
        final arming = c.arm();
        async.flushMicrotasks();
        expect(c.phase, TimedCapturePhase.arming);
        expect(_await(async, c.arm()), 'already_arming');
        _await(async, c.cancel('operator_cancelled'));
        r.grantedGate!.complete(true);
        expect(_await(async, arming), 'superseded');
        expect(c.phase, TimedCapturePhase.cancelled);
        async.elapse(const Duration(seconds: 60));
        expect(_await(async, c.runFromSystemTrigger()), 'not_armed');
        expect(r.capture.starts, 0);
      });
    });

    test('account switch during arm awaits (no auth event) refuses the arm', () {
      _run((async, c, r, ev) {
        r.grantedGate = Completer<bool>();
        final arming = c.arm();
        async.flushMicrotasks();
        r.uid = 'someone-else';
        r.grantedGate!.complete(true);
        expect(_await(async, arming), 'account_changed');
        expect(c.phase, TimedCapturePhase.idle);
      });
    });

    test('account change while recording: token revoked first, stopped, verified', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        r.uid = 'someone-else';
        _await(async, c.onAuthChanged('someone-else'));
        expect(c.phase, TimedCapturePhase.failed);
        expect(c.lastReason, 'account_changed');
        async.elapse(const Duration(minutes: 2));
        expect(r.capture.stops, 1);
        _expectTokenGone(r, c);
      });
    });
  });

  group('start containment', () {
    test('start throws before opening: failed, token gone', () {
      _run((async, c, r, ev) {
        r.capture.startMode = StartMode.throws;
        expect(_armAndTrigger(async, c), 'start_failed');
        expect(r.native.revoked, contains(_token0));
        _expectTokenGone(r, c);
      });
    });

    test('partial start (mic opened, then error): stopped and verified', () {
      _run((async, c, r, ev) {
        r.capture.startMode = StartMode.partial;
        expect(_armAndTrigger(async, c), 'start_failed');
        expect(r.capture.stops, 1);
        _expectTokenGone(r, c);
      });
    });

    test('hung start: bounded, re-arm blocked through the late-start grace', () {
      _run((async, c, r, ev) {
        r.capture.startMode = StartMode.hang;
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 45));
        String? outcome;
        c.runFromSystemTrigger().then((v) => outcome = v);
        async.elapse(const Duration(seconds: 15));
        expect(outcome, 'start_timeout');
        expect(c.lateStartPending, isTrue);
        expect(_await(async, c.arm()), 'late_start_pending');
        async.elapse(const Duration(seconds: 60));
        expect(c.lateStartPending, isFalse);
        _expectTokenGone(r, c);
      });
    });
  });

  group('stop containment', () {
    test('stop returns false but closed the session: done with a note, no cutoff', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.add(StopMode.falseClosed);
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20));
        expect(c.phase, TimedCapturePhase.done);
        expect(c.lastReason, 'stop_returned_false_mic_verified_closed');
        expect(r.native.cutoffs, 0);
        _expectTokenGone(r, c);
      });
    });

    test('stop returns false and the session stays: token cutoff closes it; re-arm waits for an idle capture', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.add(StopMode.falseOpen);
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20));
        expect(r.native.cutoffs, 1);
        expect(c.phase, TimedCapturePhase.done);
        _expectTokenGone(r, c);
        expect(r.capture.ownsCurrent(_token0), isTrue, reason: 'Dart capture still believes it is live');
        expect(_await(async, c.arm()), 'capture_busy');
        r.capture.live = false;
        expect(_await(async, c.arm()), isNull);
      });
    });

    test('stop throws once: retried, verified', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.add(StopMode.throws);
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 22));
        expect(r.capture.stops, 2);
        expect(c.phase, TimedCapturePhase.done);
        _expectTokenGone(r, c);
      });
    });

    test('stop hangs twice: bounded, token cutoff closes, no loop', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.addAll([StopMode.hang, StopMode.hang]);
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 20 + 10 + 2 + 10));
        expect(r.capture.stops, 2);
        expect(c.phase, TimedCapturePhase.done);
        _expectTokenGone(r, c);
        async.elapse(const Duration(minutes: 5));
        expect(r.capture.stops, 2);
      });
    });

    test('native state unknown: never "done"; HOLD blocks re-arm', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        r.native.micStateUnknown = true;
        async.elapse(const Duration(seconds: 20));
        expect(c.phase, TimedCapturePhase.hold);
        expect(r.terminals, ['hold']);
        expect(_await(async, c.arm()), 'hold_unresolved');
      });
    });

    test('wedged native (modeled limit): HOLD; resolve only once the token session is gone and capture idle', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.addAll([StopMode.hang, StopMode.hang]);
        r.native.cutoffMode = CutoffMode.wedged;
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 60));
        expect(c.phase, TimedCapturePhase.hold);
        expect(ev.names, contains('UNVERIFIED_HOLD'));
        expect(r.native.valid(_token0), isFalse, reason: 'revoked: no restart can reopen it');
        expect(_await(async, c.resolveHold()), 'mic_not_verified_idle');
        r.native.currentId = null;
        expect(_await(async, c.resolveHold()), 'capture_not_idle');
        r.capture.live = false;
        expect(_await(async, c.resolveHold()), 'resolved');
        _expectTokenGone(r, c);
      });
    });

    test('Dart->native cutoff call hangs: bounded; native grant expiry still closes the token session', () {
      _run((async, c, r, ev) {
        r.capture.stopModes.addAll([StopMode.hang, StopMode.hang]);
        r.native.cutoffMode = CutoffMode.hangs; // only the channel call hangs; native expiry runs on its own
        _armAndTrigger(async, c);
        async.elapse(const Duration(seconds: 90));
        expect(r.native.cutoffs, 1, reason: 'native expiry at grant+45 s');
        expect(c.phase, TimedCapturePhase.done);
        _expectTokenGone(r, c);
      });
    });
  });

  group('dispose', () {
    test('dispose while recording: token revoked, stop + cutoff, HOLD, token session gone', () {
      _run(dispose: false, (async, c, r, ev) {
        _armAndTrigger(async, c);
        c.dispose();
        async.flushMicrotasks();
        expect(c.phase, TimedCapturePhase.hold);
        expect(r.native.revoked, contains(_token0));
        expect(r.capture.stops, 1);
        expect(r.native.cutoffs, 1);
        expect(r.tokenGone(_token0), isTrue);
        async.elapse(const Duration(minutes: 2));
        expect(r.capture.stops, 1, reason: 'no timer fires after dispose');
      });
    });

    test('dispose while armed opens and grants nothing', () {
      _run(dispose: false, (async, c, r, ev) {
        _await(async, c.arm());
        c.dispose();
        async.elapse(const Duration(minutes: 20));
        expect(r.capture.starts, 0);
        expect(r.native.expiry, isEmpty);
      });
    });
  });

  group('arm window', () {
    test('early trigger is refused and keeps the one shot', () {
      _run((async, c, r, ev) {
        _await(async, c.arm());
        async.elapse(const Duration(seconds: 10));
        expect(_await(async, c.runFromSystemTrigger()), 'too_early');
        expect(c.phase, TimedCapturePhase.armed);
        async.elapse(const Duration(seconds: 35));
        expect(_await(async, c.runFromSystemTrigger()), 'recording:20');
        async.elapse(const Duration(seconds: 20));
        _expectTokenGone(r, c);
      });
    });

    test('duplicate trigger never starts a second recording', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        expect(_await(async, c.runFromSystemTrigger()), 'not_armed');
        async.elapse(const Duration(seconds: 30));
        expect(_await(async, c.runFromSystemTrigger()), 'not_armed');
        expect(r.capture.starts, 1);
      });
    });

    test('arm expires at the window end; nothing is granted', () {
      _run((async, c, r, ev) {
        _await(async, c.arm());
        async.elapse(const Duration(minutes: 10));
        expect(c.phase, TimedCapturePhase.cancelled);
        expect(_await(async, c.runFromSystemTrigger()), 'not_armed');
        expect(r.native.expiry, isEmpty);
      });
    });

    test('operator cancel never interrupts a running recording', () {
      _run((async, c, r, ev) {
        _armAndTrigger(async, c);
        _await(async, c.cancel('operator_cancelled'));
        expect(c.phase, TimedCapturePhase.recording);
        async.elapse(const Duration(seconds: 20));
        _expectTokenGone(r, c);
      });
    });

    test('durations are clamped; grant TTL covers the whole bounded recording', () {
      expect(const TimedCaptureConfig(recordFor: Duration(seconds: 2)).boundedRecordFor, const Duration(seconds: 5));
      expect(const TimedCaptureConfig(recordFor: Duration(minutes: 2)).boundedRecordFor, const Duration(seconds: 60));
      expect(const TimedCaptureConfig(window: Duration(hours: 2)).boundedWindow, const Duration(minutes: 30));
      expect(const TimedCaptureConfig(recordFor: Duration(minutes: 2)).grantTtl, const Duration(seconds: 85));
    });
  });
}
