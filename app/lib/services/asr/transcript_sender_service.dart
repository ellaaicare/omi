import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Service for sending on-device ASR transcript chunks to backend
/// Uses existing WebSocket connection to send text instead of audio
///
/// Based on backend spec: /backend/docs/EDGE_ASR_INTEGRATION_GUIDE.md
class TranscriptSenderService {
  WebSocketChannel? _webSocket;

  // Buffering for 600ms chunking
  String _textBuffer = '';
  Timer? _bufferTimer;
  static const _chunkInterval = Duration(milliseconds: 600);

  // Timestamp tracking
  double _segmentStartTime = 0.0;

  /// Set the WebSocket channel (should be same as audio WebSocket)
  void setWebSocket(WebSocketChannel webSocket) {
    _webSocket = webSocket;
  }

  /// Send a transcript chunk immediately (unbuffered)
  ///
  /// Use this for final transcripts or when you want immediate send
  void sendTranscriptChunk({
    required String text,
    String speaker = 'SPEAKER_00',
    double? start,
    double? end,
    bool isFinal = true,
    double confidence = 0.95,
    String? asrProvider, // 'apple_speech' for iOS Speech, 'deepgram' for cloud
  }) {
    if (_webSocket == null) {
      print('❌ [TranscriptSender] WebSocket not connected');
      return;
    }

    if (text.trim().isEmpty) {
      print('⚠️ [TranscriptSender] Empty text, skipping');
      return;
    }

    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
    final message = {
      'type': 'transcript_segment',
      'text': text.trim(),
      'speaker': speaker,
      'start': start ?? _segmentStartTime,
      'end': end ?? now,
      'is_final': isFinal,
      'confidence': confidence,
      if (asrProvider != null) 'asr_provider': asrProvider, // Optional source tagging
    };

    try {
      final jsonString = jsonEncode(message);
      _webSocket!.sink.add(jsonString);

      print('📤 [TranscriptSender] Sent: ${text.substring(0, text.length > 50 ? 50 : text.length)}... (${text.length} chars)');
    } catch (e) {
      print('❌ [TranscriptSender] Error sending: $e');
    }
  }

  /// Send a transcript chunk with 600ms buffering
  ///
  /// This buffers text and sends every 600ms or on final result
  /// Matches backend expectations for chunking frequency
  void sendTranscriptBuffered({
    required String text,
    required bool isFinal,
    double confidence = 0.95,
    String? asrProvider, // 'apple_speech' for iOS Speech, 'deepgram' for cloud
  }) {
    if (text.isEmpty) return;

    // Add to buffer
    if (_textBuffer.isEmpty) {
      _segmentStartTime = DateTime.now().millisecondsSinceEpoch / 1000.0;
    }
    _textBuffer += '$text ';

    // Cancel existing timer
    _bufferTimer?.cancel();

    if (isFinal) {
      // Send immediately for final results
      _flushBuffer(confidence: confidence, asrProvider: asrProvider);
    } else {
      // Buffer for 600ms for interim results
      _bufferTimer = Timer(_chunkInterval, () {
        _flushBuffer(confidence: confidence, asrProvider: asrProvider);
      });
    }
  }

  /// Flush the text buffer and send
  void _flushBuffer({double confidence = 0.95, String? asrProvider}) {
    if (_textBuffer.trim().isEmpty) return;

    final text = _textBuffer.trim();
    sendTranscriptChunk(
      text: text,
      isFinal: true,
      confidence: confidence,
      asrProvider: asrProvider,
    );

    _textBuffer = '';
  }

  /// Force flush any pending buffered text
  void flush() {
    _flushBuffer();
  }

  /// Clean up resources
  void dispose() {
    _bufferTimer?.cancel();
    _textBuffer = '';
  }
}
