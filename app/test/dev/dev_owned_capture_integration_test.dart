import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/dev/dev_mic_token.dart';
import 'package:omi/gen/phone_mic_pigeon.g.dart';

import '../support/capture/capture_replay_world.dart';

// DEV-ONLY timed capture ownership through the REAL CaptureController, capture coordinator and
// NativeMicRecorderService (CaptureReplayWorld: only native host, clock, transport and uploads are fake).
// Run with --dart-define=BASE_DEV_TIMED_CAPTURE=true (the recorder only mints tagged ids in dev builds).
//
// Proves on the actual wiring: ordinary starts are never tagged; an owned start never preempts or adopts a
// user's capture, in either race order; the owned stop never stops a different session; the owned session's
// own stall restart stays tagged with the same token; no armed token ever lingers; a test-owned start is live-only
// (batch preference and offline fallback refuse it with no effects) while ordinary batch is unaffected.

const _t = 0x1BEEF;

/// v4.9: a refused owned start carries the reducer's closed guard code (DEV-ONLY evidence), never a bare error type.
Matcher _refused(String reason) => throwsA(isA<PlatformException>()
    .having((e) => e.code, 'code', 'dev_start_refused')
    .having((e) => e.message, 'message', reason));

void main() {
  late Directory tempDir;
  late CaptureReplayWorld world;

  setUpAll(() {
    if (!kBaseDevTimedCapture) {
      fail('run with --dart-define=BASE_DEV_TIMED_CAPTURE=true');
    }
  });

  setUp(() async {
    tempDir = await Directory.systemTemp.createTemp('dev_owned_capture_');
    world = await CaptureReplayWorld.boot(tempDir: tempDir);
  });

  tearDown(() async {
    expect(DevMicToken.armed, isNull, reason: 'an armed token must never outlive its start call');
    await world.dispose();
    if (tempDir.existsSync()) tempDir.deleteSync(recursive: true);
  });

  List<int> ids() => world.hostApi.startSessionIds;

  test('ordinary start is never tagged', () async {
    await world.startLiveCapture();
    expect(DevMicToken.isTest(ids().single), isFalse);
  });

  test('owned start tags exactly its own native start, and its stop stops it', () async {
    await world.controller.devStreamRecordingOwned(_t);
    await world.settle();
    expect(DevMicToken.tokenOf(ids().single), _t);
    expect(world.controller.devOwnsCurrentSession(_t), isTrue);
    final stopsBefore = world.hostApi.stopCalls;
    expect(await world.controller.devStopOwned(_t), isTrue);
    await world.settle();
    expect(world.hostApi.stopCalls, greaterThan(stopsBefore));
    expect(world.controller.devOwnsCurrentSession(_t), isFalse);
  });

  test('user capture already live: owned start is refused and the user capture is untouched', () async {
    await world.startLiveCapture();
    world.emitNativeState(PhoneMicCaptureState.running);
    final stopsBefore = world.hostApi.stopCalls;
    await expectLater(world.controller.devStreamRecordingOwned(_t), _refused('dev_refused_phase_phoneLive'));
    await world.settle();
    expect(ids().length, 1, reason: 'no second native start');
    expect(world.hostApi.stopCalls, stopsBefore, reason: 'the user capture was not stopped');
    expect(world.hostApi.nativeRecording, isTrue);
  });

  test('race, user start dispatched first in the same turn: owned start refused, user untagged', () async {
    final user = world.controller.streamRecording();
    final owned = world.controller.devStreamRecordingOwned(_t);
    await user;
    await expectLater(owned, _refused('dev_refused_phase_phoneLive'));
    await world.settle();
    expect(ids().length, 1);
    expect(DevMicToken.isTest(ids().single), isFalse);
    expect(world.hostApi.nativeRecording, isTrue);
  });

  test('race, owned start first then a user start: the user wins untagged; the owned stop is a no-op', () async {
    final owned = world.controller.devStreamRecordingOwned(_t);
    final user = world.controller.streamRecording();
    await owned;
    await user;
    await world.settle();
    expect(DevMicToken.tokenOf(ids().first), _t);
    expect(DevMicToken.isTest(ids().last), isFalse, reason: 'the user restart is an ordinary session');
    expect(world.controller.devOwnsCurrentSession(_t), isFalse);
    final stopsBefore = world.hostApi.stopCalls;
    expect(await world.controller.devStopOwned(_t), isFalse);
    await world.settle();
    expect(world.hostApi.stopCalls, stopsBefore, reason: 'the user session was not stopped');
    expect(world.hostApi.nativeRecording, isTrue);
  });

  test('owned stop meets a new unrelated user capture: no-op, the user capture keeps recording', () async {
    await world.controller.devStreamRecordingOwned(_t);
    await world.settle();
    await world.stopLiveCapture(); // the user stops it themselves
    await world.startLiveCapture(); // and starts their own
    world.emitNativeState(PhoneMicCaptureState.running);
    final stopsBefore = world.hostApi.stopCalls;
    expect(await world.controller.devStopOwned(_t), isFalse);
    await world.settle();
    expect(world.hostApi.stopCalls, stopsBefore);
    expect(world.hostApi.nativeRecording, isTrue);
    expect(DevMicToken.isTest(ids().last), isFalse);
  });

  test('owned session stall restart stays tagged with the same token', () async {
    await world.controller.devStreamRecordingOwned(_t);
    await world.settle();
    world.emitNativeState(PhoneMicCaptureState.running);
    await world.elapse(const Duration(seconds: 6)); // no frames: the stall watchdog restarts the mic
    expect(ids().length, greaterThanOrEqualTo(2), reason: 'stall restart issued a new native start');
    expect(ids().every((id) => DevMicToken.tokenOf(id) == _t), isTrue);
    expect(ids().toSet().length, ids().length, reason: 'each restart has a new seq');
  });

  test('ordinary session stall restart stays ordinary', () async {
    await world.startLiveCapture();
    world.emitNativeState(PhoneMicCaptureState.running);
    await world.elapse(const Duration(seconds: 6));
    expect(ids().length, greaterThanOrEqualTo(2));
    expect(ids().any(DevMicToken.isTest), isFalse);
  });

  // ---------------------------------------------------------------- v4.2: live-only test-owned starts

  void expectNoEffects(int socketsBefore) {
    expect(world.hostApi.startCalls, 0, reason: 'no native start of any kind');
    expect(world.hostApi.stopCalls, 0);
    expect(world.controller.isPhoneMicBatchRecording, isFalse);
    expect(world.controller.devOwnsCurrentSession(_t), isFalse);
    expect(world.socketCreates, socketsBefore, reason: 'no transport opened');
  }

  test('batch preference: test-owned start refused with no effects; ordinary batch start unaffected', () async {
    expect(await world.controller.setBatchMode(true), isTrue);
    await world.settle();
    final sockets = world.socketCreates;
    await expectLater(world.controller.devStreamRecordingOwned(_t), _refused('dev_refused_transport_batch'));
    await world.settle();
    expectNoEffects(sockets);
    await world.controller.streamRecording();
    await world.settle();
    expect(world.controller.isPhoneMicBatchRecording, isTrue);
    expect(world.hostApi.lastStartMode, PhoneMicCaptureMode.batch);
    expect(DevMicToken.isTest(ids().single), isFalse);
  });

  test('offline fallback: test-owned start refused with no effects; ordinary offline batch unaffected', () async {
    world.setConnected(false);
    await world.settle();
    final sockets = world.socketCreates;
    await expectLater(world.controller.devStreamRecordingOwned(_t), _refused('dev_refused_transport_batch'));
    await world.settle();
    expectNoEffects(sockets);
    await world.controller.streamRecording();
    await world.settle();
    expect(world.controller.isPhoneMicBatchRecording, isTrue);
    expect(world.hostApi.lastStartMode, PhoneMicCaptureMode.batch);
    expect(DevMicToken.isTest(ids().single), isFalse);
  });
}
