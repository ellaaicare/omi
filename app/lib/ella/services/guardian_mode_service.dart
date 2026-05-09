import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

enum GuardianModeState {
  idle,
  active,
  error,
}

class GuardianModeService {
  static final GuardianModeService _instance = GuardianModeService._internal();
  factory GuardianModeService() => _instance;
  GuardianModeService._internal();

  static const MethodChannel _channel = MethodChannel('com.ellaaicare.ella/guardian_mode');

  StreamController<GuardianModeState>? _stateController;
  Stream<GuardianModeState> get stateStream =>
      (_stateController ??= StreamController<GuardianModeState>.broadcast()).stream;

  GuardianModeState _currentState = GuardianModeState.idle;
  GuardianModeState get currentState => _currentState;

  Timer? _testAudioTimer;
  int _testClipCounter = 0;

  // Bundled MP3 test files (simulating server audio responses)
  static const List<String> _testAudioFiles = [
    'test_audio_0.mp3',
    'test_audio_1.mp3',
    'test_audio_2.mp3',
    'test_audio_3.mp3',
    'test_audio_4.mp3',
  ];

  /// Start Guardian Mode
  Future<void> start() async {
    if (_currentState == GuardianModeState.active) {
      print('GuardianMode: Already active');
      return;
    }

    try {
      // Call iOS native to start silent loop
      await _channel.invokeMethod('start');
      print('GuardianMode: Native started');

      _updateState(GuardianModeState.active);

      // Start test audio injection timer (every 5 seconds)
      // _startTestAudioTimer(); // Disabled - using polling service instead
    } catch (e) {
      print('GuardianMode: Error starting: $e');
      _updateState(GuardianModeState.error);
      rethrow;
    }
  }

  /// Stop Guardian Mode
  Future<void> stop() async {
    if (_currentState == GuardianModeState.idle) {
      print('GuardianMode: Already stopped');
      return;
    }

    try {
      // Stop test audio timer
      _stopTestAudioTimer();

      // Call iOS native to stop
      await _channel.invokeMethod('stop');
      print('GuardianMode: Stopped');

      _updateState(GuardianModeState.idle);

      // Clean up resources (singleton pattern - no dispose() method)
      _cleanup();
    } catch (e) {
      print('GuardianMode: Error stopping: $e');
      _updateState(GuardianModeState.error);
    }
  }

  /// Ask native iOS to interrupt the active Guardian poll timer after a likely
  /// wake phrase. Native side still verifies Guardian is active.
  Future<void> requestWakeAckPoll({String reason = 'wake_candidate', String? transcript}) async {
    try {
      await _channel.invokeMethod('requestWakeAckPoll', {
        'reason': reason,
        if (transcript != null) 'transcript': transcript,
      });
    } catch (e) {
      debugPrint('GuardianMode: Error requesting wake ack poll: $e');
    }
  }

  /// Start timer to inject test audio clips
  void _startTestAudioTimer() {
    _testClipCounter = 0;
    _testAudioTimer?.cancel();

    // Wait 2 seconds before first clip (let silent loop initialize)
    Future.delayed(const Duration(seconds: 2), () {
      if (_currentState == GuardianModeState.active) {
        _injectNextTestClip();
      }
    });

    _testAudioTimer = Timer.periodic(const Duration(seconds: 5), (timer) async {
      await _injectNextTestClip();
    });
  }

  /// Stop test audio timer
  void _stopTestAudioTimer() {
    _testAudioTimer?.cancel();
    _testAudioTimer = null;
    _testClipCounter = 0;
  }

  /// Inject next test audio clip from bundled MP3 files
  Future<void> _injectNextTestClip() async {
    // Check if we're still in active state (prevents race conditions)
    if (_currentState != GuardianModeState.active) {
      print('GuardianMode: Skipping clip injection - not in active state');
      return;
    }

    try {
      // Cycle through the test audio files
      final fileName = _testAudioFiles[_testClipCounter % _testAudioFiles.length];
      _testClipCounter++;

      print('GuardianMode: Injecting bundled test clip $_testClipCounter: $fileName');

      // Pass filename to native side - it will look it up in app bundle
      await _channel.invokeMethod('injectBundledAudioClip', {'fileName': fileName});
      print('GuardianMode: Injected clip $_testClipCounter');
    } catch (e) {
      print('GuardianMode: Error injecting test clip: $e');
    }
  }

  /// Update state and notify listeners
  void _updateState(GuardianModeState newState) {
    _currentState = newState;
    (_stateController ??= StreamController<GuardianModeState>.broadcast()).add(newState);
  }

  /// Clean up resources
  /// Note: This is a singleton, so dispose() is not appropriate.
  /// Resources are cleaned up when stop() is called instead.
  void _cleanup() {
    //     _stateController?.close();
    //     _stateController = null;
  }
}
