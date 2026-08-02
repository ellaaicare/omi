import 'dart:async';

import 'package:flutter/services.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';

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
  bool get isAvailable => allowsGuardianSurface();

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
    if (!isAvailable) {
      _updateState(GuardianModeState.idle);
      throw StateError('Guardian is unavailable in this build');
    }
    if (_currentState == GuardianModeState.active) {
      print('GuardianMode: Already active');
      return;
    }

    try {
      await _channel.invokeMethod('configureAvailability', {'enabled': true});
      // Call iOS native to start silent loop
      await _channel.invokeMethod('start');
      print('GuardianMode: Native started');

      _updateState(GuardianModeState.active);

      // Start test audio injection timer (every 5 seconds)
      // _startTestAudioTimer(); // Disabled - using polling service instead
    } catch (e) {
      try {
        await _channel.invokeMethod('configureAvailability', {'enabled': false});
      } catch (_) {
        // Native setup may already be unavailable; local state still fails closed.
      }
      print('GuardianMode: Error starting: $e');
      _updateState(GuardianModeState.error);
      rethrow;
    }
  }

  /// Stop Guardian Mode
  Future<void> stop() async {
    try {
      // Stop test audio timer
      _stopTestAudioTimer();

      // Disable native availability even when Flutter already considers the
      // service idle. This prevents a failed or interrupted start from leaving
      // polling/playback enabled.
      await _channel.invokeMethod('configureAvailability', {'enabled': false});
      print('GuardianMode: Stopped');

      _updateState(GuardianModeState.idle);

      // Clean up resources (singleton pattern - no dispose() method)
      _cleanup();
    } catch (e) {
      print('GuardianMode: Error stopping: $e');
      _updateState(GuardianModeState.error);
    }
  }

  Future<void> stopForAccountTransition() async {
    _stopTestAudioTimer();
    try {
      await _channel.invokeMethod('configureAvailability', {'enabled': false});
      await _channel.invokeMethod('stop');
    } catch (_) {
      // The native side may not be initialized; local state still fails closed.
    }
    _updateState(GuardianModeState.idle);
    _cleanup();
  }

  /// Start timer to inject test audio clips
  // ignore: unused_element
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
    if (!isAvailable || _currentState != GuardianModeState.active) {
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
