// Concrete [EllaAudioRouter] for EllaUpstreamCaptureRuntime: routes gated
// audio frames to the existing /v4/listen backend contract via Ella's own
// TranscriptSegmentSocketService (services/sockets/transcription_service.dart
// — not upstream-owned). Fixes the codec per source per rule 3(c): opus for
// the necklace, pcm16 for the phone. This pass does not negotiate the codec
// dynamically and does not replicate capture_controller.dart's segment/
// conversation lifecycle handling (WAL, backpressure, reconnect-with-resume)
// — those stay a known gap tied to the god-object orchestration this pass
// does not promote (see UPSTREAM_OWNED.txt).
import 'dart:async';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/sockets/transcription_service.dart';

class EllaUpstreamSocketRouter {
  EllaUpstreamSocketRouter({this.language = 'en', this.source = 'ella_upstream_capture'});

  final String language;
  final String source;

  static const int necklaceSampleRate = 16000;
  static const int phoneSampleRate = 16000;

  TranscriptSegmentSocketService? _necklaceSocket;
  TranscriptSegmentSocketService? _phoneSocket;

  /// Matches [EllaAudioRouter]'s synchronous signature: connecting and
  /// sending both happen off this call, in order, per source.
  void route({required bool isPhone, required List<int> bytes}) {
    unawaited(_sendWhenConnected(isPhone: isPhone, bytes: bytes));
  }

  Future<void> _sendWhenConnected({required bool isPhone, required List<int> bytes}) async {
    final socket = await _socketFor(isPhone: isPhone);
    if (socket.state == SocketServiceState.connected) {
      await socket.send(bytes);
    }
  }

  Future<TranscriptSegmentSocketService> _socketFor({required bool isPhone}) async {
    if (isPhone) {
      final existing = _phoneSocket;
      if (existing != null && existing.state == SocketServiceState.connected) return existing;
      final socket = existing ??
          TranscriptSocketServiceFactory.createDefault(phoneSampleRate, BleAudioCodec.pcm16, language, source: source);
      _phoneSocket = socket;
      await socket.socket.connect();
      return socket;
    }
    final existing = _necklaceSocket;
    if (existing != null && existing.state == SocketServiceState.connected) return existing;
    final socket = existing ??
        TranscriptSocketServiceFactory.createDefault(necklaceSampleRate, BleAudioCodec.opus, language, source: source);
    _necklaceSocket = socket;
    await socket.socket.connect();
    return socket;
  }

  Future<void> close() async {
    await _necklaceSocket?.socket.disconnect();
    await _phoneSocket?.socket.disconnect();
    _necklaceSocket = null;
    _phoneSocket = null;
  }
}
