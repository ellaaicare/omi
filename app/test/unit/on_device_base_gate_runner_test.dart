import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/services/on_device/on_device_engine.dart';
import 'package:omi/services/on_device/on_device_runner.dart';
import 'package:omi/services/on_device/on_device_settings.dart';
import 'package:omi/services/on_device/on_device_transcription_controller.dart';
import 'package:omi/services/wals/local_wal_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/utils/analytics/analytics_manager.dart';
import '../support/on_device_fakes.dart';

// Ella Base v4.7, gate -> setting -> queue. Synthetic silence only.
//  - Gate open (Base) with default settings starts no work.
//  - The user's own opt-in (setting on + Transcribe now) behaves exactly as before: the whole queue.
//  - The test-only exact-WAL selector limits a run to named files and is cleared after one pass.

class _Listener implements IWalSyncListener {
  @override
  void onWalUpdated() {}
  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) {}
}

Wal _wal(int timerStart) => Wal(
    timerStart: timerStart,
    codec: BleAudioCodec.pcm16,
    seconds: 10,
    status: WalStatus.miss,
    storage: WalStorage.disk,
    filePath: 'synthetic_$timerStart.bin',
    onDeviceOnly: true);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late Directory directory;
  late List<Wal> wals;
  late LocalWalSyncImpl sync;
  late List<ServerConversation> rows;
  var calls = 0;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('omi-base-gate-test-');
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(const MethodChannel('plugins.flutter.io/path_provider'), (_) async => directory.path);
    wals = [_wal(1700000100), _wal(1700000200), _wal(1700000300)];
    for (final wal in wals) {
      final builder = BytesBuilder();
      for (var i = 0; i < wal.seconds * 100; i++) {
        builder.add((ByteData(4)..setUint32(0, 320, Endian.little)).buffer.asUint8List());
        builder.add(Uint8List(320));
      }
      File('${directory.path}/${wal.filePath}').writeAsBytesSync(builder.takeBytes());
    }
    sync = LocalWalSyncImpl(_Listener(), persistWals: (_) async {});
    sync.testWals = wals;
    rows = [];
    calls = 0;
  });
  tearDown(() async {
    AnalyticsManager.resetForTesting();
    await directory.delete(recursive: true);
  });

  OnDeviceRunner runner(OnDeviceTranscriptionController controller) => OnDeviceRunner(
      sync: sync,
      controller: controller,
      publish: rows.add,
      transcribeChunk: (bytes, language) async {
        calls++;
        return OnDeviceBatchResult(engine: 'synthetic', text: 'Synthetic.', words: [OnDeviceWord('Synthetic.', 0, 1)]);
      });

  void untouched(Iterable<Wal> list) {
    for (final wal in list) {
      expect(wal.status, WalStatus.miss);
      expect(wal.onDeviceConversation, isNull);
      expect(File('${directory.path}/${wal.filePath}').existsSync(), isTrue, reason: 'audio kept');
    }
  }

  test('gate open, default settings: active, nothing kept, nothing transcribed', () async {
    await SharedPreferencesUtil().saveBool('onDeviceOverflowConfigured', true); // stale flags are rewritten off
    await SharedPreferencesUtil().saveBool('onDeviceOverflowActive', true);
    final controller = OnDeviceTranscriptionController(
        model: FakeOnDeviceModel(), power: FakePhonePowerSource(), readGate: () async => true);
    await controller.ensureLoaded();
    expect(controller.isActive, isTrue);
    expect(controller.settings.enabled, isFalse, reason: 'disabled by default, unchanged');
    expect(controller.shouldKeepAudioLocal, isFalse);
    expect(SharedPreferencesUtil().getBool('onDeviceOverflowConfigured'), isFalse);
    expect(SharedPreferencesUtil().getBool('onDeviceOverflowActive'), isFalse);
    final r = runner(controller);
    await r.run();
    await r.run(transcribeNow: true);
    expect(calls, 0);
    expect(rows, isEmpty);
    expect(r.pendingSeconds, 30, reason: 'only counted');
    untouched(wals);
    r.dispose();
    controller.dispose();
  });

  test('user opt-in is unchanged: setting on + Transcribe now handles the whole queue', () async {
    final controller = OnDeviceTranscriptionController(model: FakeOnDeviceModel(), power: FakePhonePowerSource());
    controller.debugActivate(settings: const OnDeviceSettings(enabled: true, keepEverythingOnDevice: true));
    final r = runner(controller);
    expect(r.debugOnlyWalFiles, isNull);
    await r.run(transcribeNow: true);
    expect(calls, 3, reason: 'every pending WAL, as before v4.7');
    expect(rows, isNotEmpty);
    r.dispose();
    controller.dispose();
  });

  test('test-only exact selector: only the named WAL, once, then the queue is untouched by it', () async {
    final controller = OnDeviceTranscriptionController(model: FakeOnDeviceModel(), power: FakePhonePowerSource());
    controller.debugActivate(settings: const OnDeviceSettings(enabled: true, keepEverythingOnDevice: true));
    final r = runner(controller)..debugOnlyWalFiles = {'synthetic_1700000200.bin'};
    await r.run(transcribeNow: true);
    expect(calls, 1);
    expect(rows, hasLength(1));
    expect(r.debugOnlyWalFiles, isNull, reason: 'one pass');
    untouched(wals.where((w) => w.filePath != 'synthetic_1700000200.bin'));
    r.dispose();
    controller.dispose();
  });

  test('exact selector does nothing while the setting is off', () async {
    final controller = OnDeviceTranscriptionController(model: FakeOnDeviceModel(), power: FakePhonePowerSource());
    controller.debugActivate(settings: const OnDeviceSettings());
    final r = runner(controller)..debugOnlyWalFiles = {'synthetic_1700000200.bin'};
    await r.run(transcribeNow: true);
    expect(calls, 0);
    untouched(wals);
    r.dispose();
    controller.dispose();
  });
}
