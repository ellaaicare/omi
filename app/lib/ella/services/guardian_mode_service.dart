import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:flutter_tts/flutter_tts.dart';

enum GuardianModeState {
  idle,
  active,
  error,
}

class GuardianModeService {
  static final GuardianModeService _instance = GuardianModeService._internal();
  factory GuardianModeService() => _instance;
  GuardianModeService._internal();

  static const MethodChannel _channel = MethodChannel('com.ellaaicare.omi/guardian_mode');

  final _stateController = StreamController<GuardianModeState>.broadcast();
  Stream<GuardianModeState> get stateStream => _stateController.stream;

  GuardianModeState _currentState = GuardianModeState.idle;
  GuardianModeState get currentState => _currentState;

  Timer? _testAudioTimer;
  int _testClipCounter = 0;
  final FlutterTts _tts = FlutterTts();

  /// Start Guardian Mode
  Future<void> start() async {
    if (_currentState == GuardianModeState.active) {
      print('GuardianMode: Already active');
      return;
    }

    try {
      // Call iOS native to start silent loop
      final result = await _channel.invokeMethod('start');
      print('GuardianMode: Native start result: $result');

      _updateState(GuardianModeState.active);

      // Start test audio injection timer (every 30 seconds)
      _startTestAudioTimer();
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
    } catch (e) {
      print('GuardianMode: Error stopping: $e');
      _updateState(GuardianModeState.error);
    }
  }

  /// Start timer to inject test audio clips
  void _startTestAudioTimer() {
    _testClipCounter = 0;
    _testAudioTimer?.cancel();

    _testAudioTimer = Timer.periodic(const Duration(seconds: 30), (timer) async {
      _testClipCounter++;
      await _generateAndInjectTestClip(_testClipCounter);
    });

    // Inject first clip immediately
    _generateAndInjectTestClip(0);
  }

  /// Stop test audio timer
  void _stopTestAudioTimer() {
    _testAudioTimer?.cancel();
    _testAudioTimer = null;
    _testClipCounter = 0;
  }

  /// Generate test audio clip using TTS and inject it
  Future<void> _generateAndInjectTestClip(int clipNumber) async {
    try {
      final text = 'Guardian test number $clipNumber';
      print('GuardianMode: Generating test clip: $text');

      // Configure TTS
      await _tts.setLanguage('en-US');
      await _tts.setSpeechRate(0.5);
      await _tts.setVolume(1.0);

      // Generate audio file
      final tempDir = await getTemporaryDirectory();
      final audioPath = '${tempDir.path}/guardian_test_$clipNumber.wav';

      await _tts.synthesizeToFile(text, audioPath);
      print('GuardianMode: Generated TTS file: $audioPath');

      // Inject into native audio queue
      await _channel.invokeMethod('injectAudioClip', {'audioPath': audioPath});
      print('GuardianMode: Injected clip $clipNumber');
    } catch (e) {
      print('GuardianMode: Error generating test clip: $e');
    }
  }

  /// Update state and notify listeners
  void _updateState(GuardianModeState newState) {
    _currentState = newState;
    _stateController.add(newState);
  }

  /// Dispose resources
  void dispose() {
    _stopTestAudioTimer();
    _stateController.close();
  }
}
