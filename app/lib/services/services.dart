import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:flutter_background_service/flutter_background_service.dart';
import 'package:flutter_sound/flutter_sound.dart';

import 'package:omi/services/connectivity_service.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/sockets.dart';
import 'package:omi/services/wals.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_service.dart';

class ServiceManager {
  late IMicRecorderService _mic;
  late IDeviceService _device;
  late ISocketService _socket;
  late IWalService _wal;
  late ISystemAudioRecorderService _systemAudio;

  static ServiceManager? _instance;
  bool _started = false;

  static ServiceManager _create() {
    ServiceManager sm = ServiceManager();
    sm._mic = MicRecorderBackgroundService(
      runner: BackgroundService(),
    );
    sm._device = DeviceService();
    sm._socket = SocketServicePool();
    sm._wal = WalService();
    if (PlatformService.isDesktop) {
      sm._systemAudio = DesktopSystemAudioRecorderService();
    }

    return sm;
  }

  static ServiceManager instance() {
    if (_instance == null) {
      throw Exception("Service manager is not initiated");
    }

    return _instance!;
  }

  static bool get isInitialized => _instance != null;

  IMicRecorderService get mic => _mic;

  IDeviceService get device => _device;

  ISocketService get socket => _socket;

  IWalService get wal => _wal;

  ISystemAudioRecorderService get systemAudio {
    if (PlatformService.isMobile) {
      throw Exception("System audio recording is only available on macOS and Windows");
    }
    return _systemAudio;
  }

  static Future<void> init() async {
    if (_instance != null) {
      throw Exception("Service manager is initiated");
    }
    _instance = ServiceManager._create();
    await ConnectivityService().init();
  }

  Future<void> start() async {
    if (_started) return;
    _mic.resumeAfterAccountTransition();
    if (Platform.isMacOS) _systemAudio.resumeAfterAccountTransition();
    _device.start();
    _wal.start();
    _started = true;
    if (Platform.isMacOS) {
      // TODO: Decide if system audio should start automatically or be user-initiated
      // await _systemAudio.start();
    }
  }

  Future<void> suspendForAccountTransition() async {
    await _socket.stop();
    await _wal.stop();
    await _mic.stop();
    await _device.stop();
    if (Platform.isMacOS) await _systemAudio.stop();
    _started = false;
  }

  /// Stops every physical capture surface even when provider state is absent
  /// or service startup did not finish. Account transitions call this before
  /// any optional UI/provider cleanup callback.
  Future<void> stopCaptureForAccountTransition() async {
    await Future.wait([
      _socket.stop(),
      _mic.stopForAccountTransition(),
      _device.stop(),
      if (Platform.isMacOS) _systemAudio.stopForAccountTransition(),
    ]);
  }

  Future<void> deinit() async {
    ConnectivityService().dispose();
    await _wal.stop();
    await _mic.stop();
    await _device.stop();
    if (Platform.isMacOS) {
      await _systemAudio.stopAndClearCallbacks();
    }
    _started = false;
  }
}

enum BackgroundServiceStatus {
  initiated,
  running,
}

@pragma('vm:entry-point')
Future<bool> onIosBackground(ServiceInstance service) async {
  WidgetsFlutterBinding.ensureInitialized();

  return true;
}

@pragma('vm:entry-point')
Future onStart(ServiceInstance service) async {
  BackgroundRecorderIsolateBridge(service).start();
}

/// Owns the production recorder protocol inside the background isolate.
/// Stop results are request-scoped and are emitted only after native capture
/// has physically stopped (or failed), never from a generic UI state callback.
class BackgroundRecorderIsolateBridge {
  BackgroundRecorderIsolateBridge(this._service, {this.watchdogInterval = const Duration(seconds: 5)});

  final ServiceInstance _service;
  final Duration watchdogInterval;
  final _operations = _SerializedCaptureOperations();
  MicRecorderService? _recorder;
  Timer? _watchdog;
  DateTime _pongAt = DateTime.now();
  int _quiescedThroughGeneration = -1;

  void start() {
    _service.on('recorder.start').listen((event) {
      final requestId = event?['request_id']?.toString() ?? '';
      final generation = event?['generation'] as int? ?? 0;
      if (generation <= _quiescedThroughGeneration) {
        _service.invoke("recorder.ui.startResult", {
          "request_id": requestId,
          "status": 'error',
          "error": 'Recorder start was superseded by an account transition',
        });
        return;
      }
      unawaited(_operations.run(() => _startRecorder(requestId, generation)).catchError(_logBridgeError));
    });

    _service.on('recorder.stop').listen((event) {
      final requestId = event?['request_id']?.toString() ?? '';
      final generation = event?['generation'] as int? ?? 0;
      final quiesce = event?['quiesce'] == true;
      if (quiesce && generation > _quiescedThroughGeneration) {
        _quiescedThroughGeneration = generation;
      }
      unawaited(_operations.run(() => _stopRecorder(requestId, quiesce: quiesce)).catchError(_logBridgeError));
    });

    _service.on('stop').listen((event) {
      _watchdog?.cancel();
      unawaited(_operations.run(_stopService).catchError(_logBridgeError));
    });

    _service.on('pong').listen((event) {
      _pongAt = DateTime.now();
    });
    _watchdog = Timer.periodic(watchdogInterval, _watchdogTick);
  }

  Future<void> _startRecorder(String requestId, int generation) async {
    if (generation <= _quiescedThroughGeneration) {
      _service.invoke("recorder.ui.startResult", {
        "request_id": requestId,
        "status": 'error',
        "error": 'Recorder start was superseded by an account transition',
      });
      return;
    }
    try {
      final recorder = _recorder ??= MicRecorderService(isInBG: Platform.isAndroid);
      recorder.resumeAfterAccountTransition();
      await recorder.start(
        onByteReceived: (bytes) {
          if (generation <= _quiescedThroughGeneration) return;
          _service.invoke("recorder.ui.audioBytes", {"data": bytes.toList()});
        },
        onStop: () => _service.invoke("recorder.ui.stateUpdate", {"state": 'stopped'}),
        onRecording: () => _service.invoke("recorder.ui.stateUpdate", {"state": 'recording'}),
        onInitializing: () => _service.invoke("recorder.ui.stateUpdate", {"state": 'initializing'}),
      );
      if (generation <= _quiescedThroughGeneration) {
        await recorder.stopForAccountTransition();
        throw BackgroundRecorderStartException('Recorder start was superseded by an account transition');
      }
      _service.invoke("recorder.ui.startResult", {"request_id": requestId, "status": 'recording'});
    } catch (error) {
      _service.invoke("recorder.ui.startResult", {
        "request_id": requestId,
        "status": 'error',
        "error": error.toString(),
      });
    }
  }

  Future<void> _stopRecorder(String requestId, {required bool quiesce}) async {
    try {
      final recorder = _recorder;
      if (recorder != null) {
        if (quiesce) {
          await recorder.stopForAccountTransition();
        } else {
          await recorder.stop();
        }
      }
      _service.invoke("recorder.ui.stopResult", {"request_id": requestId, "status": 'stopped'});
    } catch (error) {
      _service.invoke("recorder.ui.stopResult", {
        "request_id": requestId,
        "status": 'error',
        "error": error.toString(),
      });
    }
  }

  Future<void> _stopService() async {
    final recorder = _recorder;
    if (recorder != null && recorder.status != RecorderServiceStatus.stop) await recorder.stopForAccountTransition();
    _service.invoke("recorder.ui.stateUpdate", {"state": 'stopped'});
    await _service.stopSelf();
  }

  Future<void> _watchdogTick(Timer timer) async {
    if (_pongAt.isBefore(DateTime.now().subtract(watchdogInterval * 3))) {
      timer.cancel();
      try {
        await _operations.run(_stopService);
      } catch (error, stackTrace) {
        Logger.handle(error, stackTrace, message: 'Background recorder watchdog stop failed');
      }
      return;
    }
    _service.invoke("ui.ping");
  }

  dynamic _logBridgeError(Object error, StackTrace stackTrace) {
    Logger.debug('Background recorder bridge operation failed: $error');
  }
}

abstract interface class IBackgroundRecorderRunner {
  Future<void> ensureRunning();
  Future<void> startRecorder({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  });
  Future<void> stopRecorder({bool quiesce = false});
}

class BackgroundRecorderStopException extends StateError {
  BackgroundRecorderStopException(super.message);
}

class BackgroundRecorderStartException extends StateError {
  BackgroundRecorderStartException(super.message);
}

class _SerializedCaptureOperations {
  Future<void> _tail = Future<void>.value();

  Future<T> run<T>(Future<T> Function() operation) {
    final completer = Completer<T>();
    final previous = _tail;
    _tail = () async {
      try {
        await previous;
        completer.complete(await operation());
      } catch (error, stackTrace) {
        completer.completeError(error, stackTrace);
      }
    }();
    return completer.future;
  }
}

class BackgroundService implements IBackgroundRecorderRunner {
  BackgroundService({
    this.recorderStartTimeout = const Duration(seconds: 5),
    this.recorderStopTimeout = const Duration(seconds: 3),
  });

  late FlutterBackgroundService _service;
  BackgroundServiceStatus? _status;
  StreamSubscription? _recordAudioByteStream;
  StreamSubscription? _recordStateStream;
  StreamSubscription? _heartbeatStream;
  final Map<String, Completer<void>> _pendingStarts = {};
  final Map<String, Completer<void>> _pendingStops = {};
  bool _initialized = false;
  int _recorderGeneration = 0;
  int _startRequestSequence = 0;
  int _stopRequestSequence = 0;
  final Duration recorderStartTimeout;
  final Duration recorderStopTimeout;

  BackgroundServiceStatus? get status => _status;

  Future<void> init() async {
    if (_initialized) return;
    _service = FlutterBackgroundService();
    _status = BackgroundServiceStatus.initiated;

    final configured = await _service.configure(
      iosConfiguration: IosConfiguration(
        autoStart: false,
        onForeground: onStart,
        onBackground: onIosBackground,
      ),
      androidConfiguration: AndroidConfiguration(
        autoStart: false,
        onStart: onStart,
        isForegroundMode: true,
        autoStartOnBoot: false,
        foregroundServiceTypes: [AndroidForegroundType.microphone],
      ),
    );
    if (!configured) throw StateError('Background recorder service configuration failed');

    _service.on('recorder.ui.stopResult').listen((event) {
      final requestId = event?['request_id']?.toString() ?? '';
      final completer = _pendingStops.remove(requestId);
      if (completer == null || completer.isCompleted) return;
      if (event?['status'] == 'stopped') {
        completer.complete();
      } else {
        completer.completeError(
          BackgroundRecorderStopException(event?['error']?.toString() ?? 'Native recorder stop failed'),
        );
      }
    });

    _service.on('recorder.ui.startResult').listen((event) {
      final requestId = event?['request_id']?.toString() ?? '';
      final completer = _pendingStarts.remove(requestId);
      if (completer == null || completer.isCompleted) return;
      if (event?['status'] == 'recording') {
        completer.complete();
      } else {
        completer.completeError(
          BackgroundRecorderStartException(event?['error']?.toString() ?? 'Native recorder start failed'),
        );
      }
    });

    _initialized = true;
    _status = BackgroundServiceStatus.initiated;
  }

  @override
  Future<void> ensureRunning() async {
    await init();
    await start();
  }

  Future<void> start() async {
    await _service.startService();
    var running = await _service.isRunning();
    final maxAttempts = (recorderStartTimeout.inMilliseconds / 50).ceil().clamp(1, 100);
    for (var attempt = 0; !running && attempt < maxAttempts; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
      running = await _service.isRunning();
    }
    if (!running) {
      throw BackgroundRecorderStartException('Background recorder service did not become ready');
    }
    _status = BackgroundServiceStatus.running;

    // heartbeat
    await _heartbeatStream?.cancel();
    _heartbeatStream = _service.on('ui.ping').listen((event) {
      _service.invoke("pong");
    });
  }

  void stop() {
    Logger.debug("invoke stop");
    _service.invoke("stop");
  }

  void onStop(ServiceInstance instance) async {
    _service.invoke("recorder.stateUpdate", {"state": 'stopped'});
    instance.stopSelf();
  }

  @override
  Future<void> startRecorder({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) async {
    await _recordAudioByteStream?.cancel();
    await _recordStateStream?.cancel();
    final generation = ++_recorderGeneration;
    _recordAudioByteStream = _service.on('recorder.ui.audioBytes').listen((event) {
      Uint8List bytes = Uint8List.fromList(event!['data'].cast<int>());
      onByteReceived(bytes);
    });
    _recordStateStream = _service.on('recorder.ui.stateUpdate').listen((event) {
      if (event!['state'] == 'recording') {
        if (onRecording != null) {
          onRecording();
        }
      } else if (event['state'] == 'initializing') {
        if (onInitializing != null) {
          onInitializing();
        }
      } else if (event['state'] == 'stopped') {
        onStop?.call();
      }
    });

    final requestId = '${generation}_${++_startRequestSequence}';
    final started = Completer<void>();
    _pendingStarts[requestId] = started;
    _service.invoke("recorder.start", {"request_id": requestId, "generation": generation});
    try {
      await started.future.timeout(recorderStartTimeout);
    } finally {
      _pendingStarts.remove(requestId);
    }
  }

  @override
  Future<void> stopRecorder({bool quiesce = false}) async {
    if (!_initialized) return;
    await _recordAudioByteStream?.cancel();
    _recordAudioByteStream = null;
    final requestId = '${_recorderGeneration}_${++_stopRequestSequence}';
    final stopped = Completer<void>();
    _pendingStops[requestId] = stopped;
    _service.invoke("recorder.stop", {
      "request_id": requestId,
      "generation": _recorderGeneration,
      "quiesce": quiesce,
    });
    try {
      await stopped.future.timeout(recorderStopTimeout);
    } finally {
      _pendingStops.remove(requestId);
      await _recordStateStream?.cancel();
      _recordStateStream = null;
    }
  }
}

enum RecorderServiceStatus {
  initialising,
  recording,
  stop,
}

abstract class IMicRecorderService {
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  });
  Future<void> stop();
  Future<void> stopForAccountTransition();
  void resumeAfterAccountTransition();
}

class MicRecorderBackgroundService implements IMicRecorderService {
  late IBackgroundRecorderRunner _runner;
  final _operations = _SerializedCaptureOperations();
  bool _transitionQuiesced = false;
  int _generation = 0;

  MicRecorderBackgroundService({required IBackgroundRecorderRunner runner}) {
    _runner = runner;
  }

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) {
    final generation = _generation;
    return _operations.run(() async {
      if (_transitionQuiesced || generation != _generation) return;
      await _runner.ensureRunning();
      if (_transitionQuiesced || generation != _generation) {
        await _runner.stopRecorder(quiesce: true);
        return;
      }
      await _runner.startRecorder(
        onByteReceived: (bytes) {
          if (!_transitionQuiesced && generation == _generation) onByteReceived(bytes);
        },
        onRecording: () {
          if (!_transitionQuiesced && generation == _generation) onRecording?.call();
        },
        onStop: () {
          if (generation == _generation) onStop?.call();
        },
        onInitializing: () {
          if (!_transitionQuiesced && generation == _generation) onInitializing?.call();
        },
      );
      if (_transitionQuiesced || generation != _generation) await _runner.stopRecorder(quiesce: true);
    });
  }

  @override
  Future<void> stop() {
    _generation++;
    return _operations.run(_runner.stopRecorder);
  }

  @override
  Future<void> stopForAccountTransition() {
    _transitionQuiesced = true;
    _generation++;
    return _operations.run(() => _runner.stopRecorder(quiesce: true));
  }

  @override
  void resumeAfterAccountTransition() {
    _transitionQuiesced = false;
  }
}

class MicRecorderService implements IMicRecorderService {
  RecorderServiceStatus? _status;

  late FlutterSoundRecorder _recorder;
  StreamController<Uint8List>? _controller;
  StreamSubscription<Uint8List>? _audioSubscription;

  Function(Uint8List bytes)? _onByteReceived;
  Function? _onRecording;
  Function? _onStop;
  final _operations = _SerializedCaptureOperations();
  bool _transitionQuiesced = false;
  int _generation = 0;

  bool _isInBG = false;

  MicRecorderService({bool isInBG = false}) {
    _recorder = FlutterSoundRecorder();
    _isInBG = isInBG;
  }

  get status => _status;

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) {
    final generation = _generation;
    return _operations.run(
      () => _start(
        onByteReceived: onByteReceived,
        onRecording: onRecording,
        onStop: onStop,
        onInitializing: onInitializing,
        generation: generation,
      ),
    );
  }

  Future<void> _start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    required int generation,
  }) async {
    if (_transitionQuiesced || generation != _generation) return;
    if (_status == RecorderServiceStatus.recording) {
      throw Exception("Recorder is recording, please stop it before start new recording.");
    }
    if (_status == RecorderServiceStatus.initialising) {
      throw Exception("Recorder is initialising");
    }

    _status = RecorderServiceStatus.initialising;

    // callback
    _onByteReceived = onByteReceived;
    _onStop = onStop;
    _onRecording = onRecording;
    onInitializing?.call();

    // new record
    await _recorder.openRecorder(isBGService: _isInBG);
    if (_transitionQuiesced || generation != _generation) {
      await _stopNative();
      return;
    }
    _controller = StreamController<Uint8List>.broadcast();

    await _recorder.startRecorder(
      toStream: _controller!.sink,
      codec: Codec.pcm16,
      numChannels: 1,
      sampleRate: 16000,
      bufferSize: 8192,
    );
    if (_transitionQuiesced || generation != _generation) {
      await _stopNative();
      return;
    }
    _audioSubscription = _controller!.stream.listen((buffer) {
      Uint8List audioBytes = buffer;
      if (!_transitionQuiesced && generation == _generation && _onByteReceived != null) {
        _onByteReceived!(audioBytes);
      }
    });

    _status = RecorderServiceStatus.recording;
    _onRecording?.call();
    return;
  }

  @override
  Future<void> stop() {
    _generation++;
    return _operations.run(_stopNative);
  }

  @override
  Future<void> stopForAccountTransition() {
    _transitionQuiesced = true;
    _generation++;
    return _operations.run(_stopNative);
  }

  @override
  void resumeAfterAccountTransition() {
    _transitionQuiesced = false;
  }

  Future<void> _stopNative() async {
    final onStop = _onStop;
    _onByteReceived = null;
    Object? stopError;
    StackTrace? stopStackTrace;
    try {
      if (_status == RecorderServiceStatus.recording || _status == RecorderServiceStatus.initialising) {
        await _recorder.stopRecorder();
        if (!_recorder.isStopped) {
          throw BackgroundRecorderStopException('Native recorder did not acknowledge a physical stop');
        }
      }
    } catch (error, stackTrace) {
      stopError = error;
      stopStackTrace = stackTrace;
    }
    await _audioSubscription?.cancel();
    _audioSubscription = null;

    _onByteReceived = null;
    _onStop = null;
    _onRecording = null;
    if (stopError != null) Error.throwWithStackTrace(stopError, stopStackTrace!);

    final controller = _controller;
    if (controller != null && !controller.isClosed) await controller.close();
    _controller = null;

    // Native stop succeeded, so it is now safe to acknowledge quiescence.
    _status = RecorderServiceStatus.stop;
    onStop?.call();
  }
}

abstract class ISystemAudioRecorderService {
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    required Function(Map<String, dynamic> format) onFormatReceived,
    Function()? onRecording,
    Function()? onStop,
    Function(String error)? onError,
    Function(bool wasRecording)? onSystemWillSleep,
    Function(bool nativeIsRecording)? onSystemDidWake,
    Function(bool wasRecording)? onScreenDidLock,
    Function()? onScreenDidUnlock,
    Function(String reason)? onDisplaySetupInvalid,
    Function()? onMicrophoneDeviceChanged,
    Function(String deviceName, double micLevel, double systemAudioLevel)? onMicrophoneStatus,
    Function()? onStoppedAutomatically,
  });
  Future<void> stop();
  Future<void> stopAndClearCallbacks();
  Future<void> stopForAccountTransition();
  void resumeAfterAccountTransition();
  void setOnRecordingStartedFromNub(Function() callback);
  void setIsRecordingPausedCallback(bool Function() callback);
  // TODO: Add status property
}

class DesktopSystemAudioRecorderService implements ISystemAudioRecorderService {
  DesktopSystemAudioRecorderService({MethodChannel? channel})
      : _channel = channel ?? const MethodChannel('screenCapturePlatform') {
    _channel.setMethodCallHandler(_handleMethodCall);
  }

  final MethodChannel _channel;
  final _operations = _SerializedCaptureOperations();
  bool _transitionQuiesced = false;
  int _generation = 0;
  Function(Uint8List bytes)? _onByteReceived;
  Function(Map<String, dynamic> format)? _onFormatReceived;
  Function()? _onRecording;
  Function()? _onStop;
  Function(String error)? _onError;

  // Sleep/wake event callbacks
  Function(bool wasRecording)? _onSystemWillSleep;
  Function(bool nativeIsRecording)? _onSystemDidWake;
  Function(bool wasRecording)? _onScreenDidLock;
  Function()? _onScreenDidUnlock;
  Function(String reason)? _onDisplaySetupInvalid;
  Function()? _onMicrophoneDeviceChanged;
  Function(String deviceName, double micLevel, double systemAudioLevel)? _onMicrophoneStatus;

  // Callback for when recording is started from nub (registered early, before start() is called)
  Function()? _onRecordingStartedFromNub;

  // Callback for when recording is stopped automatically (e.g., meeting ended)
  Function()? _onStoppedAutomatically;

  // Callback to query if recording is paused
  bool Function()? _isRecordingPausedCallback;

  // To keep track of recording state from Dart's perspective
  bool _isRecording = false;

  Future<dynamic> _handleMethodCall(MethodCall call) async {
    if (_transitionQuiesced && call.method != 'isRecordingPaused') return;
    switch (call.method) {
      case 'audioFrame':
        if (_onByteReceived != null && call.arguments is Uint8List) {
          _onByteReceived!(call.arguments);
        }
        break;
      case 'audioFormat':
        Logger.debug("audioFormat: ${call.arguments}");
        if (_onFormatReceived != null && call.arguments is Map) {
          final Map<String, dynamic> format = Map<String, dynamic>.from(call.arguments as Map);
          _onFormatReceived!(format);
        }
        break;
      case 'audioStreamEnded':
        Logger.debug("audioStreamEnded");
        _isRecording = false;
        if (_onStop != null) {
          _onStop!();
        }
        _clearCallbacks();
        break;
      case 'captureError':
      case 'converterError':
        Logger.debug("captureError: ${call.arguments}");
        _isRecording = false;
        if (_onError != null && call.arguments is String) {
          _onError!(call.arguments as String);
        }
        if (_onStop != null) {
          _onStop!(); // Also call onStop if there's an error
        }
        _clearCallbacks(); // Clear callbacks after error
        break;
      case 'systemWillSleep':
        await _handleSystemWillSleep(call.arguments);
        break;
      case 'systemDidWake':
        await _handleSystemDidWake(call.arguments);
        break;
      case 'screenDidLock':
        await _handleScreenDidLock(call.arguments);
        break;
      case 'screenDidUnlock':
        await _handleScreenDidUnlock(call.arguments);
        break;
      case 'displaySetupInvalid':
        await _handleDisplaySetupInvalid(call.arguments);
        break;
      case 'microphoneDeviceChanged':
        await _handleMicrophoneDeviceChanged(call.arguments);
        break;
      case 'microphoneStatus':
        if (_onMicrophoneStatus != null && call.arguments is Map) {
          final args = Map<String, dynamic>.from(call.arguments as Map);
          final deviceName = args['deviceName'] as String? ?? 'Unknown Device';
          final micLevel = (args['micLevel'] as num? ?? 0.0).toDouble();
          final systemAudioLevel = (args['systemAudioLevel'] as num? ?? 0.0).toDouble();
          _onMicrophoneStatus!(deviceName, micLevel, systemAudioLevel);
        }
        break;
      case 'recordingStartedFromNub':
        // Recording was started from the meeting detection nub
        if (_onRecordingStartedFromNub != null) {
          _onRecordingStartedFromNub!();
        } else {
          Logger.debug(
              'DesktopSystemAudioRecorderService: WARNING - No callback registered for recordingStartedFromNub');
        }
        break;
      case 'recordingStoppedAutomatically':
        Logger.debug('recordingStoppedAutomatically received - will trigger conversation processing after stop');
        if (_onStoppedAutomatically != null) {
          _onStoppedAutomatically!();
        }

        _isRecording = false;
        if (_onStop != null) {
          _onStop!();
        }
        break;
      case 'speakerStatusChanged': //TODO: Handle speaker status changed
        if (call.arguments is Map) {
          final args = Map<String, dynamic>.from(call.arguments as Map);
          final isSpeakerActive = args['isSpeakerActive'] as bool? ?? false;
          Logger.debug('Speaker status changed: $isSpeakerActive');
        }
        break;
      case 'isRecordingPaused':
        // Return the pause state to native code
        if (_isRecordingPausedCallback != null) {
          return _isRecordingPausedCallback!();
        }
        return false;
      default:
        Logger.debug('DesktopSystemAudioRecorderService: Unhandled method call: ${call.method}');
    }
  }

  void _clearCallbacks() {
    _onByteReceived = null;
    _onFormatReceived = null;
    _onRecording = null;
    _onStop = null;
    _onError = null;
    _onSystemWillSleep = null;
    _onSystemDidWake = null;
    _onScreenDidLock = null;
    _onScreenDidUnlock = null;
    _onDisplaySetupInvalid = null;
    _onMicrophoneDeviceChanged = null;
    _onMicrophoneStatus = null;
    _onStoppedAutomatically = null;
  }

  // Sleep/wake event handlers
  Future<void> _handleSystemWillSleep(dynamic arguments) async {
    final args = arguments as Map<String, dynamic>?;
    final wasRecording = args?['wasRecording'] as bool? ?? false;
    _onSystemWillSleep?.call(wasRecording);
  }

  Future<void> _handleSystemDidWake(dynamic arguments) async {
    final args = arguments as Map<String, dynamic>?;
    final nativeIsRecording = args?['nativeIsRecording'] as bool? ?? false;

    if (nativeIsRecording && !_isRecording) {
      _isRecording = true;
      _onRecording?.call();
    } else if (!nativeIsRecording && _isRecording) {
      _isRecording = false;
      _onStop?.call();
      _clearCallbacks();
    }

    _onSystemDidWake?.call(nativeIsRecording);
  }

  Future<void> _handleScreenDidLock(dynamic arguments) async {
    final args = arguments as Map<String, dynamic>?;
    final wasRecording = args?['wasRecording'] as bool? ?? false;
    _onScreenDidLock?.call(wasRecording);
  }

  Future<void> _handleScreenDidUnlock(dynamic arguments) async {
    _onScreenDidUnlock?.call();
  }

  Future<void> _handleDisplaySetupInvalid(dynamic arguments) async {
    final args = arguments as Map<String, dynamic>?;
    final reason = args?['reason'] as String? ?? 'Unknown reason';

    _isRecording = false;
    _onDisplaySetupInvalid?.call(reason);
    _onStop?.call();
  }

  Future<void> _handleMicrophoneDeviceChanged(dynamic arguments) async {
    _onMicrophoneDeviceChanged?.call();
  }

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    required Function(Map<String, dynamic> format) onFormatReceived,
    Function()? onRecording,
    Function()? onStop,
    Function(String error)? onError,
    Function(bool wasRecording)? onSystemWillSleep,
    Function(bool nativeIsRecording)? onSystemDidWake,
    Function(bool wasRecording)? onScreenDidLock,
    Function()? onScreenDidUnlock,
    Function(String reason)? onDisplaySetupInvalid,
    Function()? onMicrophoneDeviceChanged,
    Function(String deviceName, double micLevel, double systemAudioLevel)? onMicrophoneStatus,
    Function()? onStoppedAutomatically,
  }) {
    final generation = _generation;
    return _operations.run(
      () => _start(
        onByteReceived: onByteReceived,
        onFormatReceived: onFormatReceived,
        onRecording: onRecording,
        onStop: onStop,
        onError: onError,
        onSystemWillSleep: onSystemWillSleep,
        onSystemDidWake: onSystemDidWake,
        onScreenDidLock: onScreenDidLock,
        onScreenDidUnlock: onScreenDidUnlock,
        onDisplaySetupInvalid: onDisplaySetupInvalid,
        onMicrophoneDeviceChanged: onMicrophoneDeviceChanged,
        onMicrophoneStatus: onMicrophoneStatus,
        onStoppedAutomatically: onStoppedAutomatically,
        generation: generation,
      ),
    );
  }

  Future<void> _start({
    required Function(Uint8List bytes) onByteReceived,
    required Function(Map<String, dynamic> format) onFormatReceived,
    Function()? onRecording,
    Function()? onStop,
    Function(String error)? onError,
    Function(bool wasRecording)? onSystemWillSleep,
    Function(bool nativeIsRecording)? onSystemDidWake,
    Function(bool wasRecording)? onScreenDidLock,
    Function()? onScreenDidUnlock,
    Function(String reason)? onDisplaySetupInvalid,
    Function()? onMicrophoneDeviceChanged,
    Function(String deviceName, double micLevel, double systemAudioLevel)? onMicrophoneStatus,
    Function()? onStoppedAutomatically,
    required int generation,
  }) async {
    if (_transitionQuiesced || generation != _generation) return;
    try {
      final nativeIsRecording = await _channel.invokeMethod('isRecording') ?? false;
      if (_transitionQuiesced || generation != _generation) {
        if (nativeIsRecording) await _channel.invokeMethod('stop');
        return;
      }

      if (nativeIsRecording && _isRecording) {
        onError?.call("Already recording");
        return;
      } else if (nativeIsRecording && !_isRecording) {
        _isRecording = true;
        _onByteReceived = onByteReceived;
        _onFormatReceived = onFormatReceived;
        _onRecording = onRecording;
        _onStop = onStop;
        _onError = onError;
        _onSystemWillSleep = onSystemWillSleep;
        _onSystemDidWake = onSystemDidWake;
        _onScreenDidLock = onScreenDidLock;
        _onScreenDidUnlock = onScreenDidUnlock;
        _onDisplaySetupInvalid = onDisplaySetupInvalid;
        _onMicrophoneDeviceChanged = onMicrophoneDeviceChanged;
        _onMicrophoneStatus = onMicrophoneStatus;
        _onStoppedAutomatically = onStoppedAutomatically;

        _onRecording?.call();
        return;
      } else if (!nativeIsRecording && _isRecording) {
        _isRecording = false;
      }
    } catch (e) {
      Logger.debug("[SystemAudio] State check error: $e");
    }

    if (_transitionQuiesced || generation != _generation) return;

    if (_isRecording) {
      onError?.call("Already recording");
      return;
    }

    _onByteReceived = onByteReceived;
    _onFormatReceived = onFormatReceived;
    _onRecording = onRecording;
    _onStop = onStop;
    _onError = onError;
    _onSystemWillSleep = onSystemWillSleep;
    _onSystemDidWake = onSystemDidWake;
    _onScreenDidLock = onScreenDidLock;
    _onScreenDidUnlock = onScreenDidUnlock;
    _onDisplaySetupInvalid = onDisplaySetupInvalid;
    _onMicrophoneDeviceChanged = onMicrophoneDeviceChanged;
    _onMicrophoneStatus = onMicrophoneStatus;
    _onStoppedAutomatically = onStoppedAutomatically;

    try {
      await _channel.invokeMethod('start');
      if (_transitionQuiesced || generation != _generation) {
        await _stopAndClearCallbacksNative();
        return;
      }
      _isRecording = true;
      _onRecording?.call();
    } catch (e) {
      _isRecording = false;
      _onError?.call(e.toString());
      _onStop?.call();
      _clearCallbacks();
    }
  }

  @override
  void setOnRecordingStartedFromNub(Function() callback) {
    _onRecordingStartedFromNub = callback;
  }

  @override
  void setIsRecordingPausedCallback(bool Function() callback) {
    _isRecordingPausedCallback = callback;
  }

  @override
  Future<void> stop() {
    _generation++;
    return _operations.run(_stopNative);
  }

  Future<void> _stopNative() async {
    try {
      await _channel.invokeMethod('stop');
    } catch (e) {
      _isRecording = false;
      _onError?.call(e.toString());
      _onStop?.call();
      _clearCallbacks();
    }
  }

  /// Stop recording and immediately clear callbacks to prevent them from being
  /// called when the native stop completes
  @override
  Future<void> stopAndClearCallbacks() {
    _generation++;
    _isRecording = false;
    _clearCallbacks();
    return _operations.run(_stopAndClearCallbacksNative);
  }

  Future<void> _stopAndClearCallbacksNative({bool failClosed = false}) async {
    _isRecording = false;
    _clearCallbacks();
    try {
      await _channel.invokeMethod('stop');
    } catch (e) {
      Logger.debug('DesktopSystemAudioRecorderService: Error stopping: $e');
      if (failClosed) rethrow;
    }
  }

  @override
  Future<void> stopForAccountTransition() {
    _transitionQuiesced = true;
    _generation++;
    _isRecording = false;
    _clearCallbacks();
    return _operations.run(() => _stopAndClearCallbacksNative(failClosed: true));
  }

  @override
  void resumeAfterAccountTransition() {
    _transitionQuiesced = false;
  }
}
