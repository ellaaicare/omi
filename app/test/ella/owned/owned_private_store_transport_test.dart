import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/services.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'package:omi/services/mic/owned/owned_phone_mic.g.dart';
import 'package:omi/services/mic/owned/owned_private_store_transport.dart';

OwnedMicBinding binding() => OwnedMicBinding(
  captureStartId: 'start',
  authorityBindingId: 'authority',
  authorityGeneration: 1,
  localSinkId: 'sink',
  sessionId: 1,
  deviceBindingId: 'phone-binding',
  sourceId: 'phone',
  pcmCodecId: 'pcm16',
  sampleRate: 16000,
  channels: 1,
  bytesPerSample: 2,
);
OwnedMicHandle handle() => OwnedMicHandle(binding: binding(), nativeEpoch: 1);
List<Object?> wh(OwnedMicHandle h) => [h.binding.encode(), h.nativeEpoch];

class FakeTransport implements OwnedPrivateTransport {
  Future<Object?> Function(String, Map<String, Object?>)? hook;
  final calls = <String>[];
  @override
  Future<Object?> invoke(String m, Map<String, Object?> a) async {
    calls.add(m);
    expect(a['version'], 1);
    if (hook != null) return hook!(m, a);
    return reply(m, a);
  }

  Object? reply(String m, Map<String, Object?> a) => switch (m) {
    'getCapabilities' => {
      'version': 1,
      'preparedLifecycle': true,
      'privateStore': true,
      'retirement': true,
      'neverStartedCancel': true,
    },
    'preparePrivateStore' => {
      'version': 1,
      'storeToken': 'opaque-token',
      'binding': a['binding'],
      'retention': a['retention'],
      'policyGeneration': 0,
    },
    'bindPrivateStore' => {'version': 1, 'storeToken': a['storeToken'], 'handle': a['handle']},
    'stagePrivatePcm' => {
      'version': 1,
      'policyGeneration': a['policyGeneration'],
      'handle': a['handle'],
      'sequence': a['sequence'],
      'acceptedValidPcmBytes': (a['pcmBytes'] as Uint8List).length,
    },
    'commitPrivateDrain' => commit(a),
    'retirePrivateStore' => {'version': 1, 'policyGeneration': a['policyGeneration'], 'retention': a['retention']},
    _ => throw StateError('unexpected'),
  };
  Map<String, Object?> commit(Map<String, Object?> a) {
    final s = a['summary'] as List;
    return {
      'version': 1,
      'policyGeneration': a['policyGeneration'],
      'ack': [
        s[0],
        s[1],
        s[5],
        s[6],
        s[7],
        s[8],
        a['previousCommitId'] == null ? 'commit-one' : 'commit-two',
        a['retention'] == 'retain' ? 'retainedExactBinding' : 'quarantinedExactBinding',
        a['previousCommitId'],
      ],
    };
  }
}

class Fixture {
  bool authority = true;
  OwnedRetentionSnapshot policy = const OwnedRetentionSnapshot(0, OwnedPrivateRetention.retain);
  final transport = FakeTransport();
  late final context = OwnedCaptureContext(
    binding: binding(),
    accountBindingId: 'account-epoch',
    windowBindingId: 'window',
    hasExactAuthority: () => authority,
    retention: () => policy,
  );
  late final sink = TransportOwnedPrivateSink(context, transport);
  Future<void> prepare() async {
    await sink.prepare();
    await sink.bindEpoch(handle());
  }

  OwnedMicFrame frame() => OwnedMicFrame(handle: handle(), sequence: 0, pcmBytes: Uint8List.fromList([1, 0]));
  OwnedMicDrainSummary summary() => OwnedMicDrainSummary(
    request: OwnedMicStopRequest(handle: handle(), transactionId: 'stop'),
    admittedInputFrames: 1,
    admittedTapBuffers: 1,
    copyFailures: 0,
    lastSequence: 0,
    emittedChunks: 1,
    emittedValidPcmBytes: 2,
    emittedPcmSha256: List.filled(64, 'a').join(),
    eosResidualValidPcmBytes: 0,
    converterReachedEos: true,
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('actual StandardMethodChannel codec preserves closed PCM and durable envelope', () async {
    const channel = MethodChannel('ella.owned_private_capture.v1');
    final fake = FakeTransport();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      final args = Map<String, Object?>.from(call.arguments as Map);
      expect(args['version'], 1);
      return fake.reply(call.method, args);
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );
    final context = OwnedCaptureContext(
      binding: binding(),
      accountBindingId: 'codec-account',
      windowBindingId: 'codec-window',
      hasExactAuthority: () => true,
      retention: () => const OwnedRetentionSnapshot(0, OwnedPrivateRetention.retain),
    );
    final sink = TransportOwnedPrivateSink(context, MethodChannelOwnedPrivateTransport());
    await sink.prepare();
    await sink.bindEpoch(handle());
    expect((await sink.stageOwnedPcm(Fixture().frame())).acceptedValidPcmBytes, 2);
    expect((await sink.commitOwnedDrain(Fixture().summary(), policy: context.retention())).commitId, 'commit-one');
  });
  test('actual codec platform error never forwards private exception details', () async {
    const channel = MethodChannel('ella.owned_private_capture.v1');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      throw PlatformException(code: 'native-private-error', message: '/private/raw/account', details: 'sensitive');
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );
    final t = MethodChannelOwnedPrivateTransport();
    try {
      await t.invoke('getCapabilities', {'version': 1});
      fail('expected');
    } catch (e) {
      expect(e.toString(), 'Bad state: owned_private_transport_failed');
    }
  });
  test('exact association prepares stages and commits without filesystem work', () async {
    final f = Fixture();
    await f.prepare();
    final a = await f.sink.stageOwnedPcm(f.frame());
    expect(a.acceptedValidPcmBytes, 2);
    final d = await f.sink.commitOwnedDrain(f.summary(), policy: f.policy);
    expect(d.disposition, OwnedMicSinkDisposition.retainedExactBinding);
  });
  for (final bad in ['extra', 'version', 'binding', 'token', 'generation']) {
    test('prepare rejects closed $bad mismatch', () async {
      final f = Fixture();
      f.transport.hook = (m, a) async {
        if (m == 'getCapabilities') return f.transport.reply(m, a);
        final r = Map<String, Object?>.from(f.transport.reply(m, a) as Map);
        switch (bad) {
          case 'extra':
            r['privatePath'] = '/private/not-exported';
          case 'version':
            r['version'] = 2;
          case 'binding':
            r['binding'] = binding().encode()..toString();
            (r['binding'] as List)[0] = 'different';
          case 'token':
            r['storeToken'] = '';
          case 'generation':
            r['policyGeneration'] = 1;
        }
        return r;
      };
      await expectLater(f.sink.prepare(), throwsStateError);
    });
  }
  test('caller mutable binding and PCM snapshot cannot change in-flight claim', () async {
    final f = Fixture();
    await f.prepare();
    final pending = Completer<Object?>();
    Map<String, Object?>? admitted;
    f.transport.hook = (m, a) {
      admitted = a;
      return pending.future;
    };
    final frame = f.frame();
    final result = f.sink.stageOwnedPcm(frame);
    frame.pcmBytes[0] = 99;
    frame.handle.binding.captureStartId = 'other';
    expect((admitted!['pcmBytes'] as Uint8List)[0], 1);
    pending.complete(f.transport.reply('stagePrivatePcm', admitted!));
    expect((await result).handle.binding.captureStartId, 'start');
  });
  for (final boundary in ['prepare', 'bind', 'stage', 'commit']) {
    test('$boundary reply after account retirement is held', () async {
      final f = Fixture();
      if (boundary != 'prepare') await f.sink.prepare();
      if (['stage', 'commit'].contains(boundary)) await f.sink.bindEpoch(handle());
      f.transport.hook = (m, a) async {
        final r = f.transport.reply(m, a);
        f.authority = false;
        return r;
      };
      final future = switch (boundary) {
        'prepare' => f.sink.prepare(),
        'bind' => f.sink.bindEpoch(handle()),
        'stage' => f.sink.stageOwnedPcm(f.frame()),
        _ => f.sink.commitOwnedDrain(f.summary(), policy: f.policy),
      };
      await expectLater(future, throwsStateError);
    });
  }
  test('cached native commit cannot return retained ACK after forbid during read', () async {
    final f = Fixture();
    await f.prepare();
    f.transport.hook = (m, a) async {
      final r = f.transport.reply(m, a);
      f.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
      return r;
    };
    await expectLater(f.sink.commitOwnedDrain(f.summary(), policy: f.policy), throwsStateError);
  });
  test('explicit old binding quarantine upgrade is allowed without new account publication', () async {
    final f = Fixture();
    await f.prepare();
    await f.sink.commitOwnedDrain(f.summary(), policy: f.policy);
    f.authority = false;
    f.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await f.sink.retirePrivateRetention(f.policy);
    final d = await f.sink.commitOwnedDrain(f.summary(), policy: f.policy, previousCommitId: 'commit-one');
    expect(d.disposition, OwnedMicSinkDisposition.quarantinedExactBinding);
    expect(d.commitId, 'commit-two');
  });
  for (final bad in ['epoch', 'transaction', 'digest', 'counts', 'disposition', 'generation']) {
    test('durable reply rejects $bad mismatch', () async {
      final f = Fixture();
      await f.prepare();
      f.transport.hook = (m, a) async {
        final r = f.transport.commit(a), ack = r['ack'] as List;
        switch (bad) {
          case 'epoch':
            ack[0] = wh(OwnedMicHandle(binding: binding(), nativeEpoch: 2));
          case 'transaction':
            ack[1] = 'other';
          case 'digest':
            ack[5] = List.filled(64, 'b').join();
          case 'counts':
            ack[4] = 4;
          case 'disposition':
            ack[7] = 'quarantinedExactBinding';
          case 'generation':
            r['policyGeneration'] = 1;
        }
        return r;
      };
      await expectLater(f.sink.commitOwnedDrain(f.summary(), policy: f.policy), throwsStateError);
    });
  }
  test('malformed summary fails before transport and leaves cleanup held', () async {
    final f = Fixture();
    await f.prepare();
    final n = f.transport.calls.length;
    final s = f.summary()..copyFailures = 1;
    await expectLater(f.sink.commitOwnedDrain(s, policy: f.policy), throwsStateError);
    expect(f.transport.calls.length, n);
  });
  test('quarantine stages exact already-admitted tail without current account', () async {
    final f = Fixture();
    await f.prepare();
    f.authority = false;
    f.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await f.sink.retirePrivateRetention(f.policy);
    expect((await f.sink.stageOwnedPcm(f.frame())).acceptedValidPcmBytes, 2);
  });
  test('forbidden rejects stage without any transport call', () async {
    final f = Fixture();
    await f.prepare();
    final n = f.transport.calls.length;
    f.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await expectLater(f.sink.stageOwnedPcm(f.frame()), throwsStateError);
    expect(f.transport.calls.length, n);
  });
  test('capability false prevents allocation', () async {
    final f = Fixture();
    f.transport.hook = (m, a) async => {
      'version': 1,
      'preparedLifecycle': true,
      'privateStore': false,
      'retirement': true,
      'neverStartedCancel': true,
    };
    await expectLater(f.sink.prepare(), throwsStateError);
    expect(f.transport.calls, ['getCapabilities']);
  });
  test('cached preparation cannot settle successfully after account retirement', () async {
    final f = Fixture();
    await f.sink.prepare();
    final n = f.transport.calls.length;
    f.authority = false;
    await expectLater(f.sink.prepare(), throwsStateError);
    expect(f.transport.calls.length, n);
  });
  test('cached preparation cannot settle successfully after policy retirement', () async {
    final f = Fixture();
    await f.sink.prepare();
    f.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await expectLater(f.sink.prepare(), throwsStateError);
  });
  test('known foreign epoch is refused before any native binding effect', () async {
    final f = Fixture();
    await f.prepare();
    final n = f.transport.calls.where((v) => v == 'bindPrivateStore').length;
    await expectLater(f.sink.bindEpoch(OwnedMicHandle(binding: binding(), nativeEpoch: 2)), throwsStateError);
    expect(f.transport.calls.where((v) => v == 'bindPrivateStore').length, n);
  });
  test('concurrent different first epochs admit only one native bind', () async {
    final f = Fixture();
    await f.sink.prepare();
    final pending = Completer<Object?>();
    Map<String, Object?>? admitted;
    f.transport.hook = (m, a) {
      admitted = a;
      return pending.future;
    };
    final first = f.sink.bindEpoch(handle());
    final same = f.sink.bindEpoch(handle());
    await expectLater(f.sink.bindEpoch(OwnedMicHandle(binding: binding(), nativeEpoch: 2)), throwsStateError);
    expect(f.transport.calls.where((v) => v == 'bindPrivateStore').length, 1);
    pending.complete(f.transport.reply('bindPrivateStore', admitted!));
    await first;
    await same;
  });
  test('ambiguous first binding permits only exact retry', () async {
    final f = Fixture();
    await f.sink.prepare();
    f.transport.hook = (m, a) async => throw StateError('held');
    await expectLater(f.sink.bindEpoch(handle()), throwsStateError);
    await expectLater(f.sink.bindEpoch(OwnedMicHandle(binding: binding(), nativeEpoch: 2)), throwsStateError);
    f.transport.hook = null;
    await f.sink.bindEpoch(handle());
    expect(f.transport.calls.where((v) => v == 'bindPrivateStore').length, 2);
  });
  test('authority/policy observer exceptions do not escape secret text', () async {
    for (final fromAuthority in [true, false]) {
      final context = OwnedCaptureContext(
        binding: binding(),
        accountBindingId: 'a',
        windowBindingId: 'w',
        hasExactAuthority: () => fromAuthority ? throw StateError('/private/authority') : true,
        retention: () => throw StateError('/private/policy'),
      );
      final sink = TransportOwnedPrivateSink(context, FakeTransport());
      try {
        await sink.prepare();
        fail('expected');
      } catch (e) {
        expect(e.toString(), isNot(contains('/private')));
      }
    }
  });
  test('transport private exception text is normalized', () async {
    final f = Fixture();
    f.transport.hook = (m, a) async => throw StateError('/private/audio/account/raw-secret');
    try {
      await f.sink.prepare();
      fail('expected');
    } catch (e) {
      expect(e.toString(), isNot(contains('/private')));
    }
  });
}
