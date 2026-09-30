import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/guardian_mode_service.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid, this.current);
  @override
  final String uid;
  final bool Function() current;
  @override
  bool isExactCurrent() => current();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late GuardianWhisperStateFence fence;
  late _Authority authority;
  var epoch = 0;

  setUp(() {
    epoch = 0;
    fence = GuardianWhisperStateFence();
    authority = _Authority('owner-a', () => epoch == 0);
  });
  tearDown(() => fence.dispose());

  test('missing or revoked authority cannot admit reads, choices or native work', () async {
    expect(fence.observe(() => null), isNull);
    expect(fence.choose(() => null, true), isNull);
    final read = fence.observe(() => authority)!;
    epoch++;
    expect(fence.observe(() => authority), isNull);
    expect(fence.choose(() => authority, true), isNull);
    var called = false;
    await fence.serialize(read, () async => called = true);
    expect(called, isFalse);
  });

  test('read beginning during a pending explicit choice cannot take priority', () async {
    final choice = fence.choose(() => authority, true)!;
    final write = Completer<void>();
    final action = fence.serialize(choice, () => write.future);
    expect(fence.observe(() => authority), isNull);
    write.complete();
    await action;
    expect(fence.observe(() => authority), isNull);
    fence.publish(choice, (enabled: true, modeVerified: true, nativeReconciled: true));
    expect(fence.observe(() => authority), isNotNull);
  });

  test('older read and native retry cannot overwrite a newer explicit choice', () async {
    final oldRead = fence.observe(() => authority)!;
    final off = fence.choose(() => authority, false)!;
    var calls = 0;
    await fence.serialize(oldRead, () async => calls++);
    fence.publish(oldRead, (enabled: true, modeVerified: true, nativeReconciled: true));
    expect(calls, 0);
    expect(fence.snapshot!.enabled, isFalse);
    expect(fence.choicePending, isTrue);
    fence.publish(off, (enabled: false, modeVerified: true, nativeReconciled: true));
    expect(fence.choicePending, isFalse);
  });

  test('newer OFF runs after a native start already in flight and wins eventual state', () async {
    final start = Completer<void>();
    final order = <String>[];
    var nativeOn = false;
    final old = fence.choose(() => authority, true)!;
    final starting = fence.serialize(old, () async {
      order.add('start-began');
      await start.future;
      nativeOn = true;
      order.add('start-finished');
      fence.publish(old, (enabled: true, modeVerified: true, nativeReconciled: true));
    });
    final off = fence.choose(() => authority, false)!;
    final stopping = fence.serialize(off, () async {
      nativeOn = false;
      order.add('stop');
      fence.publish(off, (enabled: false, modeVerified: true, nativeReconciled: true));
    });
    expect(order, ['start-began']);
    start.complete();
    await Future.wait([starting, stopping]);
    expect(order, ['start-began', 'start-finished', 'stop']);
    expect(nativeOn, isFalse);
    expect(fence.snapshot!.enabled, isFalse);
  });

  test('failed native work does not poison a later explicit OFF', () async {
    final on = fence.choose(() => authority, true)!;
    await expectLater(fence.serialize<void>(on, () async => throw StateError('native unavailable')), throwsStateError);
    fence.publish(on, (enabled: true, modeVerified: true, nativeReconciled: false));
    expect(fence.snapshot!.nativeReconciled, isFalse);
    final off = fence.choose(() => authority, false)!;
    var stopped = false;
    await fence.serialize(off, () async => stopped = true);
    expect(stopped, isTrue);
  });

  test('account profile ABA invalidates an awaited completion despite the same UID', () async {
    final old = fence.choose(() => authority, true)!;
    final pending = Completer<void>();
    final action = fence.serialize(old, () async {
      await pending.future;
      fence.publish(old, (enabled: true, modeVerified: true, nativeReconciled: true));
    });
    epoch++;
    fence.invalidate();
    final replacement = _Authority('owner-a', () => epoch == 1);
    final current = fence.choose(() => replacement, false)!;
    pending.complete();
    await action;
    expect(old.isCurrent, isFalse);
    expect(current.isCurrent, isTrue);
    expect(fence.snapshot!.enabled, isFalse);
    expect(fence.choicePending, isTrue);
  });

  test('account transition disables native after older in-flight start completes', () async {
    final pending = Completer<void>();
    var nativeOn = false;
    final old = fence.choose(() => authority, true)!;
    final start = fence.serialize(old, () async {
      await pending.future;
      nativeOn = true;
    });
    final stop = fence.stopAfterInFlight(() async => nativeOn = false, authorityProvider: () => authority);
    expect(old.isCurrent, isFalse);
    pending.complete();
    await Future.wait([start, stop]);
    expect(nativeOn, isFalse);
    expect(fence.snapshot, isNull);
  });

  test('shutdown rejects fresh outgoing tickets even after repeated old-account notifications', () async {
    final oldRead = fence.observe(() => authority)!;
    final stop = Completer<void>();
    final stopping = fence.stopAfterInFlight(() => stop.future, authorityProvider: () => authority);
    fence.invalidate();
    fence.invalidate();
    expect(oldRead.isCurrent, isFalse);
    expect(fence.observe(() => authority), isNull);
    expect(fence.choose(() => authority, true), isNull);
    stop.complete();
    await stopping;
    expect(fence.observe(() => authority), isNull);
    expect(fence.choose(() => authority, false), isNull);
  });

  test('confirmed same-UID replacement profile can resume after shutdown without reopening old tickets', () async {
    final oldRead = fence.observe(() => authority)!;
    await fence.stopAfterInFlight(() async {}, authorityProvider: () => authority);
    epoch++;
    fence.invalidate();
    final replacement = _Authority('owner-a', () => epoch == 1);
    final current = fence.observe(() => replacement);
    expect(current, isNotNull);
    expect(current!.isCurrent, isTrue);
    expect(oldRead.isCurrent, isFalse);
    expect(fence.choose(() => replacement, true), isNotNull);
  });
}
