import 'dart:async';
import 'package:flutter/services.dart';
import 'package:omi/services/asr/transcript_sender_service.dart';
import 'package:omi/services/heuristics/heuristics_service.dart';

/// On-Device Automatic Speech Recognition Service
/// Uses iOS Speech framework for local transcription
/// Provides real-time local transcription without cloud upload
/// Automatically sends transcripts to backend via WebSocket
class OnDeviceASRService {
  static const MethodChannel _channel = MethodChannel('ella.ai/on_device_asr');

  /// Stream controller for transcript updates
  final _transcriptController = StreamController<TranscriptSegment>.broadcast();

  /// Stream of transcript segments
  Stream<TranscriptSegment> get transcriptStream => _transcriptController.stream;


  /// Transcript sender for backend integration
  TranscriptSenderService? _transcriptSender;

  /// Singleton instance
  static final OnDeviceASRService _instance = OnDeviceASRService._internal();

  factory OnDeviceASRService() => _instance;

  OnDeviceASRService._internal() {
    _channel.setMethodCallHandler(_handleMethodCall);
  }

  /// Set the transcript sender for backend integration
  /// Call this to enable automatic sending of transcripts to backend
  void setTranscriptSender(TranscriptSenderService sender) {
    _transcriptSender = sender;
    print('✅ [OnDeviceASR] TranscriptSender configured - transcripts will be sent to backend');
  }

  /// Handle method calls from native iOS
  Future<void> _handleMethodCall(MethodCall call) async {
    switch (call.method) {
      case 'onTranscript':
        final args = call.arguments as Map;
        final text = args['text'] as String;
        final isFinal = args['isFinal'] as bool;
        final confidence = args['confidence'] as double? ?? 0.95; // iOS can send confidence
        final source = args['source'] as String? ?? 'apple_speech'; // 'apple_speech' for iOS Speech framework

        // Create segment
        final segment = TranscriptSegment(
          text: text,
          isFinal: isFinal,
          timestamp: DateTime.now(),
          confidence: confidence,
          source: source,
        );

        // Emit to stream (for UI/debugging)
        _transcriptController.add(segment);

        // Scan for wake words (on-device heuristics)
        // HeuristicsService has built-in debounce to prevent false triggers
        if (text.trim().isNotEmpty) {
          HeuristicsService().scanForWakeWord(text);
        }

        // Send to backend if sender is configured
        if (_transcriptSender != null && text.trim().isNotEmpty) {
          _transcriptSender!.sendTranscriptBuffered(
            text: text,
            isFinal: isFinal,
            confidence: confidence,
            asrProvider: source, // Pass source as asr_provider for backend tagging
          );
        }
        break;

      case 'onError':
        final args = call.arguments as Map;
        final error = args['error'] as String;
        print('❌ [OnDeviceASR] Error: $error');
        _transcriptController.addError(error);
        break;

      default:
        print('⚠️ [OnDeviceASR] Unknown method: ${call.method}');
    }
  }

  /// Check if on-device ASR is available on this device
  Future<bool> isAvailable() async {
    try {
      final result = await _channel.invokeMethod<bool>('isAvailable');
      return result ?? false;
    } catch (e) {
      print('❌ [OnDeviceASR] isAvailable error: $e');
      return false;
    }
  }

  /// Check if device hardware is capable of efficient on-device ASR
  /// Returns true for iPhone 12+ (A14 chip or newer with Neural Engine)
  Future<bool> isDeviceCapable() async {
    try {
      final result = await _channel.invokeMethod<bool>('isDeviceCapable');
      return result ?? false;
    } catch (e) {
      print('❌ [OnDeviceASR] isDeviceCapable error: $e');
      return false;
    }
  }

  /// Get recommended ASR mode for this device
  /// Returns 'on_device' for capable devices (iPhone 12+)
  /// Returns 'cloud' for older devices (should use Deepgram)
  Future<String> getRecommendedMode() async {
    try {
      final result = await _channel.invokeMethod<String>('getRecommendedMode');
      return result ?? 'cloud';
    } catch (e) {
      print('❌ [OnDeviceASR] getRecommendedMode error: $e');
      return 'cloud';
    }
  }

  /// Request speech recognition authorization
  Future<bool> requestAuthorization() async {
    try {
      final result = await _channel.invokeMethod<bool>('requestAuthorization');
      return result ?? false;
    } catch (e) {
      print('❌ [OnDeviceASR] requestAuthorization error: $e');
      return false;
    }
  }

  /// Start on-device transcription
  Future<bool> startTranscription() async {
    try {
      print('🎙️ [OnDeviceASR] Starting on-device transcription...');
      final result = await _channel.invokeMethod<bool>('startTranscription');
      print('✅ [OnDeviceASR] Started: ${result ?? false}');
      return result ?? false;
    } catch (e) {
      print('❌ [OnDeviceASR] startTranscription error: $e');
      return false;
    }
  }

  /// Stop on-device transcription
  Future<bool> stopTranscription() async {
    try {
      print('🛑 [OnDeviceASR] Stopping on-device transcription...');
      final result = await _channel.invokeMethod<bool>('stopTranscription');
      print('✅ [OnDeviceASR] Stopped: ${result ?? false}');
      return result ?? false;
    } catch (e) {
      print('❌ [OnDeviceASR] stopTranscription error: $e');
      return false;
    }
  }


  /// Dispose resources
  void dispose() {
    _transcriptController.close();
  }
}

/// Represents a transcript segment from on-device ASR
class TranscriptSegment {
  final String text;
  final bool isFinal;
  final DateTime timestamp;
  final double confidence;
  final String source; // 'apple_speech' for iOS Speech framework

  TranscriptSegment({
    required this.text,
    required this.isFinal,
    required this.timestamp,
    this.confidence = 0.95,
    this.source = 'apple_speech',
  });

  @override
  String toString() {
    return 'TranscriptSegment(text: "$text", isFinal: $isFinal, confidence: $confidence, source: $source, timestamp: $timestamp)';
  }
}
