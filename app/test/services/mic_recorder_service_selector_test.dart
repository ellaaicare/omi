import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:flutter_sound_platform_interface/flutter_sound_recorder_platform_interface.dart';

import 'package:omi/services/services.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('production selector uses direct microphone recorder on iOS', () {
    var backgroundFactoryCalls = 0;

    final service = selectMicRecorderService(
      isIOS: true,
      backgroundRunnerFactory: () {
        backgroundFactoryCalls++;
        return _RecordingBackgroundRunner();
      },
    );

    expect(service, isA<MicRecorderService>());
    expect(backgroundFactoryCalls, 0);
  });

  test('selected iOS recorder fences an in-flight start across account transition', () async {
    final originalPlatform = FlutterSoundRecorderPlatform.instance;
    final native = _ControlledFlutterSoundRecorderPlatform();
    FlutterSoundRecorderPlatform.instance = native;
    addTearDown(() async {
      native.releaseStart();
      await Future<void>.delayed(Duration.zero);
      FlutterSoundRecorderPlatform.instance = originalPlatform;
    });
    final service = selectMicRecorderService(isIOS: true);
    var recordingCallbacks = 0;

    final start = service.start(
      onByteReceived: (_) {},
      onRecording: () => recordingCallbacks++,
    );
    await native.startEntered.future;

    var transitionCompleted = false;
    final transition = service.stopForAccountTransition().then((_) => transitionCompleted = true);
    await Future<void>.delayed(Duration.zero);
    expect(transitionCompleted, isFalse);

    native.releaseStart();
    await Future.wait([start, transition]);
    expect(recordingCallbacks, 0);

    service.resumeAfterAccountTransition();
    await service.start(
      onByteReceived: (_) {},
      onRecording: () => recordingCallbacks++,
    );
    expect(recordingCallbacks, 1);
    await service.stop();
  });

  test('production selector retains background runner and transition contract elsewhere', () async {
    final runner = _RecordingBackgroundRunner();

    final service = selectMicRecorderService(
      isIOS: false,
      backgroundRunnerFactory: () => runner,
    );

    expect(service, isA<MicRecorderBackgroundService>());

    await service.start(onByteReceived: (_) {});
    expect(runner.ensureRunningCalls, 1);
    expect(runner.startRecorderCalls, 1);

    await service.stopForAccountTransition();
    expect(runner.stopQuiesceValues, [true]);

    service.resumeAfterAccountTransition();
    await service.start(onByteReceived: (_) {});
    expect(runner.startRecorderCalls, 2);
  });
}

class _RecordingBackgroundRunner implements IBackgroundRecorderRunner {
  int ensureRunningCalls = 0;
  int startRecorderCalls = 0;
  final List<bool> stopQuiesceValues = [];

  @override
  Future<void> ensureRunning() async {
    ensureRunningCalls++;
  }

  @override
  Future<void> startRecorder({
    required void Function(Uint8List bytes) onByteReceived,
    void Function()? onRecording,
    void Function()? onStop,
    void Function()? onInitializing,
  }) async {
    startRecorderCalls++;
  }

  @override
  Future<void> stopRecorder({bool quiesce = false}) async {
    stopQuiesceValues.add(quiesce);
  }
}

class _ControlledFlutterSoundRecorderPlatform extends FlutterSoundRecorderPlatform {
  final Completer<void> startEntered = Completer<void>();
  final Completer<void> _startRelease = Completer<void>();
  var startCalls = 0;
  var stopCalls = 0;

  @override
  Future<void> openRecorder(FlutterSoundRecorderCallback callback, {required dynamic logLevel}) async {
    callback.openRecorderCompleted(RecorderState.isStopped.index, true);
  }

  @override
  Future<void> resetPlugin(FlutterSoundRecorderCallback callback) async {}

  @override
  Future<void> closeRecorder(FlutterSoundRecorderCallback callback) async {}

  @override
  Future<bool> isEncoderSupported(FlutterSoundRecorderCallback callback, {required Codec codec}) async => true;

  @override
  Future<void> startRecorder(
    FlutterSoundRecorderCallback callback, {
    Codec? codec,
    String? path,
    int sampleRate = 44100,
    int numChannels = 1,
    int bitRate = 16000,
    int bufferSize = 8192,
    Duration timeSlice = Duration.zero,
    bool enableVoiceProcessing = false,
    bool interleaved = true,
    required bool toStream,
    AudioSource? audioSource,
  }) async {
    startCalls++;
    if (!startEntered.isCompleted) startEntered.complete();
    await _startRelease.future;
    callback.startRecorderCompleted(RecorderState.isRecording.index, true);
  }

  @override
  Future<void> stopRecorder(FlutterSoundRecorderCallback callback) async {
    stopCalls++;
    callback.stopRecorderCompleted(RecorderState.isStopped.index, true, null);
  }

  void releaseStart() {
    if (!_startRelease.isCompleted) _startRelease.complete();
  }

  @override
  int getSampleRate(FlutterSoundRecorderCallback callback) => 16000;

  @override
  void requestData(FlutterSoundRecorderCallback callback) {}
}
