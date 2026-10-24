import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/owned/owned_prepared_native_api.dart';
import 'package:omi/services/mic/owned/owned_phone_mic.g.dart';
import 'owned_capture_controller_test.dart' show binding;

void main() {
  final testBinding = TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('ella.owned_private_capture.v1');
  late PreparedOwnedPhoneMicApi api;
  setUp(() => api = PreparedOwnedPhoneMicApi());
  tearDown(() => testBinding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
  void reply(FutureOr<Object?> Function(MethodCall) handler) =>
      testBinding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async => handler(call));
  test('actual StandardMethodCodec capabilities exact typed map', () async {
    reply((call) {
      expect(call.method, 'getCapabilities');
      expect(call.arguments, {'version': 1});
      return {
        'version': 1,
        'preparedLifecycle': true,
        'privateStore': true,
        'retirement': true,
        'neverStartedCancel': true
      };
    });
    expect((await api.getCapabilities()).supported, true);
  });
  for (final malformed in [
    {'version': true, 'preparedLifecycle': true, 'privateStore': true, 'retirement': true, 'neverStartedCancel': true},
    {'version': 1, 'preparedLifecycle': 1, 'privateStore': true, 'retirement': true, 'neverStartedCancel': true},
    {
      'version': 1,
      'preparedLifecycle': true,
      'privateStore': true,
      'retirement': true,
      'neverStartedCancel': true,
      'extra': 0
    }
  ]) {
    test('capabilities rejects malformed closed map ${malformed.toString()}', () async {
      reply((_) => malformed);
      await expectLater(api.getCapabilities(), throwsStateError);
    });
  }
  test('held prepare input mutation cannot change original exact binding', () async {
    final gate = Completer<Object?>();
    final b = binding();
    final original = b.encode();
    reply((call) {
      expect((call.arguments as Map)['binding'], original);
      return gate.future;
    });
    final result = api.prepareOwnedStream(b);
    b.captureStartId = 'foreign';
    gate.complete({
      'version': 1,
      'handle': [original, 11]
    });
    expect((await result).binding.captureStartId, 'capture-A');
  });
  for (final epoch in [true, 1.5, 0, -1, 9223372036854775807]) {
    test('prepare rejects invalid epoch $epoch', () async {
      reply((_) => {
            'version': 1,
            'handle': [binding().encode(), epoch]
          });
      await expectLater(api.prepareOwnedStream(binding()), throwsStateError);
    });
  }
  test('held start snapshots original mutable handle', () async {
    final gate = Completer<Object?>();
    final h = OwnedMicHandle(binding: binding(), nativeEpoch: 11);
    final wire = [binding().encode(), 11];
    reply((_) => gate.future);
    final result = api.startPreparedOwnedStream(h, 'token-A');
    h.nativeEpoch = 12;
    h.binding.captureStartId = 'foreign';
    gate.complete({'version': 1, 'storeToken': 'token-A', 'handle': wire});
    await result;
  });
  test('cancellation requires actual exact boolean never-started proof', () async {
    reply((_) => {
          'version': 1,
          'storeToken': 'token-A',
          'handle': [binding().encode(), 11],
          'cancelledBeforeStart': 1
        });
    await expectLater(api.cancelPreparedOwnedStream(OwnedMicHandle(binding: binding(), nativeEpoch: 11), 'token-A'),
        throwsStateError);
  });
  test('cancellation snapshots mutable input and accepts exact proof only', () async {
    final gate = Completer<Object?>();
    final h = OwnedMicHandle(binding: binding(), nativeEpoch: 11);
    reply((_) => gate.future);
    final result = api.cancelPreparedOwnedStream(h, 'token-A');
    h.nativeEpoch = 12;
    gate.complete({
      'version': 1,
      'storeToken': 'token-A',
      'handle': [binding().encode(), 11],
      'cancelledBeforeStart': true
    });
    expect((await result).handle.nativeEpoch, 11);
  });
}
