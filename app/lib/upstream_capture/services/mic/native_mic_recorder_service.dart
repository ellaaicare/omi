import 'dart:async';
import 'dart:typed_data';

import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/services/mic/confirmed_mic_stop.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/mic/lease_scoped_owned_mic.dart';
import 'package:omi/services/mic/owned/owned_capture_controller.dart';
import 'package:omi/utils/logger.dart';

/// iOS/Android [IMicRecorderService] backed by the native PhoneMic module — an
/// AVAudioEngine capture layer with self-healing interruption and route-change
/// recovery.
///
/// Interruptions are NOT restarted from Dart: the native controller resumes
/// itself and this service only mirrors state into the existing callbacks
/// (`onInterruption(true/false)` for UI state, `onRecording` on resume). The
/// 3s stall watchdog — same constants as [MicRecorderService] — stays as the
/// outer safety net: CaptureController escalates a stall to stop()+start().
///
/// Session identity: each start()/startBatch() mints a new monotonically
/// increasing [_sessionId] and passes it to native. Every FlutterApi event
/// carries the id of the session it belongs to, and every handler drops events
/// whose id is not the current one — so a stale terminal `idle` from a
/// just-stopped session cannot clobber a fresh session, and a de-synced restart
/// converges: native adopts the new id on a start onto a still-live session and
/// re-emits its current state under it, so `running` reaches the new session even
/// without a fresh `starting`. stop() always forwards to native (killing any
/// orphaned session) and only runs local teardown once.
class NativeMicRecorderService
    implements IMicRecorderService, PhoneMicFlutterApi, ConfirmedMicStop, LeaseScopedOwnedMic {
  static const Duration _stallThreshold = Duration(seconds: 3);
  static const Duration _stallCheckInterval = Duration(seconds: 1);
  // Batch has no frames; liveness is the arrival of onBatchProgress (1Hz), which
  // keeps ticking through mutes/interruptions. A longer window than the stream
  // stall threshold tolerates the coarser cadence.
  static const Duration _batchStallThreshold = Duration(seconds: 10);

  final PhoneMicHostApi _hostApi;
  final Timer Function(Duration, void Function(Timer)) _periodic;
  final DateTime Function() _now;
  final OwnedCaptureController? _ownedController;

  Function(Uint8List bytes)? _onByteReceived;
  Function()? _onRecording;
  Function()? _onStop;
  Function()? _onInitializing;
  Function()? _onStalled;
  Function(bool began)? _onInterruption;
  Function()? _onBatchStalled;
  Function(String code, String message)? _onError;

  bool _sessionActive = false;
  Future<void>? _confirmedStopInFlight;
  Future<void>? _nativeStartInFlight;
  final Set<Future<void>> _stopReceipts = {};
  // Dart-minted session identity, bumped on every start()/startBatch(). Events
  // are dropped unless they carry this id (see the FlutterApi handlers below).
  int _sessionId = 0;
  bool _batchMode = false;
  bool _interrupted = false;
  DateTime? _lastByteAt;
  Timer? _stallTimer;
  bool _stallReported = false;
  DateTime? _lastBatchProgressAt;
  Timer? _batchStallTimer;
  bool _batchStallReported = false;

  NativeMicRecorderService({
    PhoneMicHostApi? hostApi,
    bool registerFlutterApi = true,
    DateTime Function() now = DateTime.now,
    Timer Function(Duration, void Function(Timer))? periodic,
    OwnedCaptureController? ownedController,
  })  : _hostApi = hostApi ?? PhoneMicHostApi(),
        _ownedController = ownedController,
        _now = now,
        _periodic = periodic ?? Timer.periodic {
    if (registerFlutterApi) {
      PhoneMicFlutterApi.setUp(this);
    }
  }

  @override
  bool get ownedStreamConfigured => _ownedController != null;
  @override
  void reserveOwnedStreamLease(AudioCaptureLease lease) {
    if (_sessionActive || _nativeStartInFlight != null || _confirmedStopInFlight != null)
      throw StateError('legacy native capture remains unsettled');
    _ownedController!.adoptLease(lease);
  }
  @override
  Future<void> startWithCaptureLease(
    AudioCaptureLease lease, {
    required void Function(Uint8List) onByteReceived,
    void Function()? onRecording,
    void Function()? onInitializing,
  }) {
    if (_sessionActive || _nativeStartInFlight != null || _confirmedStopInFlight != null)
      return Future.error(StateError('legacy native capture remains unsettled'));
    _batchMode = false;
    return _ownedController!.start(
      lease: lease,
      onByteReceived: onByteReceived,
      onRecording: onRecording,
      onInitializing: onInitializing,
    );
  }

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    Function(bool began)? onInterruption,
  }) async {
    final owned = _ownedController;
    if (owned != null) {
      if (_sessionActive || _nativeStartInFlight != null || _confirmedStopInFlight != null)
        throw StateError('legacy native capture remains unsettled');
      _batchMode = false;
      await owned.start(
        onByteReceived: (bytes) => onByteReceived(bytes),
        onRecording: onRecording,
        onInitializing: onInitializing,
      );
      return;
    }
    if (_confirmedStopInFlight == null && _nativeStartInFlight == null && _stopReceipts.isNotEmpty) {
      await Future.wait(_stopReceipts.toList()).timeout(const Duration(seconds: 10));
    }
    if (_confirmedStopInFlight != null || _nativeStartInFlight != null || _stopReceipts.isNotEmpty) {
      throw StateError('native microphone receipt pending');
    }
    _onByteReceived = onByteReceived;
    _onRecording = onRecording;
    _onStop = onStop;
    _onInitializing = onInitializing;
    _onStalled = onStalled;
    _onInterruption = onInterruption;
    _interrupted = false;
    _batchMode = false;
    _sessionActive = true;
    final sessionId = ++_sessionId;
    try {
      // Throws a PlatformException (permission_denied, session_config_failed,
      // format_invalid, converter_init_failed, engine_start_failed) instead of
      // recording silence.
      await _awaitNativeStart(PhoneMicCaptureMode.stream, sessionId);
    } catch (e) {
      if (_sessionId == sessionId) {
        _sessionActive = false;
        _cancelStallWatchdog();
        _clearCallbacks();
      }
      rethrow;
    }
  }

  @override
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  }) async {
    if (_ownedController?.hasReservation == true) throw StateError('owned stream prevents batch replacement');
    if (_confirmedStopInFlight == null && _nativeStartInFlight == null && _stopReceipts.isNotEmpty) {
      await Future.wait(_stopReceipts.toList()).timeout(const Duration(seconds: 10));
    }
    if (_confirmedStopInFlight != null || _nativeStartInFlight != null || _stopReceipts.isNotEmpty) {
      throw StateError('native microphone receipt pending');
    }
    _onStop = onStop;
    _onInterruption = onInterruption;
    _onBatchStalled = onBatchStalled;
    _onError = onError;
    _interrupted = false;
    _batchMode = true;
    _sessionActive = true;
    final sessionId = ++_sessionId;
    try {
      // Batch adds opus_init_failed / batch_dir_unavailable to the stream error
      // codes; the native writer stores .bin files instead of streaming frames.
      await _awaitNativeStart(PhoneMicCaptureMode.batch, sessionId);
    } catch (e) {
      if (_sessionId == sessionId) {
        _sessionActive = false;
        _batchMode = false;
        _cancelBatchWatchdog();
        _clearCallbacks();
      }
      rethrow;
    }
  }

  Future<void> _awaitNativeStart(PhoneMicCaptureMode mode, int sessionId) async {
    late final Future<void> receipt;
    receipt = _hostApi.start(mode, sessionId).whenComplete(() {
      if (identical(_nativeStartInFlight, receipt)) _nativeStartInFlight = null;
    });
    _nativeStartInFlight = receipt;
    try {
      await receipt.timeout(const Duration(seconds: 12));
      if (!_sessionActive || _sessionId != sessionId) throw StateError('native microphone start retired');
    } catch (_) {
      if (_sessionId == sessionId) stop();
      rethrow;
    }
  }

  @override
  void stop() {
    final owned = _ownedController;
    if (owned != null && !_batchMode) {
      if (owned.hasReservation) unawaited(owned.stop().catchError((Object error) {
        Logger.error('[NativeMic] owned drain held: ${error.runtimeType}');
      }));
      return;
    }
    // Always forward to native — even when Dart already thinks the session is
    // inactive. A de-synced Dart state must never leave a native session
    // capturing forever; native resolves harmlessly when it is already idle.
    late final Future<void> receipt;
    receipt = Future<void>.sync(() => _hostApi.stop()).catchError((Object error) {
      Logger.error('[NativeMic] native stop failed: $error');
    }).whenComplete(() => _stopReceipts.remove(receipt));
    _stopReceipts.add(receipt);
    // Local teardown (and onStop) run only once, on the transition out of an
    // active session — so a second stop() (or a stop() after a native idle) does
    // not double-fire callbacks.
    _retireLocalSession()?.call();
  }

  @override
  Future<void> stopAndConfirm() async {
    final owned = _ownedController;
    if (owned != null && !_batchMode) {
      if (owned.hasReservation) await owned.stop();
      return;
    }
    if (_confirmedStopInFlight != null) throw StateError('native microphone stop pending');
    final session = _sessionId;
    final onStop = _retireLocalSession();
    late final Future<void> stop;
    stop = (() async {
      // A timeout retires the Dart start waiter, not the native host request.
      // Confirm silence only after that exact request can no longer start late.
      final pendingStart = _nativeStartInFlight;
      if (pendingStart != null) {
        try {
          await pendingStart;
        } catch (_) {
          // A rejected start still requires an acknowledged physical stop.
        }
      }
      final pendingStops = _stopReceipts.toList(growable: false);
      if (pendingStops.isNotEmpty) await Future.wait(pendingStops);
      await _hostApi.stop();
      final recording = await _hostApi.isRecording();
      if (_sessionId != session || recording) throw StateError('native microphone stop not confirmed');
    })()
        .whenComplete(() {
      if (identical(_confirmedStopInFlight, stop)) _confirmedStopInFlight = null;
    });
    _confirmedStopInFlight = stop;
    await stop.timeout(const Duration(seconds: 10));
    onStop?.call();
  }

  Function()? _retireLocalSession() {
    if (!_sessionActive) return null;
    _sessionActive = false;
    _interrupted = false;
    _batchMode = false;
    _cancelStallWatchdog();
    _cancelBatchWatchdog();
    final onStop = _onStop;
    _clearCallbacks();
    return onStop;
  }

  // PhoneMicFlutterApi

  @override
  void onAudioFrame(Uint8List pcm16leMono16k, int sessionId) {
    if (sessionId != _sessionId) {
      Logger.debug('[NativeMic] dropping frame from session $sessionId (current $_sessionId)');
      return;
    }
    _lastByteAt = _now();
    _stallReported = false;
    _onByteReceived?.call(pcm16leMono16k);
  }

  @override
  void onStateChanged(PhoneMicCaptureState state, int sessionId) {
    // Session-identity gate: events from a previous (dead) session — most
    // dangerously a late `idle` acknowledging its own stop — carry that
    // session's id and are dropped here so they cannot clobber the current one.
    if (sessionId != _sessionId) {
      Logger.debug('[NativeMic] dropping $state from session $sessionId (current $_sessionId)');
      return;
    }
    // An event for the current id after Dart-side stop (e.g. the terminal idle
    // acknowledging our own stop()) is still ignored.
    if (!_sessionActive) return;
    switch (state) {
      case PhoneMicCaptureState.starting:
        _onInitializing?.call();
        break;
      case PhoneMicCaptureState.running:
        if (_interrupted) {
          _interrupted = false;
          _onInterruption?.call(false);
        }
        if (_batchMode) {
          // Batch emits no frames, so the stream stall watchdog would misfire.
          // Arm the batch (progress-arrival) watchdog once, on the first running.
          if (_batchStallTimer == null) _startBatchWatchdog();
        } else {
          // Armed here rather than in start(): the permission prompt can hold
          // start() open far longer than the stall threshold.
          _startStallWatchdog();
          _onRecording?.call();
        }
        break;
      case PhoneMicCaptureState.interrupted:
        if (!_interrupted) {
          _interrupted = true;
          _onInterruption?.call(true);
        }
        break;
      case PhoneMicCaptureState.rebuilding:
        // Frames pause briefly during a route/config rebuild — fresh grace
        // period so the watchdog doesn't misread it as a stall.
        _lastByteAt = _now();
        break;
      case PhoneMicCaptureState.idle:
        // Native-side terminal stop without a Dart stop() call.
        _sessionActive = false;
        _interrupted = false;
        _batchMode = false;
        _cancelStallWatchdog();
        _cancelBatchWatchdog();
        final onStop = _onStop;
        _clearCallbacks();
        onStop?.call();
        break;
    }
  }

  @override
  void onCaptureError(String code, String message, int sessionId) {
    if (sessionId != _sessionId) {
      Logger.debug('[NativeMic] dropping error $code from session $sessionId (current $_sessionId)');
      return;
    }
    // Native self-heals; the stall watchdog is the escalation path.
    Logger.error('[NativeMic] capture error $code: $message');
    // In batch there is no socket/UI listening for frames, so surface the code
    // (e.g. batch_storage_full) to the session so CaptureController can react.
    if (_batchMode) _onError?.call(code, message);
  }

  @override
  void onBatchProgress(double capturedSeconds, int sessionId) {
    if (sessionId != _sessionId) {
      Logger.debug('[NativeMic] dropping batch progress from session $sessionId (current $_sessionId)');
      return;
    }
    if (!_sessionActive || !_batchMode) return;
    // Arrival — not value — is the liveness signal: the value freezes during
    // mutes/interruptions while events keep coming.
    _lastBatchProgressAt = _now();
    _batchStallReported = false;
  }

  void _startStallWatchdog() {
    _cancelStallWatchdog();
    _lastByteAt = _now();
    _stallReported = false;
    _stallTimer = _periodic(_stallCheckInterval, (_) {
      if (_stallReported || _lastByteAt == null || _interrupted || !_sessionActive) return;
      if (_now().difference(_lastByteAt!) >= _stallThreshold) {
        _stallReported = true;
        _onStalled?.call();
      }
    });
  }

  void _cancelStallWatchdog() {
    _stallTimer?.cancel();
    _stallTimer = null;
    _lastByteAt = null;
    _stallReported = false;
  }

  void _startBatchWatchdog() {
    _cancelBatchWatchdog();
    _lastBatchProgressAt = _now();
    _batchStallReported = false;
    _batchStallTimer = _periodic(_stallCheckInterval, (_) {
      if (_batchStallReported || _lastBatchProgressAt == null || !_sessionActive) return;
      if (_now().difference(_lastBatchProgressAt!) >= _batchStallThreshold) {
        _batchStallReported = true;
        _onBatchStalled?.call();
      }
    });
  }

  void _cancelBatchWatchdog() {
    _batchStallTimer?.cancel();
    _batchStallTimer = null;
    _lastBatchProgressAt = null;
    _batchStallReported = false;
  }

  /// Soft-rearm liveness after the app returns to foreground (#4706).
  ///
  /// Wall-clock silence from timer suspension / screen lock must not force an
  /// immediate Dart stop→start (that races the native `appBecameActive` rebuild
  /// and also false-positives a healthy session). Reset the stall clock so the
  /// periodic watchdog owns escalation only if frames still don't arrive.
  @override
  void probeStallAfterForeground() {
    if (!_sessionActive || _interrupted) return;
    if (_batchMode) {
      _lastBatchProgressAt = _now();
      _batchStallReported = false;
      return;
    }
    _lastByteAt = _now();
    _stallReported = false;
  }

  void _clearCallbacks() {
    _onByteReceived = null;
    _onRecording = null;
    _onStop = null;
    _onInitializing = null;
    _onStalled = null;
    _onInterruption = null;
    _onBatchStalled = null;
    _onError = null;
  }
}
