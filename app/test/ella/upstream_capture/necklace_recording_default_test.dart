import 'dart:async';
import 'dart:io';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/backend/schema/bt_device/bt_device.dart' as remembered;
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/upstream_capture/services/capture/capture_coordinator.dart';
import 'package:omi/upstream_capture/services/devices.dart' as devices;
import 'support/ella_upstream_capture_harness.dart';
import '../../upstream_capture/support/capture/scripted_device_connection.dart';

void main() {
  late Directory dir;
  late EllaUpstreamCaptureHarness h;
  setUp(() async {
    dir = await Directory.systemTemp.createTemp('default_necklace_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
    expect(await h.bind(), isTrue);
  });
  tearDown(() async {
    upstream.SharedPreferencesUtil.capturePolicyBridgeForTesting = null;
    await h.dispose();
    if (dir.existsSync()) dir.deleteSync(recursive: true);
  });

  Future<void> connect() async {
    await h.connectPendant();
    await h.runtime.connectNecklace(
      accountA,
      EllaUpstreamCaptureHarness.pendant,
    );
    h.ellaPreferences.btDevice = remembered.BtDevice.fromJson(
      EllaUpstreamCaptureHarness.pendant.toJson(),
    );
    await h.ellaPreferences.btDeviceOwnerBindingSet(accountA);
    await h.settle();
  }

  test(
    'authorized Connect clears the persisted pause without a Play action',
    () async {
      await h.connectPendant();
      await h.provider.pauseDeviceRecording();
      expect(h.provider.isPaused, isTrue);
      await h.runtime.connectNecklace(
        accountA,
        EllaUpstreamCaptureHarness.pendant,
      );
      await h.settle();
      expect(h.provider.isPaused, isFalse);
      expect(h.provider.recordingState, RecordingState.deviceRecord);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
      h.deviceConnection!.emitAudio();
      await h.settle();
      expect(h.audioFramesSent, greaterThan(0));
    },
  );

  test(
    'reopening an active necklace keeps recording/window/socket and one producer',
    () async {
      await connect();
      final id = h.provider.activeRecordingId;
      final window = h.provider.captureWindowGeneration;
      final sockets = h.sockets.length;
      await h.runtime.requestDefaultNecklaceRecordingForTesting();
      await h.settle();
      expect(h.provider.activeRecordingId, id);
      expect(h.provider.captureWindowGeneration, window);
      expect(h.sockets.length, sockets);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
    },
  );

  test(
    'repeated default-start events do not reopen a live audio consumer',
    () async {
      await connect();
      final opened = h.deviceConnection!.audioSubscriptionsOpened;
      await Future.wait(
        List.generate(
          4,
          (_) => h.provider.ensureDeviceRecordingDefault(
            device: EllaUpstreamCaptureHarness.pendant,
            isCurrent: () => h.authority.hasCurrentAuthority,
          ),
        ),
      );
      await h.settle();
      expect(h.deviceConnection!.audioSubscriptionsOpened, opened);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
    },
  );

  test(
    'backgrounding does not stop, pause, finalize or replace active capture',
    () async {
      await connect();
      final id = h.provider.activeRecordingId;
      final processes = h.processCalls;
      h.runtime.didChangeAppLifecycleState(AppLifecycleState.paused);
      h.deviceConnection!.emitAudio();
      await h.settle();
      expect(h.provider.activeRecordingId, id);
      expect(h.provider.isPaused, isFalse);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
      expect(h.processCalls, processes);
    },
  );

  test(
    'deliberate Stop stays stopped until the next explicit lifecycle wake',
    () async {
      await connect();
      h.deviceConnection!.emitAudio();
      await h.settle();
      await h.runtime.disconnectNecklace();
      await h.settle();
      final attempts = h.connectionAttempts;
      await h.settle();
      expect(h.connectionAttempts, attempts);
      expect(h.deviceConnection!.openAudioSubscriptions, 0);
      await h.runtime.requestDefaultNecklaceRecordingForTesting();
      await h.settle();
      expect(h.connectionAttempts, greaterThan(attempts));
      expect(h.provider.recordingState, RecordingState.deviceRecord);
      expect(h.provider.isPaused, isFalse);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
    },
  );

  test(
    'a reconnect callback resumes only the remembered owner-selected necklace',
    () async {
      await connect();
      await h.provider.pauseDeviceRecording();
      await h.settle();
      h.runtime.onDeviceConnectionStateChanged(
        'other-device',
        devices.DeviceConnectionState.connected,
      );
      await h.settle();
      expect(h.provider.isPaused, isTrue);
      h.runtime.onDeviceConnectionStateChanged(
        EllaUpstreamCaptureHarness.pendant.id,
        devices.DeviceConnectionState.connected,
      );
      await h.settle();
      expect(h.provider.isPaused, isFalse);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
    },
  );

  test(
    'an account switch cannot auto-resume the previous account necklace',
    () async {
      await connect();
      await h.provider.pauseDeviceRecording();
      await h.settle();
      final attempts = h.connectionAttempts;
      h.switchAccount(accountB);
      await h.runtime.requestDefaultNecklaceRecordingForTesting();
      await h.settle();
      expect(h.connectionAttempts, attempts);
      final sent = h.audioFramesSent;
      h.deviceConnection!.emitAudio();
      await h.settle();
      expect(h.audioFramesSent, sent);
    },
  );

  test(
    'a remembered device without the exact owner binding is never auto-selected',
    () async {
      await connect();
      await h.provider.pauseDeviceRecording();
      await h.settle();
      await h.ellaPreferences.btDeviceOwnerBindingSet(accountB);
      final attempts = h.connectionAttempts;
      await h.runtime.requestDefaultNecklaceRecordingForTesting();
      await h.settle();
      expect(h.connectionAttempts, attempts);
      expect(h.provider.isPaused, isTrue);
    },
  );

  test(
    'a retired start guard does not clear mute or create a producer',
    () async {
      await h.connectPendant();
      await h.provider.pauseDeviceRecording();
      final opened = h.deviceConnection!.audioSubscriptionsOpened;
      await expectLater(
        h.provider.ensureDeviceRecordingDefault(
          device: EllaUpstreamCaptureHarness.pendant,
          isCurrent: () => false,
        ),
        throwsStateError,
      );
      expect(h.provider.isPaused, isTrue);
      expect(h.deviceConnection!.audioSubscriptionsOpened, opened);
    },
  );

  test(
    'same-owner generation retirement during unmute stops later start effects',
    () async {
      await connect();
      await h.provider.pauseDeviceRecording();
      await h.settle();
      final oldEpoch = h.authority.bindingEpoch;
      final begun = Completer<void>(), release = Completer<void>();
      upstream.SharedPreferencesUtil.capturePolicyBridgeForTesting =
          (method, arguments) async {
            if (method == 'setMuted' &&
                arguments['muted'] == false &&
                !begun.isCompleted) {
              begun.complete();
              await release.future;
            }
            return null;
          };
      final pending = h.provider.ensureDeviceRecordingDefault(
        device: EllaUpstreamCaptureHarness.pendant,
        isCurrent: () =>
            h.authority.hasCurrentAuthority &&
            h.authority.bindingEpoch == oldEpoch,
      );
      await begun.future;
      h.authority.release();
      h.authority.bind(accountA);
      release.complete();
      await expectLater(pending, throwsStateError);
      await h.settle();
      expect(h.deviceConnection!.openAudioSubscriptions, 0);
    },
  );

  test(
    'Create memory still finalizes once while the default necklace continues',
    () async {
      await connect();
      for (var i = 0; i < 3; i++) {
        h.deviceConnection!.emitAudio();
      }
      await h.settle();
      final session = h.runtime.captureSession!;
      final before = h.audioFramesSent;
      await h.runtime.finishCaptureSession(session, stopDeviceIngress: false);
      await h.settle();
      await h.runtime.finishCaptureSession(session, stopDeviceIngress: false);
      await h.settle();
      h.deviceConnection!.emitAudio();
      await h.settle();
      expect(h.processCalls, 1);
      expect(h.audioFramesSent, greaterThan(before));
      expect(h.disconnectedDevices, isEmpty);
      expect(h.deviceConnection!.openAudioSubscriptions, 1);
    },
  );
  test('cold reopen exercises the real upstream force-dependent connection service', () async {
    await connect();
    await h.runtime.disconnectNecklace();
    final link = _ServiceLink();
    h.deviceConnection = link;
    final service = devices.DeviceService(connectionBuilder: (_) => link);
    final forces = <bool>[];
    h.ensureConnectionOverride = (id, {required force}) async {
      forces.add(force);
      return service.ensureConnection(id, force: force);
    };
    // This is the production contract that the old callback-only harness missed.
    expect(await service.ensureConnection(EllaUpstreamCaptureHarness.pendant.id), isNull);
    await h.runtime.requestDefaultNecklaceRecordingForTesting();
    await h.settle();
    expect(forces, [true]);
    expect(link.connects, 1);
    expect(h.provider.isPaused, isFalse);
    expect(link.openAudioSubscriptions, 1);
    expect(await service.ensureConnection(EllaUpstreamCaptureHarness.pendant.id, force: true), same(link));
    expect(link.connects, 1);
  });


  test('foreground reconnect retains the live session while rebinding a replaced transport', () async {
    await connect();
    final id = h.provider.activeRecordingId;
    final window = h.provider.captureWindowGeneration;
    final old = h.deviceConnection!;
    final replacement = _ServiceLink();
    final service = devices.DeviceService(connectionBuilder: (_) => replacement);
    h.ensureConnectionOverride = (deviceId, {required force}) async {
      final connection = await service.ensureConnection(deviceId, force: force);
      h.deviceConnection = replacement;
      return connection;
    };
    await h.runtime.requestDefaultNecklaceRecordingForTesting();
    await h.settle();
    expect(replacement.connects, 1);
    expect(h.provider.activeRecordingId, id);
    expect(h.provider.captureWindowGeneration, window);
    expect(old.openAudioSubscriptions, 0);
    expect(replacement.openAudioSubscriptions, 1);
    final sent = h.audioFramesSent;
    replacement.emitAudio(); await h.settle();
    expect(h.audioFramesSent, greaterThan(sent));
    await h.runtime.requestDefaultNecklaceRecordingForTesting();
    expect(replacement.connects, 1);
    expect(replacement.audioSubscriptionsOpened, 1);
  });

  test('explicit Connect may establish its own initial authorized binding', () async {
    await h.runtime.releaseAccount();
    await h.settle();
    expect(h.authority.isBound, isFalse);
    h.deviceConnection = _ServiceLink();
    final result = await h.runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
    await h.settle();
    expect(result.name, 'started');
    expect(h.authority.boundUid, accountA);
    expect(h.provider.isPaused, isFalse);
    expect(h.deviceConnection!.openAudioSubscriptions, 1);
  });

  test('an automatic wake waiting for disconnect cannot adopt a rebound same UID epoch', () async {
    await connect();
    final entered = Completer<void>(), release = Completer<void>();
    h.beforeDeviceDisconnect = () async { entered.complete(); await release.future; };
    final stop = h.runtime.disconnectNecklace();
    await entered.future;
    final before = h.connectionAttempts;
    final wake = h.runtime.requestDefaultNecklaceRecordingForTesting();
    h.authority.release();
    h.authority.bind(accountA);
    release.complete();
    await Future.wait([stop, wake]);
    await h.settle();
    expect(h.connectionAttempts, before);
    expect(h.deviceConnection!.openAudioSubscriptions, 0);
  });

  test('an automatic wake waiting for disconnect cannot follow a changed selection', () async {
    await connect();
    final entered = Completer<void>(), release = Completer<void>();
    h.beforeDeviceDisconnect = () async { entered.complete(); await release.future; };
    final stop = h.runtime.disconnectNecklace();
    await entered.future;
    final before = h.connectionAttempts;
    final wake = h.runtime.requestDefaultNecklaceRecordingForTesting();
    h.ellaPreferences.btDevice = remembered.BtDevice.fromJson({
      ...EllaUpstreamCaptureHarness.pendant.toJson(), 'id': 'another-selected-device',
    });
    release.complete();
    await Future.wait([stop, wake]);
    await h.settle();
    expect(h.connectionAttempts, before);
    expect(h.deviceConnection!.openAudioSubscriptions, 0);
  });

  test('connected notification during Connect is consumed and cannot later undo Pause', () async {
    await connect();
    final entered = Completer<void>(), release = Completer<void>();
    h.ensureConnectionOverride = (_, {required force}) async {
      entered.complete();
      await release.future;
      return h.deviceConnection;
    };
    final connecting = h.runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
    await entered.future;
    h.runtime.onDeviceConnectionStateChanged(EllaUpstreamCaptureHarness.pendant.id,
      devices.DeviceConnectionState.connected);
    release.complete(); await connecting;
    h.ensureConnectionOverride = null;
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    await h.settle();
    // Ordinary state/finalization notifications are not lifecycle wake intents.
    h.runtime.finishPending.value = true;
    h.runtime.finishPending.value = false;
    await h.settle();
    expect(h.provider.isPaused, isTrue);
    await h.runtime.requestDefaultNecklaceRecordingForTesting();
    await h.settle();
    expect(h.provider.isPaused, isFalse);
    expect(h.deviceConnection!.openAudioSubscriptions, 1);
  });

}


class _ServiceLink extends ScriptedDeviceConnection {
  int connects = 0;
  devices.DeviceConnectionState _status = devices.DeviceConnectionState.disconnected;
  @override devices.DeviceConnectionState get status => _status;
  @override Future<void> connect({void Function(String, devices.DeviceConnectionState)? onConnectionStateChanged}) async {
    connects++;
    _status = devices.DeviceConnectionState.connected;
    onConnectionStateChanged?.call(EllaUpstreamCaptureHarness.pendant.id, _status);
  }
}
