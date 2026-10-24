import 'dart:typed_data';
import 'audio_capture_ownership.dart';

/// Opt-in seam only. The arbiter passes its existing exact lease, so the
/// captured controller does not reserve or stop an unrelated second owner.
abstract interface class LeaseScopedOwnedMic {
  bool get ownedStreamConfigured;
  void reserveOwnedStreamLease(AudioCaptureLease lease);
  Future<void> startWithCaptureLease(
    AudioCaptureLease lease, {
    required void Function(Uint8List) onByteReceived,
    void Function()? onRecording,
    void Function()? onInitializing,
  });
}
