import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/services/native_dictation_output_admission.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('fictional-native-output-exclusion');
  late AppOutputAdmission registry;
  late List<MethodCall> calls;
  Map<String, Object> reply(MethodCall call, String flag, {bool success = true}) =>
      {'version': 1, 'request_id': (call.arguments as Map)['request_id'] as String, flag: success};
  NativeDictationOutputAdmission adapter({Duration deadline = const Duration(seconds: 1)}) =>
      NativeDictationOutputAdmission(admission: registry, channel: channel, deadline: deadline);
  void handler(Future<Object?> Function(MethodCall) callback) {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return callback(call);
    });
  }

  setUp(() {
    registry = AppOutputAdmission();
    calls = [];
    handler((call) async => reply(call, call.method == 'dictationReleaseOutput' ? 'released' : 'held'));
  });

  test('definitive native terminal denial releases Dart without guessing a native stop', () async {
    handler((call) async => reply(call, 'held', success: false));
    final lease = adapter().tryReserve()!;
    await expectLater(lease.prepareBeforePermissions(), throwsA(isA<NativeDictationOutputBusy>()));
    await lease.releaseAfterInputStopped();
    expect(calls.length, 1);
    expect(registry.dictationReserved, false);
  });

  test('Dart busy denies before any native acquisition or takeover', () {
    registry.reserveOutput('fictional pending output');
    expect(adapter().tryReserve(), isNull);
    expect(calls, isEmpty);
  });

  test('closed actual codec acquisition and proof precede release with one canonical identity', () async {
    final lease = adapter().tryReserve()!;
    expect(() => registry.reserveOutput('late output'), throwsStateError);
    await lease.prepareBeforePermissions();
    expect(calls.map((c) => c.method), ['dictationAcquireOutput', 'dictationProofOutput']);
    expect(lease.isCurrent, true);
    final id = (calls.first.arguments as Map)['request_id'] as String;
    expect(id, matches(RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')));
    await lease.releaseAfterInputStopped();
    expect(calls.last.method, 'dictationReleaseOutput');
    expect(calls.every((c) => (c.arguments as Map).length == 1 && (c.arguments as Map)['request_id'] == id), true);
    expect(lease.isCurrent, false);
    expect(registry.dictationReserved, false);
  });

  test('ambiguous proof and failed release preserve same identity for exact retry', () async {
    var releases = 0;
    handler((call) async {
      if (call.method == 'dictationProofOutput') return {...reply(call, 'held'), 'request_id': 'foreign'};
      if (call.method == 'dictationReleaseOutput') {
        releases++;
        return reply(call, 'released', success: releases > 1);
      }
      return reply(call, 'held');
    });
    final lease = adapter().tryReserve()!;
    await expectLater(lease.prepareBeforePermissions(), throwsStateError);
    await expectLater(lease.releaseAfterInputStopped(), throwsStateError);
    expect(registry.dictationReserved, true);
    expect(adapter().tryReserve(), isNull);
    await lease.releaseAfterInputStopped();
    expect(registry.dictationReserved, false);
    expect(calls.map((c) => (c.arguments as Map)['request_id']).toSet().length, 1);
  });

  test('extra or missing native reply fields cannot grant permission admission', () async {
    handler((call) async => call.method == 'dictationReleaseOutput'
        ? reply(call, 'released')
        : {...reply(call, 'held'), 'uncheckedQuiet': true});
    final lease = adapter().tryReserve()!;
    await expectLater(lease.prepareBeforePermissions(), throwsStateError);
    expect(lease.isCurrent, false);
    expect(registry.dictationReserved, true);
    await lease.releaseAfterInputStopped();
  });

  test('held acquisition timeout shares original deadline with joining and late callers', () async {
    final held = Completer<Object?>();
    handler((call) async => call.method == 'dictationAcquireOutput' ? await held.future : reply(call, 'released'));
    final lease = adapter(deadline: const Duration(milliseconds: 30)).tryReserve()!;
    final first = lease.prepareBeforePermissions();
    final firstError = expectLater(first, throwsA(isA<TimeoutException>()));
    final join = lease.prepareBeforePermissions();
    expect(identical(first, join), true);
    final joinError = expectLater(join, throwsA(isA<TimeoutException>()));
    await Future.wait([firstError, joinError]);
    expect(identical(first, lease.prepareBeforePermissions()), true);
    await expectLater(lease.prepareBeforePermissions(), throwsA(isA<TimeoutException>()));
    expect(registry.dictationReserved, true);
    final closing = lease.releaseAfterInputStopped();
    final closingCheck = expectLater(closing, completes);
    held.complete(reply(calls.first, 'held'));
    await closingCheck;
    expect(calls.where((c) => c.method == 'dictationProofOutput'), isEmpty);
    expect(registry.dictationReserved, false);
  });

  test('closing during held acquisition waits exact receipt and cannot initiate proof or input', () async {
    final held = Completer<Object?>();
    handler((call) async => call.method == 'dictationAcquireOutput' ? await held.future : reply(call, 'released'));
    final lease = adapter().tryReserve()!;
    final preparing = expectLater(lease.prepareBeforePermissions(), throwsStateError);
    await Future<void>.delayed(Duration.zero);
    final closing = lease.releaseAfterInputStopped();
    await Future<void>.delayed(Duration.zero);
    expect(calls.map((c) => c.method), ['dictationAcquireOutput', 'dictationReleaseOutput']);
    expect(registry.dictationReserved, true);
    held.complete(reply(calls.first, 'held'));
    await preparing;
    await closing;
    expect(calls.map((c) => c.method), ['dictationAcquireOutput', 'dictationReleaseOutput']);
    expect(registry.dictationReserved, false);
  });

  test('held native release keeps Dart exclusion until its actual acknowledgement', () async {
    final held = Completer<Object?>();
    handler((call) async => call.method == 'dictationReleaseOutput' ? await held.future : reply(call, 'held'));
    final lease = adapter().tryReserve()!;
    await lease.prepareBeforePermissions();
    final closing = lease.releaseAfterInputStopped();
    expect(identical(closing, lease.releaseAfterInputStopped()), true);
    await Future<void>.delayed(Duration.zero);
    expect(registry.dictationReserved, true);
    expect(() => registry.reserveOutput('late output'), throwsStateError);
    held.complete(reply(calls.last, 'released'));
    await closing;
    expect(registry.dictationReserved, false);
  });
}
