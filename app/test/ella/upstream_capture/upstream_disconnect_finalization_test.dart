import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory dir;
  late EllaUpstreamCaptureHarness h;
  setUp(() async {
    dir = await Directory.systemTemp.createTemp('disconnect_finalize_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
    expect(await h.bind(), isTrue);
    await h.connectPendant();
  });
  tearDown(() async {
    await h.dispose();
    if (dir.existsSync()) dir.deleteSync(recursive: true);
  });
  test('ordinary Disconnect processes before clearing the admitted pendant', () async {
    expect(h.runtime.captureSession?.source, 'omi');
    await h.runtime.disconnectNecklace();
    await h.settle();
    expect(h.processCalls, 1);
    expect(h.provider.recordingDevice, isNull);
    expect(h.disconnectedDevices, [EllaUpstreamCaptureHarness.pendant.id]);
  });
  test('concurrent Disconnect calls join one finalization and one BLE cleanup', () async {
    final a = h.runtime.disconnectNecklace();
    final b = h.runtime.disconnectNecklace();
    expect(identical(a, b), isTrue);
    await Future.wait([a, b]);
    expect(h.processCalls, 1);
    expect(h.disconnectedDevices, hasLength(1));
  });
  test('retired account does not process or disconnect a successor owner', () async {
    h.switchAccount(accountB);
    await h.runtime.disconnectNecklace();
    expect(h.processCalls, 0);
    expect(h.disconnectedDevices, isEmpty);
  });
  test('Disconnect of a connected pendant preserves a current phone producer', () async {
    final phone = await h.startPhone();
    final recording = h.provider.activeRecordingId;
    await h.runtime.disconnectNecklace();
    expect(h.hostApi.nativeRecording, isTrue);
    expect(h.runtime.captureSession?.source, 'phone');
    expect(h.provider.activeRecordingId, recording);
    expect(h.hostApi.lastStartSessionId, phone);
    expect(h.processCalls, 0);
    expect(h.disconnectedDevices, hasLength(1));
  });

  group('protocol 2 original socket', () {
    setUp(() async {
      await h.dispose();
      h = await EllaUpstreamCaptureHarness.boot(tempDir: dir, protocolV2: true, deliveryScope: () => null);
      expect(await h.bind(), isTrue);
      await h.connectPendant();
    });
    test('drain acknowledged before exact original tuple processing', () async {
      final original = h.sockets.single.service;
      h.beforeFinalization = () async {
        expect((original as dynamic).drainAcknowledged, isTrue);
        expect(h.deviceConnection!.openAudioSubscriptions, 0, reason: 'BLE ingress closed before the POST');
        expect(h.provider.recordingDevice?.id, EllaUpstreamCaptureHarness.pendant.id);
      };
      await h.runtime.disconnectNecklace();
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.protocolFinalizations.single.conversationId, 'synthetic-conversation-0');
      expect(h.protocolFinalizations.single.generation, 'synthetic-generation-0');
      expect(h.protocolFinalizations.single.ownerToken, 'synthetic-owner-0');
      expect(h.disconnectedDevices, hasLength(1));
      expect(h.provider.recordingDevice, isNull);
    });
    test('Finish pending then Disconnect joins the same original request', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final finish = h.runtime.finishCaptureSession(h.runtime.captureSession!);
      await entered.future;
      final disconnect = h.runtime.disconnectNecklace();
      release.complete();
      await Future.wait([finish, disconnect]);
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
    });
    test('completed Finish then Disconnect never rePOSTs the same socket', () async {
      await h.runtime.finishCaptureSession(h.runtime.captureSession!);
      await h.runtime.disconnectNecklace();
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
    });
    test('unknown processing failure is surfaced and BLE cleanup still runs once', () async {
      h.finalizationError = StateError('synthetic unknown request outcome');
      await expectLater(h.runtime.disconnectNecklace(), throwsA(isA<StateError>()));
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
      expect(h.provider.recordingDevice, isNull);
      await h.runtime.disconnectNecklace();
      expect(h.protocolFinalizations, hasLength(1));
    });
    test('new Connect waits for original finalization and cleanup', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      final connect = h.runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
      await pumpEventQueue();
      expect(h.connectionAttempts, 0);
      release.complete();
      await disconnect;
      await connect;
      expect(h.connectionAttempts, 1);
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.sockets.length, 2);
      expect(h.disconnectedDevices, hasLength(1));
    });
    test('held processing refuses further pendant frames without a mute policy write', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      final frames = h.audioFramesSent;
      expect(h.deviceConnection!.openAudioSubscriptions, 0);
      h.deviceConnection!.emitAudio();
      await pumpEventQueue();
      expect(h.audioFramesSent, frames);
      release.complete();
      await disconnect;
    });
    test('raw Finish and Disconnect share one pinned socket request', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final finish = h.runtime.finishConversation();
      await entered.future;
      final disconnect = h.runtime.disconnectNecklace();
      release.complete();
      await Future.wait([finish, disconnect]);
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
    });
    test('queued phone replacement cannot be stopped by the old Disconnect', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final originalId = h.provider.activeRecordingId;
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      final phone = h.startPhone();
      await pumpEventQueue();
      release.complete();
      await disconnect;
      await phone;
      expect(h.runtime.captureSession?.source, 'phone');
      expect(h.provider.activeRecordingId, isNot(originalId));
      expect(h.hostApi.nativeRecording, isTrue);
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, isEmpty);
    });
    test('late socket interruption cannot rearm processing during Disconnect', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      h.socket!.emitClose();
      release.complete();
      await disconnect;
      await h.settle();
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
      expect(h.provider.recordingDevice, isNull);
      expect(h.provider.keepAliveScheduledForTesting, isFalse);
    });
    test('a Connect queued behind failed finalization does not start automatically', () async {
      final entered = Completer<void>(), release = Completer<void>();
      h.beforeFinalization = () async {
        entered.complete();
        await release.future;
      };
      h.finalizationError = StateError('synthetic unknown outcome');
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      final connect = h.runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
      final rejectedDisconnect = expectLater(disconnect, throwsA(isA<StateError>()));
      final rejectedConnect = expectLater(connect, throwsA(isA<StateError>()));
      release.complete();
      await Future.wait([rejectedDisconnect, rejectedConnect]);
      expect(h.connectionAttempts, 0);
      expect(h.protocolFinalizations, hasLength(1));
      expect(h.disconnectedDevices, hasLength(1));
    });
    test('account retirement during held drain sends no processing request', () async {
      // A fresh socket consumes this drain hold at its creation.
      await h.runtime.disconnectNecklace();
      h.protocolFinalizations.clear();
      h.disconnectedDevices.clear();
      final entered = Completer<void>(), release = Completer<void>();
      h.holdNextDrain = release;
      h.nextDrainEntered = entered;
      await h.connectPendant();
      final disconnect = h.runtime.disconnectNecklace();
      await entered.future;
      h.switchAccount(accountB);
      release.complete();
      await disconnect;
      expect(h.protocolFinalizations, isEmpty);
      expect(h.disconnectedDevices, isEmpty);
    });
  });
}
