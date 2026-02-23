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

  StreamController<GuardianModeState>? _stateController;
  Stream<GuardianModeState> get stateStream => 
      (_stateController ??= StreamController<GuardianModeState>.broadcast()).stream;

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
      await _channel.invokeMethod('start');
      print('GuardianMode: Native started');

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
      // Stop TTS
      await _tts.stop();
      
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

  /// Start timer to inject test audio clips
  void _startTestAudioTimer() {
    _testClipCounter = 0;
    _testAudioTimer?.cancel();

    _testAudioTimer = Timer.periodic(const Duration(seconds: 5), (timer) async {
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
    // Check if we're still in active state (prevents race conditions)
    if (_currentState != GuardianModeState.active) {
      print('GuardianMode: Skipping clip generation - not in active state');
      return;
    }

    try {
      final text = 'Guardian test number $clipNumber';
      print('GuardianMode: Generating test clip: $text');

      // Configure TTS
      await _tts.setLanguage('en-US');
      await _tts.setSpeechRate(0.5);
      await _tts.setVolume(1.0);

      // Generate audio file - flutter_tts only accepts filename, saves to Documents
      final fileName = 'guardian_test_$clipNumber.wav';
      await _tts.synthesizeToFile(text, fileName);

      // Get the actual path where flutter_tts saved it (Documents directory)
      final documentsDir = await getApplicationDocumentsDirectory();
      final audioPath = '${documentsDir.path}/$fileName';
      print('GuardianMode: Generated TTS file: $audioPath');

      // Validate that TTS file was created successfully (with retry for async file write)
      bool fileExists = false;
      for (int i = 0; i < 10; i++) {
        if (await File(audioPath).exists()) {
          fileExists = true;
          break;
        }
        await Future.delayed(const Duration(milliseconds: 100));
      }
      
      if (!fileExists) {
        print('GuardianMode: TTS file not created after 1 second: $audioPath');
        return;
      }

      // Inject into native audio queue (native will delete after playing)
      await _channel.invokeMethod('injectAudioClip', {'audioPath': audioPath});
      print('GuardianMode: Injected clip $clipNumber');
    } catch (e) {
      print('GuardianMode: Error generating test clip: $e');
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
    _stateController?.close();
    _stateController = null;
  }
}
