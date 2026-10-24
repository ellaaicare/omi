import 'dart:io';
import 'dart:async';
import 'dart:convert';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/capture/scenarios/native_event_vector.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import '../services/whisper_delivery_client_test.dart' as delivery;
import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory dir;
  late EllaUpstreamCaptureHarness h;
  late delivery.Harness d;
  late ValueNotifier<int> changes;
  setUp(() async {
    dir = await Directory.systemTemp.createTemp('ella_whisper_capture_');
    changes = ValueNotifier(0);
    d = delivery.Harness();
    d.authority = delivery.Authority(accountA);
    final original = d.client;
    d.client = WhisperDeliveryClient(
      transport: original.transport,
      media: d.media,
      read: original.read,
      write: original.write,
      authorityProvider: () => d.authority,
      newId: d.id,
      lifetimeIdentity: (_) => d.lifetime,
      onChanged: () => changes.value++,
    );
    h = await EllaUpstreamCaptureHarness.boot(
        tempDir: dir, protocolV2: true, deliveryChanges: changes, deliveryScope: () => d.client.scope);
    expect(await h.bind(), isTrue);
  });
  tearDown(() async {
    await h.dispose();
    changes.dispose();
    if (dir.existsSync()) dir.deleteSync(recursive: true);
  });
  test('capture begun Off binds current delivery tuple after On without restarting recorder', () async {
    final link = await h.connectPendant();
    final session = h.runtime.captureSession!;
    expect(h.socketDeliveryQueries.single, isEmpty);
    await d.start();
    await h.settle();
    await h.runtime.pendingDeliveryRefresh;
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    final trace = h.runtime.captureDiagnostics.protocolTrace.map((e) => e.toJson()).toList();
    expect(trace.any((e) => e["event"] == "deliveryChanged" && e["delivery_opt_in"] == true), isTrue);
    expect(trace.any((e) => e["event"] == "rebindEnd"), isTrue);
    expect(h.sockets.length, 2);
    expect(h.sockets.first.transport.closeCalls, 1);
    expect(link.audioSubscriptionsOpened, 1);
    expect(session.matches(h.runtime.captureSession), isTrue);
  });
  test('stop/resume scope rotation replaces active socket URL', () async {
    await d.start();
    await h.connectPendant();
    final old = Map.of(d.client.captureQuery);
    await d.client.stop();
    await h.settle();
    await h.runtime.pendingDeliveryRefresh;
    expect(d.client.captureQuery, isNot(old));
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(h.sockets.first.transport.closeCalls, 1);
  });
  test('production v4 URL contains current tuple and removes stale reserved parameters', () async {
    await d.start();
    final socket = createEllaCaptureProtocolSocket(
        codec: BleAudioCodec.pcm16,
        sampleRate: 16000,
        language: 'multi',
        deliveryQuery: h.runtime.captureDeliveryQuery);
    final uri = Uri.parse((socket.socket as PureSocket).url);
    expect(uri.path, '/v4/listen');
    for (final entry in d.client.captureQuery.entries) {
      expect(uri.queryParameters[entry.key], entry.value);
    }
    final off = ellaCaptureProtocolUri(uri, deliveryQuery: const {});
    expect(off.queryParameters.keys.any((k) => k.startsWith('delivery_')), isFalse);
    expect(off.queryParameters['capture_protocol'], '2');
  });

  test('rapid Off/On while draining coalesces to newest scope with one replacement', () async {
    final drain = Completer<void>();
    final entered = Completer<void>();
    h.holdNextDrain = drain;
    h.nextDrainEntered = entered;
    final link = await h.connectPendant();
    final old = h.sockets.single;
    await d.start();
    await entered.future;
    final before = old.transport.sentBinary.length;
    link.emitAudio();
    await pumpEventQueue();
    expect(old.transport.sentBinary.length, before, reason: 'stale binding fenced before async refresh');
    for (var i = 0; i < 4; i++) {
      await d.client.stop(resume: false);
      await d.client.start(WhisperPresentation.audio, explicitOn: true);
    }
    expect(h.sockets.length, 1, reason: 'predecessor must close before another socket opens');
    drain.complete();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.sockets.length, 2);
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(old.transport.closeCalls, 1);
    expect(link.audioSubscriptionsOpened, 1);
    expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), hasLength(1));
  });

  test('scope rotation during pending handshake retires late ready and opens latest tuple', () async {
    await h.connectPendant();
    final hold = Completer<void>();
    final entered = Completer<void>();
    h.holdNextSocketStart = hold;
    h.nextSocketStartEntered = entered;
    await d.start();
    await entered.future;
    final stale = h.sockets.last;
    await d.client.stop();
    hold.complete();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(stale.service.state, SocketServiceState.disconnected);
    expect(stale.transport.closeCalls, 1);
    final active = h.sockets.last.service as EllaCaptureProtocolSocket;
    final authority = active.captureAuthority;
    stale.transport.onMessage(jsonEncode({
      'type': 'service_status',
      'status': 'capture_protocol_ready',
      'protocol_version': 2,
      'conversation_id': 'late-old',
      'generation': 'old',
      'owner_token': 'old'
    }));
    stale.transport.onError(StateError('late synthetic error'), StackTrace.current);
    stale.transport.onClosed();
    expect(active.captureAuthority, same(authority));
    expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), hasLength(1));
  });

  test('disconnect/reconnect uses latest scope under upstream recovery ownership', () async {
    await d.start();
    final link = await h.connectPendant();
    h.setConnected(false);
    h.socket!.emitClose(1011);
    await h.settle();
    await d.client.stop();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), isEmpty);
    h.setConnected(true);
    await h.elapse(const Duration(seconds: 30));
    expect(h.sockets.last.service.state, SocketServiceState.connected);
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(link.audioSubscriptionsOpened, 1);
  });

  for (final retirement in ['logout', 'account switch', 'same account replacement']) {
    test('$retirement during rebind cannot revive old capture or delivery URL', () async {
      final link = await h.connectPendant();
      final hold = Completer<void>();
      final entered = Completer<void>();
      h.holdNextSocketStart = hold;
      h.nextSocketStartEntered = entered;
      await d.start();
      await entered.future;
      final stale = h.sockets.last;
      d.authority.current = false;
      final release = h.runtime.releaseAccount();
      if (retirement == 'account switch') h.switchAccount(accountB);
      hold.complete();
      await release;
      await h.runtime.pendingDeliveryRefresh;
      await h.settle();
      final sent = h.audioFramesSent;
      link.emitAudio();
      await h.settle();
      expect(h.audioFramesSent, sent);
      expect(stale.service.state, SocketServiceState.disconnected);
      expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), isEmpty);
      if (retirement != 'logout') {
        grantEllaConsent(h.ellaPreferences, retirement == 'account switch' ? accountB : accountA);
        expect(await h.bind(), isTrue);
        await h.connectPendant();
        expect(h.socketDeliveryQueries.last, isEmpty, reason: 'no cross-lifetime delivery reuse');
        expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), hasLength(1));
      }
    });
  }

  test('another account ready scope never opts this capture into delivery', () async {
    d.authority = delivery.Authority(accountB);
    await d.start();
    await h.connectPendant();
    expect(d.client.captureQuery, isNotEmpty);
    expect(h.socketDeliveryQueries.single, isEmpty);
  });

  test('failed native Whisper start leaves existing capture running without delivery', () async {
    final link = await h.connectPendant();
    final gate = Completer<void>();
    d.media.activation = gate;
    final starting = d.start();
    final failure = expectLater(starting, throwsStateError);
    await pumpEventQueue();
    expect(d.client.scope, isNull);
    gate.completeError(StateError('synthetic native refusal'));
    await failure;
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(d.client.scope, isNull);
    expect(h.socketDeliveryQueries.single, isEmpty);
    expect(link.audioSubscriptionsOpened, 1);
    link.emitAudio();
    await h.settle();
    expect(h.audioFramesSent, greaterThan(0));
  });

  test('revoked delivery authority drops opt-in without stopping capture', () async {
    await d.start();
    final link = await h.connectPendant();
    d.authority.current = false;
    await d.client.retireAuthority();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.socketDeliveryQueries.last, isEmpty);
    expect(link.audioSubscriptionsOpened, 1);
    link.emitAudio();
    await h.settle();
    expect(h.sockets.last.transport.sentBinary, isNotEmpty);
  });

  test('phone audio during rebind is retained byte-for-byte without native restart', () async {
    final drain = Completer<void>();
    final entered = Completer<void>();
    h.holdNextDrain = drain;
    h.nextDrainEntered = entered;
    final native = await h.startPhone();
    final session = h.runtime.captureSession!;
    h.injectPhoneFrames(100, sessionId: native, firstFrameIndex: 0);
    await h.settle();
    await d.start();
    await entered.future;
    final before = h.audioFramesSent;
    h.injectPhoneFrames(200, sessionId: native, firstFrameIndex: 100);
    await pumpEventQueue();
    expect(h.audioFramesSent, before);
    expect(h.hostApi.nativeRecording, isTrue);
    drain.complete();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    h.injectPhoneFrames(100, sessionId: native, firstFrameIndex: 300);
    await h.settle();
    expect(h.hostApi.startCalls, 1);
    expect(h.hostApi.stopCalls, 0);
    expect(session.matches(h.runtime.captureSession), isTrue);
    h.setConnected(false);
    h.socket!.emitClose(1011);
    await h.settle();
    await h.provider.stopStreamRecording();
    await h.settle();
    final frames = <List<int>>[];
    for (final w in await h.wal.syncs.phone.getAllWals()) {
      final bytes = File((await Wal.getFilePath(w.filePath))!).readAsBytesSync();
      var offset = 0;
      while (offset + 4 <= bytes.length) {
        final length = ByteData.sublistView(bytes, offset, offset + 4).getUint32(0, Endian.little);
        offset += 4;
        frames.add(bytes.sublist(offset, offset + length));
        offset += length;
      }
    }
    expect(frames, List.generate(400, (i) => NativeEventVector.synthesizePcmFrame(i)));
  });
  test('failed resume clears stale scope and retains only capture without opt-in', () async {
    await d.start();
    final link = await h.connectPendant();
    d.corrupt = (path, response) {
      if (path == 'stop') response['resume'] = {'malformed': true};
    };
    await expectLater(d.client.stop(), throwsStateError);
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(d.client.scope, isNull);
    expect(h.socketDeliveryQueries.last, isEmpty);
    expect(link.audioSubscriptionsOpened, 1);
    link.emitAudio();
    await h.settle();
    expect(h.sockets.last.transport.sentBinary, isNotEmpty);
  });

  test('same UID authentication lifetime restart gets fresh epoch and capture tuple', () async {
    await d.start();
    await h.connectPendant();
    final old = d.client.captureQuery;
    await d.client.retireAuthority();
    await h.runtime.pendingDeliveryRefresh;
    d.lifetime = 'new-authentication-lifetime';
    await d.client.start(WhisperPresentation.audio, explicitOn: true);
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(d.client.captureQuery['delivery_client_epoch'], isNot(old['delivery_client_epoch']));
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), hasLength(1));
  });

  test('unchanged presentation/poll notifications do not churn capture socket', () async {
    await d.start();
    await h.connectPendant();
    final before = h.sockets.length;
    for (var i = 0; i < 5; i++) {
      changes.value++;
      await d.client.poll();
    }
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.sockets.length, before);
  });
  for (final invalidation in ['account', 'scope']) {
    test('$invalidation change while auth headers pending blocks obsolete handshake', () async {
      var origin = true, binding = true;
      final gate = Completer<Map<String, String>>();
      final headers = ellaCaptureSocketHeaders(() => gate.future,
          hasOriginAuthority: () => origin, hasCurrentDeliveryBinding: () => binding);
      final request = headers();
      final refused = expectLater(request, throwsStateError);
      if (invalidation == 'account') {
        origin = false;
      } else {
        binding = false;
      }
      gate.complete(const {'Authorization': 'synthetic-test-header'});
      await refused;
    });
  }
  test('retired origin refuses even to request headers', () async {
    var calls = 0;
    final headers = ellaCaptureSocketHeaders(() async {
      calls++;
      return {};
    }, hasOriginAuthority: () => false, hasCurrentDeliveryBinding: () => true);
    await expectLater(headers(), throwsStateError);
    expect(calls, 0);
  });
  test('scope changes while capture paused preserve paused owner and fresh tuple on resume', () async {
    await d.start();
    await h.connectPendant();
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    await d.client.stop();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.runtime.isCapturePaused(session), isTrue);
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    await h.runtime.setCapturePaused(session, paused: false);
    expect(session.matches(h.runtime.captureSession), isTrue);
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    final trace = h.runtime.captureDiagnostics.protocolTrace.map((e) => e.toJson()).toList();
    expect(trace.any((e) => e["event"] == "pauseCommitted" && e["paused"] == true), isTrue);
    expect(trace.any((e) => e["event"] == "resumeCommitted" && e["paused"] == false), isTrue);
  });
  test('scope change after transport connects retires pending ready without false failure', () async {
    await h.connectPendant();
    final hold = Completer<void>();
    final entered = Completer<void>();
    h.holdNextReady = hold;
    h.nextReadyEntered = entered;
    await d.start();
    await entered.future;
    final stale = h.sockets.last.service as EllaCaptureProtocolSocket;
    expect(stale.socket.status, PureSocketStatus.connected);
    expect(stale.captureAuthority, isNull);
    await d.client.stop();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    final active = h.sockets.last.service as EllaCaptureProtocolSocket;
    final authority = active.captureAuthority;
    hold.complete();
    await pumpEventQueue();
    expect(stale.lastAdmissionFailure, isNull, reason: 'intentional rebind is not a backend admission failure');
    expect(stale.state, SocketServiceState.disconnected);
    expect(active.captureAuthority, same(authority));
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
  });
  for (final stage in ['drain', 'auth', 'ready']) {
    test('terminal disposal during $stage prevents replacement without releasing account first', () async {
      final hold = Completer<void>();
      final entered = Completer<void>();
      if (stage == 'drain') {
        h.holdNextDrain = hold;
        h.nextDrainEntered = entered;
      }
      final link = await h.connectPendant();
      if (stage == 'auth') {
        h.holdNextAuth = hold;
        h.nextAuthEntered = entered;
      }
      if (stage == 'ready') {
        h.holdNextReady = hold;
        h.nextReadyEntered = entered;
      }
      await d.start();
      await entered.future;
      expect(h.authority.hasCurrentAuthority, isTrue);
      final created = h.sockets.length;
      final socket = h.sockets.last.service as EllaCaptureProtocolSocket;
      h.runtime.dispose();
      expect(h.authority.hasCurrentAuthority, isTrue, reason: 'disposal fence cannot be masked by account retirement');
      expect(socket.hasOriginAuthority, isFalse);
      hold.complete();
      await h.runtime.pendingDeliveryRefresh;
      await pumpEventQueue();
      expect(h.sockets.length, created, reason: 'no socket may be constructed after terminal disposal');
      expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), isEmpty);
      final sent = h.audioFramesSent;
      link.emitAudio();
      await pumpEventQueue();
      expect(h.audioFramesSent, sent);
      socket.onMessage(jsonEncode({
        'type': 'service_status',
        'status': 'capture_protocol_ready',
        'protocol_version': 2,
        'conversation_id': 'late-disposed',
        'generation': 'old',
        'owner_token': 'old'
      }));
      expect(socket.state, SocketServiceState.disconnected);
      await d.client.stop();
      await pumpEventQueue();
      expect(h.sockets.length, created);
      expect(await h.runtime.bindAccount(accountA), isFalse);
      await expectLater(h.runtime.ensureBooted(), throwsStateError);
      h.runtime.dispose();
    });
  }
  test('scope rotation during pending auth retires attempt and binds latest tuple', () async {
    await h.connectPendant();
    final hold = Completer<void>(), entered = Completer<void>();
    h.holdNextAuth = hold;
    h.nextAuthEntered = entered;
    await d.start();
    await entered.future;
    final stale = h.sockets.last.service as EllaCaptureProtocolSocket;
    await d.client.stop();
    hold.complete();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.socketDeliveryQueries.last, d.client.captureQuery);
    expect(stale.state, SocketServiceState.disconnected);
    expect(stale.lastAdmissionFailure, isNull);
    expect(h.sockets.where((s) => s.transport.status == PureSocketStatus.connected), hasLength(1));
  });
}
