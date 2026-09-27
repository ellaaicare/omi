// Extracted verbatim from BasedHardware/omi @ 3219f05ced7fb518619abe92df856175b0a96985
// (app/lib/services/services.dart, `IMicRecorderService`). Ella silos this one
// interface instead of promoting the whole services.dart orchestration hub,
// which pulls in upstream's own services/devices.dart, services/sockets.dart,
// services/wals.dart and connectivity_service.dart — a graph Ella does not
// carry. See UPSTREAM_PATCHES.md.
import 'dart:typed_data';

abstract class IMicRecorderService {
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    // Fired with began=true/false around an audio-session interruption. Only
    // NativeMicRecorderService emits it — capture resumes natively; Dart just
    // mirrors the state.
    Function(bool began)? onInterruption,
  });

  // Transcribe Later capture: audio is opus-encoded and written to WAL-compatible
  // .bin files natively (no onByteReceived — nothing streams to Dart). onBatchStalled
  // fires when the native liveness feed (onBatchProgress) goes silent; onError
  // forwards non-fatal native failures (e.g. batch_storage_full). Requires the native
  // recorder (`ServiceManager.phoneMic` on iOS/Android); the flutter_sound
  // implementations throw UnsupportedError.
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  });

  void stop();

  /// Soft-rearm frame/progress liveness after the app returns to foreground.
  /// iOS may suspend Dart timers while Stage Manager lets another app steal
  /// the mic (#4706). Must not immediately escalate — that races native rebuild
  /// and false-restarts healthy sessions. No-op on flutter_sound stacks.
  void probeStallAfterForeground();
}
