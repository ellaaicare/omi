import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'thick_closed_stop_v2_test.dart' as closed;
import 'whisper_delivery_client_test.dart' as baseline;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('exact server-closed poll retires native output without failure or invented ACK', () async {
    final x = closed.ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    final before = x.h.media.effects.where((e) => e.startsWith('stop:')).length;
    await x.client.poll();
    expect(x.client.ready, false);
    expect(x.client.stopPending, false);
    expect(x.h.media.effects.where((e) => e.startsWith('stop:')).length, before + 1);
    expect(x.client.acknowledgedStopRequest, isNull);
    expect(x.h.saved!['scope'], isNull);
    expect(x.h.requests.where((r) => r.path == 'stop'), isEmpty);
    expect(x.h.requests.where((r) => r.path == 'sessions').length, 1);
    await x.client.poll();
    expect(x.reads, 1);
  });
  final mutations = <String, void Function(Map<String, dynamic>)>{
    'owner': (m) => m['uid'] = 'foreign',
    'device': (m) => m['device_id'] = baseline.event,
    'session': (m) => m['session_id'] = baseline.event,
    'epoch': (m) => m['client_epoch'] = baseline.event,
    'presentation': (m) => m['presentation'] = 'text_only',
    'revision': (m) => m['revision'] = 3,
    'contract': (m) => m['contract'] = true,
    'event': (m) => m['event'] = {'fictional': true},
    'stop identity': (m) => m['stop_request_id'] = null,
    'cursor': (m) => m['cursor'] = {'revision': 2, 'position': -1},
    'resume': (m) => m['resume'] = {'fictional': true},
  };
  for (final entry in mutations.entries) {
    test('closed poll rejects ${entry.key}', () async {
      final x = closed.ClosedHarness()..corrupt = entry.value;
      await x.client.start(WhisperPresentation.audio);
      x.close();
      await expectLater(x.client.poll(), throwsA(isA<WhisperDeliveryFailure>()));
      expect(x.client.ready, false);
      expect(x.h.saved!['scope'], isNotNull);
      expect(x.client.acknowledgedStopRequest, isNull);
    });
  }
  test('closed poll native refusal retains pending; retry requires a fresh native Stop', () async {
    final x = closed.ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    x.h.media.denyStop = true;
    await expectLater(x.client.poll(), throwsA(anything));
    expect(x.client.stopPending, true);
    final before = x.h.media.effects.length;
    await expectLater(x.client.reconcileStop(), throwsA(anything));
    expect(x.h.media.effects.length, before + 1);
    expect(x.client.stopPending, true);
    x.h.media.denyStop = false;
    await x.client.reconcileStop();
    expect(x.client.stopPending, false);
    expect(x.client.acknowledgedStopRequest, isNull);
  });
  for (final pending in [true, false]) {
    test('closed poll ${pending ? 'pending' : 'commit'} persistence failure retains original scope', () async {
      final x = closed.ClosedHarness();
      await x.client.start(WhisperPresentation.audio);
      x.close();
      if (pending) {
        x.denyPending = true;
      } else {
        x.denyCommit = true;
      }
      await expectLater(x.client.poll(), throwsA(anything));
      expect(x.client.stopPending, true);
      expect(x.client.acknowledgedStopRequest, isNull);
      x.denyPending = false;
      x.denyCommit = false;
      await x.client.reconcileStop();
      expect(x.client.stopPending, false);
      expect(x.h.saved!['scope'], isNull);
    });
  }
  test('held closed response cannot retire a replacement same-UID authority', () async {
    final x = closed.ClosedHarness()..held = Completer<Map<String, dynamic>>();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    final result = expectLater(x.client.poll(), throwsA(isA<WhisperDeliveryFailure>()));
    while (x.reads == 0) {
      await Future<void>.delayed(Duration.zero);
    }
    x.h.authority.current = false;
    x.h.authority = baseline.Authority('synthetic-owner');
    x.held!.complete(x.h.copy(x.h.state!));
    await result;
    expect(x.client.acknowledgedStopRequest, isNull);
  });
  test('actual runtime exact closed poll confirms native Stop without public failure', () async {
    final x = closed.ClosedHarness();
    const channel = MethodChannel('fictional.closed.poll');
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    var stops = 0;
    messenger.setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'strictStop') {
        stops++;
        return {'stopped': true};
      }
      return {'accepted': true};
    });
    final runtime = WhisperDeliveryRuntime.testing(
        transport: x.transport,
        read: x.h.client.read,
        write: x.write,
        authorityProvider: () => x.h.authority,
        channel: channel);
    try {
      await runtime.client.start(WhisperPresentation.audio);
      x.close();
      final before = stops;
      await runtime.client.poll();
      expect(stops, before + 1);
      expect(runtime.failed, false);
      expect(runtime.playing, false);
      expect(runtime.client.ready, false);
      expect(runtime.client.stopPending, false);
    } finally {
      await runtime.client.retireAuthority();
      runtime.dispose();
      messenger.setMockMethodCallHandler(channel, null);
    }
  });
}
