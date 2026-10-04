import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as ella_preferences;
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_finalization.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/backend/schema/conversation.dart' as ella_schema;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';

import 'support/ella_upstream_capture_harness.dart';

class _Transport implements IPureSocket {
  IPureSocketListener? listener;
  Future<void> Function()? onStop;
  PureSocketStatus _status = PureSocketStatus.notConnected;
  final List<dynamic> sent = [];
  int stopCalls = 0;
  bool connectResult = true;
  Future<void> Function()? beforeConnectReturns;

  @override
  PureSocketStatus get status => _status;

  @override
  Future<bool> connect() async {
    await beforeConnectReturns?.call();
    if (!connectResult) return false;
    _status = PureSocketStatus.connected;
    listener?.onConnected();
    return true;
  }

  @override
  Future disconnect() async {
    _status = PureSocketStatus.disconnected;
  }

  @override
  Future stop() async {
    stopCalls++;
    await onStop?.call();
    _status = PureSocketStatus.disconnected;
  }

  @override
  void send(dynamic message) => sent.add(message);

  @override
  void setListener(IPureSocketListener value) => listener = value;

  @override
  void onMessage(dynamic message) => listener?.onMessage(message);

  @override
  void onConnected() => listener?.onConnected();

  @override
  void onClosed() => listener?.onClosed();

  @override
  void onError(Object err, StackTrace trace) => listener?.onError(err, trace);

  void serverStatus(String status, {Map<String, Object?> fields = const {}}) =>
      listener?.onMessage(jsonEncode({'type': 'service_status', 'status': status, ...fields}));

  void serverClose(int code) {
    _status = PureSocketStatus.disconnected;
    listener?.onClosed(code);
  }
}

class _AccountVerifier implements ExactAccountAuthorityVerifier {
  _AccountVerifier(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isExactCurrent() => current;
}

const _authority = {
  'protocol_version': 2,
  'conversation_id': 'conversation-a',
  'generation': 'generation-a',
  'owner_token': 'owner-a',
};

Future<void> _tick() async => Future<void>.delayed(const Duration(milliseconds: 1));

void registerEllaCaptureProtocolSocketCases() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('production URL preserves upstream parameters and negotiates capture protocol v2', () {
    final uri = ellaCaptureProtocolUri(Uri.parse('wss://example.test/v4/listen?codec=opus&source=friend'));
    expect(uri.path, '/v4/listen');
    expect(uri.queryParameters, {'codec': 'opus', 'source': 'friend', 'capture_protocol': '2'});
  });

  test('policy rejection quiesces capture, transient server and network closes retain upstream retry ownership', () {
    expect(captureProtocolRejectionIsPermanent('capture_socket_closed_before_ready', 1008), isTrue);
    expect(captureProtocolRejectionIsPermanent('invalid_capture_protocol_ready', null), isTrue);
    expect(captureProtocolRejectionIsPermanent('capture_socket_closed_before_ready', 1011), isFalse);
    expect(captureProtocolRejectionIsPermanent('capture_socket_closed_before_ready', 1013), isFalse);
    expect(captureProtocolRejectionIsPermanent('capture_protocol_ready_unavailable', null), isFalse);
  });

  test('composed runtime snapshot follows exact protocol readiness and content-free failure', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final originEpoch = harness.authority.bindingEpoch;
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority && harness.authority.bindingEpoch == originEpoch,
    );
    final start = socket.start();
    await _tick();
    expect(harness.runtime.captureDiagnostics.ready, isFalse);
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.ready, isTrue);
    transport.serverClose(1011);
    final failed = harness.runtime.captureDiagnostics;
    expect(failed.ready, isFalse);
    expect(failed.lastFailure?.reason, EllaCaptureSocketFailureReason.captureSocketClosed);
    expect(failed.lastFailure?.closeCode, 1011);
    harness.authority.release();
    expect(harness.runtime.captureDiagnostics.lastFailure, isNull);
    expect(await harness.bind(), isTrue);
    expect(harness.runtime.captureDiagnostics.lastFailure, isNull);
  });

  test('composed diagnostics cannot report ready from a same-UID retired socket', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-aba-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final epoch = harness.authority.bindingEpoch;
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority && harness.authority.bindingEpoch == epoch,
    );
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.ready, isTrue);
    harness.authority.release();
    expect(await harness.bind(), isTrue);
    expect(socket.state, SocketServiceState.connected, reason: 'the old transport is still awaiting teardown');
    expect(harness.runtime.captureDiagnostics.ready, isFalse);
  });

  test('tracked diagnostics do not reappear after presentation-profile ABA', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-profile-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority,
    );
    harness.runtime.trackDiagnosticSocketForTesting(socket);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.captureReady);

    await harness.ellaPreferences.saveString('aiConsentProfileBindingId', 'replacement-profile');
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull);
    await harness.ellaPreferences.saveString('aiConsentProfileBindingId', 'profile-binding-$accountA');
    expect(
      harness.runtime.captureDiagnostics.lastAttempt,
      isNull,
      reason: 'a returning profile label cannot resurrect the retired attempt',
    );
  });

  test('consent A-to-B-to-A between reads retires the old socket attempt', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-consent-rollover-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority,
    );
    harness.runtime.trackDiagnosticSocketForTesting(socket);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.captureReady);

    final before = harness.ellaPreferences.aiConsentPresentationRevision;
    harness.ellaPreferences.acceptAiConsent(
      receiptId: 'aicr_receipt-b',
      uid: accountA,
      profileBindingId: 'profile-binding-b',
      serverDecidedAt: '2026-07-28T00:00:00Z',
    );
    harness.ellaPreferences.acceptAiConsent(
      receiptId: 'aicr_receipt-$accountA',
      uid: accountA,
      profileBindingId: 'profile-binding-$accountA',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    expect(harness.ellaPreferences.aiConsentProfileBindingId, 'profile-binding-$accountA');
    expect(harness.ellaPreferences.aiConsentPresentationRevision, greaterThan(before));
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull);
    expect(
      harness.runtime.reuseProtocolSocketForTesting(
        socket,
        force: false,
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        clientConversationId: null,
      ),
      same(socket),
    );
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull);
  });

  test('presentation revision changes only for relevant value or type mutations', () async {
    SharedPreferences.setMockInitialValues({'app_locale': 'en'});
    await ella_preferences.SharedPreferencesUtil.init();
    final preferences = ella_preferences.SharedPreferencesUtil();
    final initial = preferences.aiConsentPresentationRevision;
    await preferences.saveString('app_locale', 'en');
    await preferences.saveString('deviceName', 'unrelated');
    await preferences.remove('aiConsentReceiptId');
    expect(preferences.aiConsentPresentationRevision, initial);

    final intWrite = preferences.saveInt('app_locale', 1);
    expect(preferences.aiConsentPresentationRevision, initial + 1);
    await intWrite;
    await preferences.saveDouble('app_locale', 1.0);
    expect(preferences.aiConsentPresentationRevision, initial + 2);
    await preferences.saveStringList('app_locale', ['en']);
    expect(preferences.aiConsentPresentationRevision, initial + 3);
    await preferences.saveStringList('app_locale', ['en']);
    expect(preferences.aiConsentPresentationRevision, initial + 3);
    final removing = preferences.remove('app_locale');
    expect(preferences.aiConsentPresentationRevision, initial + 4);
    await removing;
    await preferences.remove('app_locale');
    expect(preferences.aiConsentPresentationRevision, initial + 4);
    await preferences.saveBool('aiConsentAccepted', true);
    expect(preferences.aiConsentPresentationRevision, initial + 5);
    await preferences.saveBool('aiConsentAccepted', true);
    expect(preferences.aiConsentPresentationRevision, initial + 5);
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    expect(preferences.aiConsentPresentationRevision, initial + 8);

    final clearing = preferences.clear();
    expect(preferences.aiConsentPresentationRevision, initial + 9);
    await clearing;
    final reinitializing = ella_preferences.SharedPreferencesUtil.init();
    expect(preferences.aiConsentPresentationRevision, initial + 10);
    await reinitializing;
  });

  test('presentation revision covers the fixed authority-bearing string keys only', () async {
    SharedPreferences.setMockInitialValues({});
    await ella_preferences.SharedPreferencesUtil.init();
    final preferences = ella_preferences.SharedPreferencesUtil();
    const relevant = [
      'uid',
      'aiConsentReceiptId',
      'aiConsentReceiptUid',
      'aiConsentProfileBindingId',
      'aiConsentServerDecidedAt',
      'aiConsentContractVersion',
      'aiConsentProcessorSetHash',
      'aiConsentScopeVersion',
      'aiConsentScopeHash',
      'app_locale',
    ];
    var revision = preferences.aiConsentPresentationRevision;
    for (final key in relevant) {
      final writing = preferences.saveString(key, 'value-a');
      expect(preferences.aiConsentPresentationRevision, ++revision, reason: key);
      await writing;
      await preferences.saveString(key, 'value-a');
      expect(preferences.aiConsentPresentationRevision, revision, reason: '$key unchanged');
    }
    for (final key in ['aiConsentAcceptedAt', 'aiConsentClientVersion', 'deviceName', 'cachedConversationsUid']) {
      await preferences.saveString(key, 'unrelated');
      expect(preferences.aiConsentPresentationRevision, revision, reason: key);
    }
  });

  test('reinitialization retires a ready attempt before a late close event', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-reinit-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority,
    );
    harness.runtime.trackDiagnosticSocketForTesting(socket);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.ready, isTrue);

    final before = harness.ellaPreferences.aiConsentPresentationRevision;
    await ella_preferences.SharedPreferencesUtil.init();
    expect(harness.ellaPreferences.aiConsentPresentationRevision, greaterThan(before));
    transport.serverClose(1011);
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull);
    expect(harness.runtime.captureDiagnostics.lastFailure, isNull);
  });

  test('composed non-force socket reuse keeps current diagnostics without reviving a retired owner', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-diagnostic-reuse-');
    EllaCaptureProtocolSocket? socket;
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir, diagnosticSocket: () => socket);
    addTearDown(() async {
      await socket?.stop();
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final epoch = harness.authority.bindingEpoch;
    final transport = _Transport();
    socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => harness.authority.hasCurrentAuthority && harness.authority.bindingEpoch == epoch,
    );
    harness.runtime.trackDiagnosticSocketForTesting(socket);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(harness.runtime.captureDiagnostics.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.captureReady);
    final beforeReuse = harness.runtime.socketOpenGenerationForTesting;

    expect(
      harness.runtime.reuseProtocolSocketForTesting(
        socket,
        force: false,
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        clientConversationId: null,
      ),
      same(socket),
    );
    expect(harness.runtime.socketOpenGenerationForTesting, beforeReuse + 1);
    expect(harness.runtime.captureDiagnostics.ready, isTrue);
    expect(harness.runtime.captureDiagnostics.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.captureReady);

    expect(
      harness.runtime.reuseProtocolSocketForTesting(
        socket,
        force: true,
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        clientConversationId: null,
      ),
      isNull,
    );
    expect(harness.runtime.socketOpenGenerationForTesting, beforeReuse + 2);
    expect(
      harness.runtime.reuseProtocolSocketForTesting(
        socket,
        force: false,
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        clientConversationId: null,
      ),
      same(socket),
    );
    expect(harness.runtime.socketOpenGenerationForTesting, beforeReuse + 3);
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull,
        reason: 'a reuse cannot revive an owner invalidated by an intervening replacement');

    harness.authority.release();
    expect(await harness.bind(), isTrue);
    expect(
      harness.runtime.reuseProtocolSocketForTesting(
        socket,
        force: false,
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        clientConversationId: null,
      ),
      same(socket),
      reason: 'reuse does not authorize or retag a retired transport',
    );
    expect(harness.runtime.captureDiagnostics.ready, isFalse);
    expect(harness.runtime.captureDiagnostics.lastAttempt, isNull);
  });

  test('production factory replaces the upstream transport with a v2 backend URL', () async {
    final dir = await Directory.systemTemp.createTemp('ella-capture-v2-');
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
    addTearDown(() async {
      await harness.dispose();
      await dir.delete(recursive: true);
    });
    final socket = createEllaCaptureProtocolSocket(
      codec: BleAudioCodec.opus,
      sampleRate: 16000,
      language: 'multi',
      source: 'friend',
      clientConversationId: 'client-session-a',
    );
    final transport = socket.socket as PureSocket;
    final uri = Uri.parse(transport.url);
    expect(uri.path, '/v4/listen');
    expect(uri.queryParameters['capture_protocol'], '2');
    expect(uri.queryParameters['source'], 'friend');
    expect(uri.queryParameters['client_conversation_id'], 'client-session-a');
    expect(harness.authority.bind(accountA), isTrue);
    final firstEpoch = harness.authority.bindingEpoch;
    harness.authority.release();
    expect(harness.authority.bind(accountA), isTrue);
    expect(
      harness.authority.bindingEpoch,
      greaterThan(firstEpoch),
      reason: 'same-UID rebind retires old socket authority',
    );
  });

  test('handshake alone cannot publish Recording or send audio; exact ready permits ingress and drain', () async {
    final transport = _Transport();
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      timeout: const Duration(milliseconds: 200),
    );
    final start = socket.start();
    await _tick();
    expect(socket.state, SocketServiceState.disconnected);
    await socket.send(<int>[1, 2, 3]);
    expect(transport.sent, isEmpty);

    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(socket.state, SocketServiceState.connected);
    await socket.send(<int>[1, 2, 3]);
    expect(transport.sent.whereType<List<int>>(), [
      <int>[1, 2, 3],
    ]);

    final stop = socket.stop();
    await _tick();
    expect(jsonDecode(transport.sent.last as String), {'type': 'capture_drain', ..._authority});
    transport.serverStatus('capture_protocol_drained', fields: {..._authority, 'owner_token': 'stale-owner'});
    await _tick();
    expect(transport.stopCalls, 0, reason: 'a mixed-owner drain cannot acknowledge this capture');
    transport.serverStatus('capture_protocol_drained', fields: _authority);
    await stop;
    expect(socket.drainAcknowledged, isTrue);
    expect(transport.stopCalls, 1);
    expect(socket.captureAuthority?.conversationId, 'conversation-a');
  });

  test('server rejection before ready reports close code without false active state or audio', () async {
    final transport = _Transport();
    final failures = <(String, int?)>[];
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      timeout: const Duration(milliseconds: 100),
      onAdmissionFailure: (reason, code) => failures.add((reason, code)),
    );
    final start = socket.start();
    await _tick();
    transport.serverClose(1008);
    await start;
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await socket.send(<int>[7]);
    expect(socket.state, SocketServiceState.disconnected);
    expect(transport.sent.whereType<List<int>>(), isEmpty);
    expect(failures, [('capture_socket_closed_before_ready', 1008)]);
    expect(socket.lastAdmissionFailure?.reason, EllaCaptureSocketFailureReason.captureSocketClosedBeforeReady);
    expect(socket.lastAdmissionFailure?.closeCode, 1008);
    expect(socket.lastAdmissionFailure?.at.isUtc, isTrue);
    expect(socket.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.closedBeforeReady);
    expect(socket.lastAttempt?.status, EllaCaptureSocketAttemptStatus.failed);
    expect(socket.lastAttempt?.closeCode, 1008);
  });

  test('handshake failure and origin retirement remain distinct without changing capture admission', () async {
    final unavailable = _Transport()..connectResult = false;
    final failed = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', unavailable);
    await failed.start();
    expect(failed.lastAdmissionFailure?.reason, EllaCaptureSocketFailureReason.transportConnectFailed);
    expect(failed.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.transportUnavailable);
    expect(failed.lastAttempt?.at.isUtc, isTrue);
    expect(unavailable.sent, isEmpty);

    var current = true;
    final retired = _Transport()
      ..beforeConnectReturns = () async {
        current = false;
      };
    final stale = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      retired,
      hasOriginAuthority: () => current,
    );
    await stale.start();
    expect(stale.lastAdmissionFailure?.reason, EllaCaptureSocketFailureReason.captureOriginRetired);
    expect(stale.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.originRetired);
    expect(retired.stopCalls, 1);
    expect(retired.sent, isEmpty);
  });

  test('ready and close phases are bounded to one attempt and ignore late ready', () async {
    for (final closeCode in [1008, 1011, 1013, 999, 5000]) {
      final transport = _Transport();
      final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport);
      final start = socket.start();
      await _tick();
      expect(socket.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.transportConnected);
      transport.serverStatus('capture_protocol_ready', fields: _authority);
      await start;
      expect(socket.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.captureReady);
      expect(socket.lastAttempt?.status, EllaCaptureSocketAttemptStatus.ready);
      transport.serverClose(closeCode);
      expect(socket.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.closedAfterReady);
      expect(socket.lastAttempt?.closeCode, closeCode >= 1000 && closeCode <= 4999 ? closeCode : null);
      transport.serverStatus('capture_protocol_ready', fields: _authority);
      expect(socket.lastAttempt?.phase, EllaCaptureSocketAttemptPhase.closedAfterReady);
      await socket.send(<int>[9]);
      expect(transport.sent.whereType<List<int>>(), isEmpty);
    }
  });

  test('invalid ready and missing ready both fail closed', () async {
    for (final invalid in [true, false]) {
      final transport = _Transport();
      final failures = <String>[];
      final socket = EllaCaptureProtocolSocket.withTransport(
        16000,
        BleAudioCodec.pcm16,
        'multi',
        transport,
        timeout: const Duration(milliseconds: 20),
        onAdmissionFailure: (reason, _) => failures.add(reason),
      );
      final start = socket.start();
      await _tick();
      if (invalid) transport.serverStatus('capture_protocol_ready', fields: {'protocol_version': 2});
      await start;
      expect(socket.state, SocketServiceState.disconnected);
      expect(transport.stopCalls, 1);
      expect(failures, hasLength(1));
      await socket.send(<int>[8]);
      expect(transport.sent, isEmpty);
    }
  });

  test('unexpected close after ready removes authority from active state and reports the code', () async {
    final transport = _Transport();
    final failures = <(String, int?)>[];
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      onAdmissionFailure: (reason, code) => failures.add((reason, code)),
    );
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    transport.serverClose(1011);
    await socket.send(<int>[9]);
    expect(socket.state, SocketServiceState.disconnected);
    expect(transport.sent.whereType<List<int>>(), isEmpty);
    expect(failures, [('capture_socket_closed', 1011)]);
  });

  test('rotation ignores stale or mixed-owner ready while preserving the current successor', () async {
    final transport = _Transport();
    final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    transport.serverStatus('capture_protocol_ready', fields: {..._authority, 'conversation_id': 'conversation-b'});
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    transport.serverStatus('capture_protocol_ready', fields: {..._authority, 'owner_token': 'other-owner'});
    expect(socket.captureAuthority?.conversationId, 'conversation-b');
    expect(socket.state, SocketServiceState.connected);
    expect(transport.stopCalls, 0);
    final stop = socket.stop();
    await _tick();
    transport.serverStatus('capture_protocol_drained', fields: {..._authority, 'conversation_id': 'conversation-b'});
    await stop;
    expect(socket.drainAcknowledged, isTrue);
  });

  test('late ready after rejection cannot revive a retired attempt', () async {
    final transport = _Transport();
    var current = true;
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      hasOriginAuthority: () => current,
    );
    final start = socket.start();
    await _tick();
    current = false;
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    expect(socket.state, SocketServiceState.disconnected);
    expect(socket.captureAuthority, isNull);
    await socket.send(<int>[1]);
    expect(transport.sent, isEmpty);
  });

  test('custom-STT final tail reaches backend before exact drain, with no audio after stop begins', () async {
    final primary = _Transport();
    final backend = _Transport();
    var current = true;
    final composite = EllaCaptureCompositeSocket(
      primarySocket: primary,
      secondarySocket: backend,
      hasOriginAuthority: () => current,
    );
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      composite,
      hasOriginAuthority: () => current,
    );
    final start = socket.start();
    await _tick();
    backend.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    primary.onStop = () async {
      primary.onMessage('[{"text":"final provider tail"}]');
      primary.onClosed();
    };
    final firstStop = socket.stop();
    final joinedStop = socket.stop();
    await socket.send(<int>[99]);
    await _tick();
    final control = backend.sent.whereType<String>().map((raw) => jsonDecode(raw) as Map<String, dynamic>).toList();
    expect(control.map((entry) => entry['type']), containsAllInOrder(['suggested_transcript', 'capture_drain']));
    expect(backend.sent.whereType<List<int>>(), isEmpty);
    backend.serverStatus('capture_protocol_drained', fields: _authority);
    await Future.wait([firstStop, joinedStop]);
    expect(socket.drainAcknowledged, isTrue);
    expect(primary.stopCalls, 1);
  });

  test('retired origin drops a pending-stop custom-STT tail before backend drain', () async {
    final primary = _Transport();
    final backend = _Transport();
    var current = true;
    final composite = EllaCaptureCompositeSocket(
      primarySocket: primary,
      secondarySocket: backend,
      hasOriginAuthority: () => current,
    );
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      composite,
      hasOriginAuthority: () => current,
    );
    final start = socket.start();
    await _tick();
    backend.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    primary.onStop = () async {
      current = false;
      primary.onMessage('[{"text":"retired provider tail"}]');
      primary.onClosed();
    };

    await socket.stop();

    final control = backend.sent.whereType<String>().map((raw) => jsonDecode(raw) as Map<String, dynamic>).toList();
    expect(control.map((entry) => entry['type']), isNot(contains('suggested_transcript')));
    expect(control.map((entry) => entry['type']), isNot(contains('capture_drain')));
    expect(socket.drainAcknowledged, isFalse);
    expect(primary.stopCalls, 1);
  });

  test('retired origin drops a delayed custom-STT provider response', () async {
    final primary = _Transport();
    final backend = _Transport();
    var current = true;
    final composite = EllaCaptureCompositeSocket(
      primarySocket: primary,
      secondarySocket: backend,
      hasOriginAuthority: () => current,
    );
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      composite,
      hasOriginAuthority: () => current,
    );
    final start = socket.start();
    await _tick();
    backend.serverStatus('capture_protocol_ready', fields: _authority);
    await start;

    current = false;
    primary.onMessage('[{"text":"late retired response"}]');
    await _tick();

    final control = backend.sent.whereType<String>().map((raw) => jsonDecode(raw) as Map<String, dynamic>).toList();
    expect(control.map((entry) => entry['type']), isNot(contains('suggested_transcript')));
    await socket.stop();
  });

  test('production finalization drains and POSTs the exact ready tuple once', () async {
    final transport = _Transport();
    final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    final verifier = _AccountVerifier('account-a');
    var requests = 0;
    final resultFuture = finalizeEllaCaptureProtocolConversation(
      socket: socket,
      exactAuthority: verifier,
      request: ({
        required conversationId,
        required protocolVersion,
        required generation,
        required ownerToken,
        required transportLost,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        requests++;
        expect(
          (conversationId, protocolVersion, generation, ownerToken),
          ('conversation-a', 2, 'generation-a', 'owner-a'),
        );
        expect(transportLost, isFalse);
        expect(expectedAuthenticatedUid, 'account-a');
        expect(identical(exactAuthority, verifier), isTrue);
        return ella_schema.CreateConversationResponse.fromJson({
          'messages': const [],
          'conversation': {
            'id': conversationId,
            'created_at': '2026-09-29T00:00:00Z',
            'structured': {'title': 'Moment', 'overview': '', 'emoji': '', 'category': 'other'},
          },
        });
      },
    );
    await _tick();
    transport.serverStatus('capture_protocol_drained', fields: _authority);
    final result = await resultFuture;
    expect(result?.conversation?.id, 'conversation-a');
    expect(requests, 1);
  });

  test('lost drain ack uses exact transport-lost reconciliation; retired account drops HTTP result', () async {
    final transport = _Transport();
    final socket = EllaCaptureProtocolSocket.withTransport(
      16000,
      BleAudioCodec.pcm16,
      'multi',
      transport,
      timeout: const Duration(milliseconds: 20),
    );
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    final verifier = _AccountVerifier('account-a');
    var requests = 0;
    final result = await finalizeEllaCaptureProtocolConversation(
      socket: socket,
      exactAuthority: verifier,
      request: ({
        required conversationId,
        required protocolVersion,
        required generation,
        required ownerToken,
        required transportLost,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        requests++;
        expect(transportLost, isTrue);
        expect((conversationId, generation, ownerToken), ('conversation-a', 'generation-a', 'owner-a'));
        verifier.current = false;
        return ella_schema.CreateConversationResponse.fromJson({
          'conversation': {
            'id': conversationId,
            'created_at': '2026-09-29T00:00:00Z',
            'structured': {'title': 'Moment', 'overview': '', 'emoji': '', 'category': 'other'},
          },
        });
      },
    );
    expect(result, isNull);
    expect(requests, 1);
  });

  test('authority change during exact reconciliation returns null rather than rejecting', () async {
    final transport = _Transport();
    final socket = EllaCaptureProtocolSocket.withTransport(16000, BleAudioCodec.pcm16, 'multi', transport);
    final start = socket.start();
    await _tick();
    transport.serverStatus('capture_protocol_ready', fields: _authority);
    await start;
    final verifier = _AccountVerifier('account-a');
    final resultFuture = finalizeEllaCaptureProtocolConversation(
      socket: socket,
      exactAuthority: verifier,
      request: ({
        required conversationId,
        required protocolVersion,
        required generation,
        required ownerToken,
        required transportLost,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        verifier.current = false;
        throw ExactAccountAuthorityChangedException('account changed');
      },
    );
    await _tick();
    transport.serverStatus('capture_protocol_drained', fields: _authority);
    expect(await resultFuture, isNull);
  });
}
