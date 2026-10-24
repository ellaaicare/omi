import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'whisper_delivery_client_test.dart' as baseline;

class ClosedHarness {
  final h = baseline.Harness();
  String code = 'delivery_stop_request_stale';
  Completer<Map<String, dynamic>>? held;
  void Function(Map<String, dynamic>)? corrupt;
  bool denyCommit = false;
  bool denyRead = false;
  bool denyPending = false;
  late final client = WhisperDeliveryClient(
    media: h.media,
    newId: h.id,
    authorityProvider: () => h.authority,
    lifetimeIdentity: (_) => h.lifetime,
    read: h.client.read,
    write: write,
    transport: transport,
  );
  Future<void> write(String uid, Map<String, dynamic> state) async {
    if (denyPending && state['pending_stop'] != null) throw StateError('fictional pending persistence failure');
    if (denyCommit && state['pending_stop'] == null) throw StateError('fictional disk failure');
    await h.client.write(uid, state);
  }

  Future<Map<String, dynamic>> transport(
      String method, String path, Map<String, dynamic> fields, ExactAccountAuthorityVerifier authority) async {
    if (path == 'stop') {
      h.requests.add((method: method, path: path, body: h.copy(fields)));
      return decodeWhisperDeliveryTransportResponse(
          method,
          path,
          409,
          jsonEncode({
            'detail': {'code': code}
          }));
    }
    final response = await h.client.transport(method, path, fields, authority);
    if (path == 'next') {
      if (denyRead) throw WhisperDeliveryFailure();
      corrupt?.call(response);
      if (held != null) return held!.future;
    }
    return response;
  }

  void close() {
    h.state = {
      ...h.copy(h.state!),
      'closed': true,
      'revision': 2,
      'stop_request_id': h.id(),
      'cursor': {'revision': 2, 'position': 0},
      'event': null
    };
  }

  int get reads => h.requests.where((r) => r.path == 'next').length;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('late Off verifies exact independently closed scope without inventing ACK or new session', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    await x.client.stop(resume: false);
    expect(x.client.ready, false);
    expect(x.client.stopPending, false);
    expect(x.client.acknowledgedStopRequest, isNull);
    expect(x.h.saved!['scope'], isNull);
    expect(x.reads, 1);
    expect(x.h.requests.where((r) => r.path == 'sessions').length, 1);
  });
  for (final resume in [true, false]) {
    test('unrelated conflict or requested resume preserves failure ($resume)', () async {
      final x = ClosedHarness();
      if (!resume) x.code = 'delivery_revision_stale';
      await x.client.start(WhisperPresentation.audio);
      x.close();
      await expectLater(x.client.stop(resume: resume), throwsA(isA<WhisperDeliveryFailure>()));
      expect(x.reads, 0);
      expect(x.client.stopPending, true);
      expect(x.client.acknowledgedStopRequest, isNull);
    });
  }
  final mutations = <String, void Function(Map<String, dynamic>)>{
    'owner': (m) => m['uid'] = 'foreign-owner',
    'device': (m) => m['device_id'] = '00000000-0000-4000-8000-000000000999',
    'session': (m) => m['session_id'] = '00000000-0000-4000-8000-000000000999',
    'epoch': (m) => m['client_epoch'] = '00000000-0000-4000-8000-000000000999',
    'presentation': (m) => m['presentation'] = 'invalid',
    'revision': (m) => m['revision'] = 3,
    'contract': (m) => m['contract'] = true,
    'open': (m) => m['closed'] = false,
    'event': (m) => m['event'] = {'fictional': true},
    'stop identity': (m) => m['stop_request_id'] = null,
    'cursor': (m) => m['cursor'] = {'revision': 2, 'position': -1},
    'resume': (m) => m['resume'] = {'fictional': true},
  };
  for (final mutation in mutations.entries) {
    test('closed read refuses ${mutation.key}', () async {
      final x = ClosedHarness()..corrupt = mutation.value;
      await x.client.start(WhisperPresentation.audio);
      x.close();
      await expectLater(x.client.stop(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
      expect(x.client.stopPending, true);
      expect(x.client.acknowledgedStopRequest, isNull);
    });
  }
  test('retired same-UID authority cannot publish held closed read', () async {
    final x = ClosedHarness()..held = Completer();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    final result = expectLater(x.client.stop(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
    while (x.reads == 0) {
      await Future<void>.delayed(Duration.zero);
    }
    x.h.authority.current = false;
    x.h.authority = baseline.Authority('synthetic-owner');
    x.held!.complete(x.h.copy(x.h.state!));
    await result;
    expect(x.client.acknowledgedStopRequest, isNull);
    expect(x.h.saved!['pending_stop'], isNotNull);
  });
  test('unconfirmed native stop never reads server closure', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    x.h.media.denyStop = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    expect(x.reads, 0);
    expect(x.client.stopPending, true);
  });
  test('readback consent or guardian-mode refusal retains pending Stop', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    x.denyRead = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
    expect(x.client.stopPending, true);
    expect(x.h.saved!['pending_stop'], isNotNull);
    expect(x.client.acknowledgedStopRequest, isNull);
  });
  test('repeated Off cannot accept closure while native Stop remains denied', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    final before = x.h.media.effects.where((e) => e.startsWith('stop:')).length;
    x.h.media.denyStop = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    expect(x.h.media.effects.where((e) => e.startsWith('stop:')).length, before + 2);
    expect(x.reads, 0);
    expect(x.client.stopPending, true);
    expect(x.client.acknowledgedStopRequest, isNull);
    x.h.media.denyStop = false;
    await x.client.stop(resume: false);
    expect(x.h.media.effects.where((e) => e.startsWith('stop:')).length, before + 3);
    expect(x.reads, 1);
    expect(x.client.stopPending, false);
    expect(x.client.acknowledgedStopRequest, isNull);
  });
  test('reconciliation must durably retain pending Stop before server request', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    x.h.media.denyStop = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    x.h.media.denyStop = false;
    x.denyPending = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    expect(x.h.requests.where((r) => r.path == 'stop'), isEmpty);
    expect(x.reads, 0);
    expect(x.client.stopPending, true);
    x.denyPending = false;
    await x.client.stop(resume: false);
    expect(x.reads, 1);
    expect(x.client.stopPending, false);
  });
  test('failed durable clear retains original pending Stop', () async {
    final x = ClosedHarness();
    await x.client.start(WhisperPresentation.audio);
    x.close();
    x.denyCommit = true;
    await expectLater(x.client.stop(resume: false), throwsA(isA<StateError>()));
    expect(x.client.stopPending, true);
    expect(x.h.saved!['pending_stop'], isNotNull);
    expect(x.client.acknowledgedStopRequest, isNull);
  });
  final malformed = <(String, String, int, String)>[
    ('GET', 'stop', 409, '{"detail":{"code":"delivery_stop_request_stale"}}'),
    ('POST', 'next', 409, '{"detail":{"code":"delivery_stop_request_stale"}}'),
    ('POST', 'stop', 403, '{"detail":{"code":"delivery_stop_request_stale"}}'),
    ('POST', 'stop', 409, '{"detail":"delivery_stop_request_stale"}'),
    ('POST', 'stop', 409, '{"detail":{"code":"delivery_stop_request_stale","extra":1}}'),
    ('POST', 'stop', 409, '{"detail":{"code":"delivery_revision_stale"}}'),
    ('POST', 'stop', 409, 'not-json'),
  ];
  for (var i = 0; i < malformed.length; i++) {
    test('transport refuses broad conflict hint $i', () {
      final v = malformed[i];
      expect(
          () => decodeWhisperDeliveryTransportResponse(v.$1, v.$2, v.$3, v.$4),
          throwsA(
              isA<WhisperDeliveryFailure>().having((e) => e is WhisperDeliveryStopRequestStale, 'typed hint', false)));
    });
  }
  test('actual runtime retains warning across repeated denied native Stop', () async {
    final x = ClosedHarness();
    final channel = MethodChannel('fictional.closed.retry.v2');
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    var denied = false;
    var stops = 0;
    messenger.setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'strictStop') {
        stops++;
        return {'stopped': !denied};
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
      denied = true;
      final before = stops;
      await expectLater(runtime.stopOutput(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
      await expectLater(runtime.stopOutput(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
      expect(stops, before + 2);
      expect(x.reads, 0);
      expect(runtime.failed, true);
      expect(runtime.client.stopPending, true);
      denied = false;
      await runtime.stopOutput(resume: false);
      expect(stops, before + 3);
      expect(x.reads, 1);
      expect(runtime.failed, false);
      expect(runtime.client.stopPending, false);
      expect(runtime.client.acknowledgedStopRequest, isNull);
    } finally {
      denied = false;
      await runtime.client.retireAuthority();
      runtime.dispose();
      messenger.setMockMethodCallHandler(channel, null);
    }
  });
  for (final known in [true, false]) {
    test('actual runtime warning clears only after exact closed proof ($known)', () async {
      final x = ClosedHarness();
      if (!known) x.code = 'delivery_revision_stale';
      final channel = MethodChannel('fictional.closed.stop.$known');
      final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(
          channel, (call) async => call.method == 'strictStop' ? {'stopped': true} : {'accepted': true});
      final runtime = WhisperDeliveryRuntime.testing(
          transport: x.transport,
          read: x.h.client.read,
          write: x.write,
          authorityProvider: () => x.h.authority,
          channel: channel);
      try {
        await runtime.client.start(WhisperPresentation.audio);
        x.close();
        if (known) {
          await runtime.stopOutput(resume: false);
          expect(runtime.failed, false);
        } else {
          await expectLater(runtime.stopOutput(resume: false), throwsA(isA<WhisperDeliveryFailure>()));
          expect(runtime.failed, true);
        }
        expect(runtime.client.ready, false);
        expect(runtime.client.acknowledgedStopRequest, isNull);
      } finally {
        await runtime.client.retireAuthority();
        runtime.dispose();
        messenger.setMockMethodCallHandler(channel, null);
      }
    });
  }
}
