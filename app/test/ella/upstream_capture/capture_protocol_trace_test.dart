import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/capture_host/ella_capture_protocol_trace.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';

class _Transport extends Fake implements IPureSocket {
  IPureSocketListener? listener;
  Completer<void>? connectBarrier;
  PureSocketStatus state = PureSocketStatus.notConnected;
  @override
  PureSocketStatus get status => state;
  @override
  void setListener(IPureSocketListener value) => listener = value;
  @override
  Future<bool> connect() async {
    await connectBarrier?.future;
    state = PureSocketStatus.connected;
    listener?.onConnected();
    return true;
  }

  @override
  Future<void> stop() async {
    state = PureSocketStatus.disconnected;
  }

  @override
  void send(dynamic value) {}
  void ready({bool valid = true}) => listener?.onMessage(jsonEncode({
        'type': 'service_status',
        'status': 'capture_protocol_ready',
        'protocol_version': 2,
        'conversation_id': 'SECRET-conversation',
        'generation': 'SECRET-generation',
        'owner_token': valid ? 'SECRET-token' : 44,
      }));
  void closed([int? code]) {
    state = PureSocketStatus.disconnected;
    listener?.onClosed(code);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('bounded immutable closed schema ignores old capabilities even after reset', () {
    final trace = EllaCaptureProtocolTrace(utcNow: () => DateTime.utc(2026), monotonicMicros: () => 42);
    final old = trace.begin();
    for (var i = 0; i < 100; i++) {
      trace.record(old, CaptureTraceEvent.deliveryChanged, deliveryOptIn: i.isEven);
    }
    expect(trace.snapshot, hasLength(64));
    expect(() => trace.snapshot.last.toJson()['raw'] = 'SECRET', throwsUnsupportedError);
    trace.clear();
    final current = trace.begin();
    trace.record(old, CaptureTraceEvent.failure, failure: CaptureTraceFailure.other);
    expect(trace.snapshot, hasLength(1));
    trace.record(current, CaptureTraceEvent.readyWaitBegin, deadline: const Duration(seconds: 8), closeCode: -1);
    expect(trace.snapshot.last.toJson()['deadline_monotonic_us'], 8000042);
    expect(trace.snapshot.last.toJson().containsKey('close_code'), isFalse);
  });
  test('field classification and reason mapping never retain payload values', () {
    final map = {'conversation_id': 'SECRET', 'generation': '', 'owner_token': 44, 'protocol_version': 2};
    expect(EllaCaptureProtocolTrace.fieldType(map, 'conversation_id'), CaptureTraceFieldType.string);
    expect(EllaCaptureProtocolTrace.fieldType(map, 'generation'), CaptureTraceFieldType.emptyString);
    expect(EllaCaptureProtocolTrace.fieldType(map, 'owner_token'), CaptureTraceFieldType.integer);
    expect(EllaCaptureProtocolTrace.fieldType(map, 'missing'), CaptureTraceFieldType.absent);
    expect(EllaCaptureProtocolTrace.classifyFailure('SECRET exception'), CaptureTraceFailure.other);
    expect(EllaCaptureProtocolTrace.classifyStop('SECRET reason'), CaptureTraceStopReason.other);
  });
  for (final valid in [true, false]) {
    testWidgets('actual ready validation sanitized valid=$valid', (tester) async {
      final trace = EllaCaptureProtocolTrace();
      final transport = _Transport();
      final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport,
          diagnosticTrace: trace, diagnosticAttempt: trace.begin());
      final start = socket.start();
      await tester.pump();
      transport.ready(valid: valid);
      await tester.pump();
      await start;
      final entry = trace.snapshot.where((e) => e.toJson()['event'] == 'readyValidated').single.toJson();
      expect(entry['validation'], valid ? 'initial' : 'invalidFields');
      expect(entry['tuple_current'], valid);
      expect(entry['tuple_sha256'], '988abd906f89f840f348deb5dd96d1c18ffc4f2b06ad184a818d5e37e8a3b58d');
      expect(EllaCaptureProtocolTrace.supportText(trace.snapshot), isNot(contains('SECRET')));
      transport.closed(1011);
      await socket.stop();
    });
  }
  testWidgets('8s ready deadline anchors after awaited transport start and first failure survives late ready',
      (tester) async {
    final trace = EllaCaptureProtocolTrace();
    final transport = _Transport()..connectBarrier = Completer<void>();
    final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport,
        diagnosticTrace: trace, diagnosticAttempt: trace.begin());
    final start = socket.start();
    await tester.pump(const Duration(seconds: 12));
    expect(trace.snapshot.any((e) => e.toJson()['event'] == 'readyWaitBegin'), isFalse);
    transport.connectBarrier!.complete();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 7999));
    expect(trace.snapshot.any((e) => e.toJson()['event'] == 'readyDeadline'), isFalse);
    await tester.pump(const Duration(milliseconds: 1));
    await start;
    final first = socket.lastAdmissionFailure;
    transport.ready();
    transport.closed(1001);
    expect(socket.lastAdmissionFailure, same(first));
    expect(trace.snapshot.where((e) => e.toJson()['event'] == 'failure'), hasLength(1));
    await socket.stop();
  });
  testWidgets('retired socket callbacks cannot overwrite newer attempt or owner-context trace', (tester) async {
    final trace = EllaCaptureProtocolTrace();
    var ownerCurrent = true;
    final oldTransport = _Transport();
    final old = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', oldTransport,
        hasOriginAuthority: () => ownerCurrent, diagnosticTrace: trace, diagnosticAttempt: trace.begin());
    final oldStart = old.start();
    await tester.pump();
    oldTransport.ready();
    await tester.pump();
    await oldStart;
    final currentTransport = _Transport();
    final current = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', currentTransport,
        diagnosticTrace: trace, diagnosticAttempt: trace.begin());
    final start = current.start();
    await tester.pump();
    currentTransport.ready();
    await tester.pump();
    await start;
    final before = EllaCaptureProtocolTrace.supportText(trace.snapshot);
    ownerCurrent = false;
    oldTransport.closed(1008);
    old.onError(StateError('SECRET'), StackTrace.empty);
    oldTransport.ready();
    expect(EllaCaptureProtocolTrace.supportText(trace.snapshot), before);
    expect(current.lastAdmissionFailure, isNull);
    trace.clear();
    currentTransport.closed(1011);
    expect(trace.snapshot, isEmpty, reason: 'retired account capabilities cannot repopulate cleared trace');
    await old.stop();
    await current.stop();
  });
  for (final ready in [false, true]) {
    testWidgets('close callback retains actual optional code and before/after ready=$ready', (tester) async {
      final trace = EllaCaptureProtocolTrace();
      final transport = _Transport();
      final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport,
          diagnosticTrace: trace, diagnosticAttempt: trace.begin());
      final start = socket.start();
      await tester.pump();
      if (ready) {
        transport.ready();
        await tester.pump();
      }
      transport.closed();
      await tester.pump();
      await start;
      final close = trace.snapshot.where((e) => e.toJson()['event'] == 'closeCallback').single.toJson();
      expect(close['was_ready'], ready);
      expect(close.containsKey('close_code'), isFalse);
      await socket.stop();
    });
  }
}
