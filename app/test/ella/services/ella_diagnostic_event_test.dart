import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/diagnostics/ella_diagnostic_event.dart';
import 'package:omi/ella/services/diagnostics/ella_diagnostic_event_journal.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  ActiveWalAuthority authority({
    String uid = 'firebase-user-a',
    String profile = 'profile-a',
    int generation = 7,
    bool Function()? currentCheck,
  }) {
    final owner = WalOwner(
      uid: uid,
      profileBindingId: profile,
      bindingRevision: 3,
      consentReceiptId: 'receipt-a',
      authorityGenerationAtCapture: generation,
    );
    return ActiveWalAuthority(
      owner: owner,
      consent: AiConsentAuthoritySnapshot(
        generation: generation,
        uid: uid,
        verifiedPersonaId: null,
        profileBindingId: profile,
        receiptId: 'receipt-a',
        policyVersion: 'policy-v1',
        processorSetHash: 'processor-hash',
        scopeVersion: 'scope-v1',
        scopeHash: 'scope-hash',
      ),
      currentCheck: currentCheck ?? () => true,
    );
  }

  test('capture trace emits opaque content-free account and device bindings', () async {
    final sink = InMemoryEllaDiagnosticEventSink();
    final trace = EllaDiagnosticCaptureTrace.begin(
      authority: authority(),
      deviceIdentifier: 'raw-ble-device-identifier',
      sink: sink,
    );

    await trace.emit(
      layer: EllaDiagnosticLayer.bleTransport,
      eventName: 'capture_attempt_started',
      outcome: EllaDiagnosticOutcome.started,
      retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
      expectedNextEvent: 'peripheral_connected',
      deadlineMs: 15000,
    );

    final json = sink.events.single.toJson();
    final encoded = json.toString();
    expect(json['schema_version'], ellaDiagnosticEventSchemaVersion);
    expect(json['account_binding_fingerprint'], authority().owner.authorityFingerprint);
    expect(json['opaque_resource_id'], hasLength(64));
    expect(encoded, isNot(contains('firebase-user-a')));
    expect(encoded, isNot(contains('profile-a')));
    expect(encoded, isNot(contains('raw-ble-device-identifier')));
    expect(encoded, isNot(contains('receipt-a')));
    expect(encoded, isNot(contains('://')));
  });

  test('capture traces are partitioned by exact authority and resource', () {
    final sink = InMemoryEllaDiagnosticEventSink();
    final first = EllaDiagnosticCaptureTrace.begin(authority: authority(), deviceIdentifier: 'device-a', sink: sink);
    final otherAccount = EllaDiagnosticCaptureTrace.begin(
      authority: authority(uid: 'firebase-user-b', profile: 'profile-b'),
      deviceIdentifier: 'device-a',
      sink: sink,
    );
    final otherDevice = EllaDiagnosticCaptureTrace.begin(
      authority: authority(),
      deviceIdentifier: 'device-b',
      sink: sink,
    );

    expect(first.accountBindingFingerprint, isNot(otherAccount.accountBindingFingerprint));
    expect(first.opaqueDeviceBinding, isNot(otherAccount.opaqueDeviceBinding));
    expect(first.opaqueDeviceBinding, isNot(otherDevice.opaqueDeviceBinding));
    expect(first.captureAttemptId, isNot(otherDevice.captureAttemptId));
  });

  test('bounded retries retain one session but use fresh attempt ids and a monotonic sequence', () async {
    final sink = InMemoryEllaDiagnosticEventSink();
    final first = EllaDiagnosticCaptureTrace.begin(authority: authority(), deviceIdentifier: 'device-a', sink: sink);
    final second = first.nextAttempt();
    final third = second.nextAttempt();

    for (final trace in <EllaDiagnosticCaptureTrace>[first, second, third]) {
      await trace.emit(
        layer: EllaDiagnosticLayer.bleTransport,
        eventName: 'capture_attempt_started',
        outcome: EllaDiagnosticOutcome.started,
        retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
        safeCounters: <String, int>{'retry_number': trace.retryNumber},
      );
    }

    expect(sink.events.map((event) => event.diagnosticSessionId).toSet(), {first.diagnosticSessionId});
    expect(sink.events.map((event) => event.captureAttemptId).toSet(), hasLength(3));
    expect(sink.events.map((event) => event.clientSequence), <int>[0, 1, 2]);
    expect(<int>[first.retryNumber, second.retryNumber, third.retryNumber], <int>[1, 2, 3]);
    expect(sink.events.map((event) => event.safeCounters['retry_number']), <int?>[1, 2, 3]);
  });

  test('diagnostics stop writing immediately when the exact capture authority drifts', () async {
    var current = true;
    final sink = InMemoryEllaDiagnosticEventSink();
    final trace = EllaDiagnosticCaptureTrace.begin(
      authority: authority(currentCheck: () => current),
      deviceIdentifier: 'device-a',
      sink: sink,
    );

    await trace.emit(
      layer: EllaDiagnosticLayer.bleTransport,
      eventName: 'peripheral_connected',
      outcome: EllaDiagnosticOutcome.succeeded,
      retryClass: EllaDiagnosticRetryClass.never,
    );
    current = false;
    await trace.emit(
      layer: EllaDiagnosticLayer.physicalAudio,
      eventName: 'audio_first_frame',
      outcome: EllaDiagnosticOutcome.succeeded,
      retryClass: EllaDiagnosticRetryClass.never,
    );

    expect(sink.events.map((event) => event.eventName), <String>['peripheral_connected']);
  });

  test('event rejects non-allowlisted counters and identifying values', () {
    final trace = EllaDiagnosticCaptureTrace.begin(
      authority: authority(),
      deviceIdentifier: 'device-a',
      sink: InMemoryEllaDiagnosticEventSink(),
    );
    final event = EllaDiagnosticEvent(
      eventId: 'event-a',
      diagnosticSessionId: trace.diagnosticSessionId,
      captureAttemptId: trace.captureAttemptId,
      accountBindingFingerprint: trace.accountBindingFingerprint,
      authorityGeneration: trace.authorityGeneration,
      layer: EllaDiagnosticLayer.serverCapture,
      eventName: 'capture_protocol_ready',
      outcome: EllaDiagnosticOutcome.failed,
      retryClass: EllaDiagnosticRetryClass.never,
      clientSequence: 0,
      clientMonotonicMs: 0,
      clientUtcTime: DateTime.utc(2026),
      firmware: 'https://example.invalid/private',
      safeCounters: const <String, int>{'transcript_words': 2},
    );

    expect(event.toJson, throwsStateError);
  });

  test('rejected diagnostic values and sink failures never escape capture callers', () async {
    final invalidValueTrace = EllaDiagnosticCaptureTrace.begin(
      authority: authority(),
      deviceIdentifier: 'device-a',
      sink: InMemoryEllaDiagnosticEventSink(),
    );
    final failingSinkTrace = EllaDiagnosticCaptureTrace.begin(
      authority: authority(),
      deviceIdentifier: 'device-a',
      sink: _ThrowingDiagnosticSink(),
    );

    await expectLater(
      invalidValueTrace.emit(
        layer: EllaDiagnosticLayer.bleTransport,
        eventName: 'peripheral_connected',
        outcome: EllaDiagnosticOutcome.succeeded,
        retryClass: EllaDiagnosticRetryClass.never,
        firmware: 'https://example.invalid/private',
      ),
      completes,
    );
    await expectLater(
      failingSinkTrace.emit(
        layer: EllaDiagnosticLayer.bleTransport,
        eventName: 'peripheral_connected',
        outcome: EllaDiagnosticOutcome.succeeded,
        retryClass: EllaDiagnosticRetryClass.never,
      ),
      completes,
    );
  });

  test('in-memory journal remains bounded', () async {
    final sink = InMemoryEllaDiagnosticEventSink(maxEvents: 2);
    final trace = EllaDiagnosticCaptureTrace.begin(authority: authority(), deviceIdentifier: 'device-a', sink: sink);
    for (var index = 0; index < 3; index++) {
      await trace.emit(
        layer: EllaDiagnosticLayer.physicalAudio,
        eventName: 'audio_frames_advancing',
        outcome: EllaDiagnosticOutcome.succeeded,
        retryClass: EllaDiagnosticRetryClass.never,
        safeCounters: <String, int>{'frames': index},
      );
    }

    expect(sink.events, hasLength(2));
    expect(sink.events.map((event) => event.clientSequence), <int>[1, 2]);
  });

  test('platform journal sends only validated event payloads', () async {
    const channel = MethodChannel('test.ella/diagnostic_events');
    final calls = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );
    final journal = EllaDiagnosticEventJournal(channel: channel);
    final trace = EllaDiagnosticCaptureTrace.begin(authority: authority(), deviceIdentifier: 'device-a', sink: journal);

    await trace.emit(
      layer: EllaDiagnosticLayer.serverCapture,
      eventName: 'capture_protocol_ready',
      outcome: EllaDiagnosticOutcome.succeeded,
      retryClass: EllaDiagnosticRetryClass.never,
    );

    expect(calls, hasLength(1));
    expect(calls.single.method, 'appendEvent');
    final payload = Map<String, Object?>.from(calls.single.arguments as Map);
    expect(payload.keys, isNot(contains('uid')));
    expect(payload.keys, isNot(contains('url')));
    expect(payload.keys, isNot(contains('transcript')));
  });

  test('platform journal exposes only append and whole-store identity-transition purge', () async {
    const channel = MethodChannel('test.ella/diagnostic_events.clear');
    final calls = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );

    await EllaDiagnosticEventJournal(channel: channel).clearAll();

    expect(calls.single.method, 'clearAllEvents');
    expect(calls.single.arguments, isNull);
  });

  test('identity-transition purge fails closed when native storage cannot be cleared', () async {
    const channel = MethodChannel('test.ella/diagnostic_events.clear.failure');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (_) async => throw PlatformException(code: 'storage_unavailable'),
    );
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );

    expect(EllaDiagnosticEventJournal(channel: channel).clearAll, throwsA(isA<PlatformException>()));
  });
}

class _ThrowingDiagnosticSink implements EllaDiagnosticEventSink {
  @override
  Future<void> append(EllaDiagnosticEvent event) => Future<void>.error(StateError('synthetic persistence failure'));
}
