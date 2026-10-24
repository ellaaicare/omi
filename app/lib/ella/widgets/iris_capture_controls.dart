import 'package:flutter/foundation.dart';

enum IrisCaptureSource { phone, necklace }

enum IrisListeningState { unavailable, active, paused }

enum IrisWhisperPlayback { unknown, ready, unavailable, error }

/// A current capture lease and a pause capability are not live transport proof.
IrisListeningState projectIrisListening({
  required bool current,
  required bool paused,
  required bool protocolUnavailable,
  required bool transcriptReady,
  required bool captureRunning,
}) {
  if (!current) return IrisListeningState.unavailable;
  if (paused) return IrisListeningState.paused;
  return !protocolUnavailable && transcriptReady && captureRunning
      ? IrisListeningState.active
      : IrisListeningState.unavailable;
}

/// Presentation capabilities registered by the existing capture dock. These
/// callbacks retain its exact-owner and upstream pause/consent gates.
class IrisCaptureControls extends ChangeNotifier {
  static final shared = IrisCaptureControls();
  Object? _owner;
  Future<void> Function()? openStatus, openWhispers, openNecklace;
  IrisListeningState Function()? readListening;
  bool? Function()? readWhispers;
  IrisWhisperPlayback Function()? readWhisperPlayback;
  bool Function()? readNecklaceDisconnected;
  IrisCaptureSource? Function()? readCaptureSource;
  void register(Object owner,
      {required Future<void> Function() status,
      required Future<void> Function() whispers,
      required Future<void> Function() necklace,
      required IrisListeningState Function() listening,
      required bool? Function() whispersEnabled,
      IrisWhisperPlayback Function()? whisperPlayback,
      bool Function()? necklaceDisconnected,
      IrisCaptureSource? Function()? captureSource}) {
    _owner = owner;
    readCaptureSource = captureSource;
    openStatus = status;
    openWhispers = whispers;
    openNecklace = necklace;
    readListening = listening;
    readWhispers = whispersEnabled;
    readWhisperPlayback = whisperPlayback;
    readNecklaceDisconnected = necklaceDisconnected;
  }

  void unregister(Object owner) {
    if (!identical(owner, _owner)) return;
    _owner = null;
    readCaptureSource = null;
    openStatus = null;
    openWhispers = null;
    openNecklace = null;
    readListening = null;
    readWhispers = null;
    readWhisperPlayback = null;
    readNecklaceDisconnected = null;
  }

  bool get necklaceDisconnected => readNecklaceDisconnected?.call() ?? false;
  void changed() => notifyListeners();
  IrisListeningState get listening => readListening?.call() ?? IrisListeningState.unavailable;
}
