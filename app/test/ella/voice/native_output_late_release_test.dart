import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/services/native_dictation_output_admission.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'foreground_dictation_host_test.dart' show Engine;
import 'ephemeral_dictation_session_test.dart' show Rig;
import '../audio_ownership/standard_speech_held_listen_test.dart' show settleSpeech;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('fictional-late-output-release-successor');
  late AppOutputAdmission registry;
  late List<MethodCall> calls;
  Map<String, Object> reply(MethodCall c, String flag, {bool ok = true}) =>
      {'version': 1, 'request_id': (c.arguments as Map)['request_id'] as String, flag: ok};
  NativeDictationOutputAdmission adapter() =>
      NativeDictationOutputAdmission(admission: registry, channel: channel, deadline: const Duration(milliseconds: 30));
  void handler(Future<Object?> Function(MethodCall) f) {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (c) async {
      calls.add(c);
      return await f(c);
    });
  }

  setUp(() {
    registry = AppOutputAdmission();
    calls = [];
  });

  test('pending timeout retry shares original receipt and retains exact output hold', () async {
    final release = Completer<Object?>();
    handler((c) async {
      if (c.method == 'dictationReleaseOutput') return await release.future;
      return reply(c, 'held');
    });
    final lease = adapter().tryReserve()!;
    await lease.prepareBeforePermissions();
    final first = lease.releaseAfterInputStopped();
    await expectLater(first, throwsA(isA<TimeoutException>()));
    final retry = lease.releaseAfterInputStopped();
    expect(identical(first, retry), true);
    await expectLater(retry, throwsA(isA<TimeoutException>()));
    expect(registry.dictationReserved, true);
    expect(calls.where((c) => c.method == 'dictationReleaseOutput').length, 1);
    release.complete(reply(calls.last, 'released'));
    await settleSpeech();
    final count = calls.length;
    await lease.releaseAfterInputStopped();
    expect(registry.dictationReserved, false);
    expect(calls.length, count);
    await expectLater(first, throwsA(isA<TimeoutException>()));
  });

  test('native release ACK cannot grant success while exact raw preparation remains pending', () async {
    final acquire = Completer<Object?>();
    handler((c) async {
      if (c.method == 'dictationAcquireOutput') return await acquire.future;
      return reply(c, 'released');
    });
    final lease = adapter().tryReserve()!;
    final preparing = expectLater(lease.prepareBeforePermissions(), throwsA(isA<TimeoutException>()));
    final first = lease.releaseAfterInputStopped();
    await expectLater(first, throwsA(isA<TimeoutException>()));
    await preparing;
    final pending = lease.releaseAfterInputStopped();
    expect(identical(first, pending), true);
    await expectLater(pending, throwsA(isA<TimeoutException>()));
    expect(registry.dictationReserved, true);
    acquire.complete(reply(calls.first, 'held'));
    await settleSpeech();
    final count = calls.length;
    await lease.releaseAfterInputStopped();
    expect(calls.length, count);
    expect(registry.dictationReserved, false);
  });

  test('late rejected raw release does not promote cached timeout to success', () async {
    final release = Completer<Object?>();
    var attempts = 0;
    handler((c) async {
      if (c.method == 'dictationReleaseOutput') {
        attempts++;
        if (attempts == 1) return await release.future;
        return reply(c, 'released', ok: attempts > 2);
      }
      return reply(c, 'held');
    });
    final lease = adapter().tryReserve()!;
    await lease.prepareBeforePermissions();
    await expectLater(lease.releaseAfterInputStopped(), throwsA(isA<TimeoutException>()));
    release.complete(reply(calls.last, 'released', ok: false));
    await settleSpeech();
    await expectLater(lease.releaseAfterInputStopped(), throwsStateError);
    expect(registry.dictationReserved, true);
    expect(adapter().tryReserve(), isNull);
    await lease.releaseAfterInputStopped();
    expect(registry.dictationReserved, false);
    expect(calls.map((c) => (c.arguments as Map)['request_id']).toSet().length, 1);
  });

  test('old confirmed release retry cannot touch a newer exact native/Dart owner', () async {
    final release = Completer<Object?>();
    var releaseCount = 0;
    handler((c) async {
      if (c.method == 'dictationReleaseOutput') {
        releaseCount++;
        if (releaseCount == 1) return await release.future;
        return reply(c, 'released');
      }
      return reply(c, 'held');
    });
    final a = adapter();
    final old = a.tryReserve()!;
    await old.prepareBeforePermissions();
    await expectLater(old.releaseAfterInputStopped(), throwsA(isA<TimeoutException>()));
    release.complete(reply(calls.last, 'released'));
    await settleSpeech();
    final newer = a.tryReserve()!;
    await newer.prepareBeforePermissions();
    final count = calls.length;
    await old.releaseAfterInputStopped();
    expect(calls.length, count);
    expect(newer.isCurrent, true);
    expect(registry.dictationReserved, true);
    expect(old.isCurrent, false);
    await newer.releaseAfterInputStopped();
  });

  test('actual host account drift during late confirmed release recovers cleanup without new permission or publication',
      () async {
    final release = Completer<Object?>();
    handler((c) async {
      if (c.method == 'dictationReleaseOutput') return await release.future;
      return reply(c, 'held');
    });
    final r = Rig(), e = Engine();
    var authority = true;
    final a = adapter();
    final host = ForegroundDictationHost(
        registration: ForegroundDictationRegistration(
            isPolicyEnabled: () => true, reserveOutput: a.tryReserve, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: r.revision),
        hasExactAuthority: () => authority,
        canStartFresh: () => true,
        applyDraft: (_) {
          r.applies++;
        });
    await host.start();
    await host.finish();
    expect(host.hasHeldOutput, true);
    final publicationsAtDrift = r.applies;
    final draftAtDrift = r.value;
    authority = false;
    release.complete(reply(calls.last, 'released'));
    await settleSpeech();
    final newOwner = registry.tryReserveDictation()!;
    final count = calls.length;
    await host.retire();
    expect(host.hasHeldOutput, false);
    expect(calls.length, count);
    expect(newOwner.isCurrent, true);
    expect(e.permissions, 1);
    expect(r.applies, publicationsAtDrift);
    expect(r.value, draftAtDrift);
    await newOwner.releaseAfterInputStopped();
    host.dispose();
    r.lease.close();
  });
}
