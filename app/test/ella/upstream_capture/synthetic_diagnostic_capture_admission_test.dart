import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import '../../upstream_capture/support/capture/capture_replay_world.dart';

void main() {
  test('diagnostic quiet admission rejects staged native start with stopped visible state', () async {
    final directory = await Directory.systemTemp.createTemp('synthetic-admission-');
    final world = await CaptureReplayWorld.boot(tempDir: directory);
    try {
      expect(world.controller.isQuiescentForDiagnostic, isTrue);
      final hold = Completer<void>();
      final entered = Completer<void>();
      world.hostApi.holdNextStart = hold;
      world.hostApi.nextStartEntered = entered;
      final start = world.controller.streamRecording();
      await entered.future;
      world.controller.recordingState = RecordingState.stop;
      expect(world.controller.isQuiescentForDiagnostic, isFalse);
      hold.complete();
      await start;
      world.emitNativeState(PhoneMicCaptureState.running);
      await world.settle();
      expect(world.controller.isQuiescentForDiagnostic, isFalse);
      await world.stopLiveCapture();
    } finally {
      await world.dispose();
      await directory.delete(recursive: true);
    }
  });
}
