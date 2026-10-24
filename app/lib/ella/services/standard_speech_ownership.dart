import 'dart:async';

import 'package:flutter/services.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

/// The reviewed Darwin fork's physical receipt, independent of isListening.
abstract interface class StandardSpeechReceiptPort {
  Future<EllaSpeechCaptureProof> probe();
  Future<void> stop();
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation);
  void setCallback(void Function(Map<Object?, Object?>)? callback);
}

class StandardSpeechDrainReceipt {
  StandardSpeechDrainReceipt.fromMap(Map<Object?, Object?> map)
      : capture = EllaSpeechCaptureProof.fromMap(map),
        terminal = map['recognitionTerminal'] == true,
        finalResultJSON = map['finalResultJSON'] as String?;
  final EllaSpeechCaptureProof capture;
  final bool terminal;
  final String? finalResultJSON;
  bool provesComplete(int generation) => capture.provesCaptureStopped(generation) && !capture.taskRetained && terminal;
}

class DarwinStandardSpeechReceiptPort implements StandardSpeechReceiptPort {
  DarwinStandardSpeechReceiptPort({Future<void> Function()? stopSpeech}) : _stopSpeech = stopSpeech;
  final Future<void> Function()? _stopSpeech;
  static const _native = MethodChannel('plugin.csdcorp.com/speech_to_text');
  static const _callbacks = MethodChannel('plugin.csdcorp.com/speech_to_text/ella_ownership');
  static DarwinStandardSpeechReceiptPort? _callbackOwner;

  @override
  Future<EllaSpeechCaptureProof> probe() async {
    final value = await _native.invokeMapMethod<Object?, Object?>('ellaOwnershipProbe');
    if (value == null) throw StateError('speech ownership probe unavailable');
    return EllaSpeechCaptureProof.fromMap(value);
  }

  @override
  Future<void> stop() async {
    final stopSpeech = _stopSpeech;
    if (stopSpeech != null) return stopSpeech();
    await _native.invokeMethod<Object?>('stop');
  }

  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) async {
    final map = await _native.invokeMapMethod<Object?, Object?>('ellaStopAndDrain', {'generation': generation});
    if (map == null) throw StateError('speech recognition drain unavailable');
    final receipt = StandardSpeechDrainReceipt.fromMap(map);
    if (!receipt.provesComplete(generation)) throw StateError('speech recognition terminal unconfirmed');
    // Only after the new exact terminal receipt may the legacy Dart SDK stop
    // clear its own listener timers. Native no longer has this task or engine.
    final stopSpeech = _stopSpeech;
    if (stopSpeech != null) await stopSpeech();
    return receipt;
  }

  @override
  void setCallback(void Function(Map<Object?, Object?>)? callback) {
    if (callback == null) {
      if (!identical(_callbackOwner, this)) return;
      _callbackOwner = null;
      _callbacks.setMethodCallHandler(null);
      return;
    }
    _callbackOwner = this;
    _callbacks.setMethodCallHandler((call) async {
      if (!identical(_callbackOwner, this) || call.method != 'callback' || call.arguments is! Map) return;
      callback(Map<Object?, Object?>.from(call.arguments as Map));
    });
  }
}

/// One logical call owns permission, initialization, listen, playback and
/// restarts. Each recognition attempt has an exact child and native generation.
class StandardSpeechOwnership {
  StandardSpeechOwnership({required this.port, AudioCaptureOwnership? ownership})
      : _ownership = ownership ?? AudioCaptureOwnership.shared;

  final StandardSpeechReceiptPort port;
  final AudioCaptureOwnership _ownership;
  final EllaSpeechGenerationGate _gate = EllaSpeechGenerationGate();
  AudioCaptureLease? _parent;
  AudioCaptureTransport? _transport;
  int? _nativeGeneration;
  Timer? _pauseTimer;
  Timer? _listenTimer;
  Duration? _pauseFor;
  void Function()? _onListeningEnded;
  void Function()? _onStopUnconfirmed;
  void Function(String)? _onFinalResultRetained;
  bool _publicationRetired = false;
  bool _listenPending = false, _earlyStopFailed = false;
  AudioCaptureTransport? _earlyStopTransport;
  Future<void>? _earlyStopWork;

  bool get isCurrent => _parent?.isCurrent == true;
  bool get hasReservation => _parent?.isOwner == true;

  bool reserve({required bool Function() hasAuthority, required void Function(String, Object?) onCallback}) {
    if (_parent != null && !_parent!.isOwner) _parent = null;
    if (_parent != null) return _parent!.isCurrent;
    final parent = _ownership.reserve(source: 'standard_voice_call', hasAuthority: hasAuthority);
    if (parent == null) return false;
    _parent = parent;
    // An early owned native request may still contain SDK stop work. Never
    // release the logical caller while that exact request can finish late.
    parent.addStopAndConfirm(() async {
      final early = _earlyStopWork;
      if (early != null) await early;
    });
    _publicationRetired = false;
    port.setCallback((envelope) {
      if (!parent.isCurrent || _transport?.isCurrent != true || !_gate.accepts(envelope)) return;
      if (envelope['method'] == 'textRecognition') _restartPauseTimer();
      onCallback(envelope['method'] as String, envelope['payload']);
    });
    return true;
  }

  Future<bool> start({
    required Future<bool> Function() prepare,
    required Future<void> Function() listen,
    Duration? pauseFor,
    Duration? listenFor,
    void Function()? onListeningEnded,
    void Function()? onStopUnconfirmed,
    void Function(String)? onFinalResultRetained,
    void Function(Object)? onStartFailure,
  }) async {
    final parent = _parent;
    if (parent?.isCurrent != true) return false;
    // Replacing recognition stops only the exact old child, keeping the call.
    await stopListening();
    if (!parent!.isCurrent) return false;
    final transport = parent.createTransport();
    _transport = transport;
    _earlyStopTransport = null;
    _earlyStopWork = null;
    _earlyStopFailed = false;
    int? generation;
    var attemptedListen = false;
    var unownedCaptureObserved = false;
    var started = false;
    try {
      await transport.start(
        start: () async {
          // Unsupported/malformed probe fails before OS permissions or init.
          final before = await port.probe();
          if (!transport.isCurrent) return;
          if (before.engineRunning ||
              before.tapMayBeInstalled ||
              before.stopAmbiguous ||
              before.taskRetained ||
              (before.recognitionTaskCreated && !before.recognitionTerminal)) {
            unownedCaptureObserved = true;
            throw StateError('speech capture already active or ambiguous');
          }
          if (!before.recognitionDrainSupported) throw StateError('speech recognition drain unsupported');
          if (!await prepare() || !transport.isCurrent) return;
          generation = before.generation + 1;
          _nativeGeneration = generation;
          _onFinalResultRetained = onFinalResultRetained;
          _gate.activate(generation!);
          attemptedListen = true;
          _listenPending = true;
          try {
            await listen();
          } finally {
            _listenPending = false;
          }
          final after = await port.probe();
          if (after.generation != generation ||
              after.stopAmbiguous ||
              !after.engineRunning ||
              !after.tapMayBeInstalled ||
              !after.taskRetained) {
            throw StateError('speech listen generation unconfirmed');
          }
          started = transport.isCurrent;
          if (started) {
            _pauseFor = pauseFor;
            _onListeningEnded = onListeningEnded;
            _onStopUnconfirmed = onStopUnconfirmed;
            _restartPauseTimer();
            if (listenFor != null) _listenTimer = Timer(listenFor, () => _timeout(transport));
          }
        },
        stopAndConfirm: () async {
          _gate.retire();
          if (unownedCaptureObserved) throw StateError('unowned speech capture remains ambiguous');
          if (!attemptedListen) return;
          final current = await port.probe();
          if (current.generation == generation! - 1 &&
              !current.engineRunning &&
              !current.tapMayBeInstalled &&
              !current.stopAmbiguous &&
              !current.taskRetained) return;
          if (current.generation != generation) throw StateError('speech stop generation mismatch');
          final receipt = await port.stopAndDrain(generation!);
          if (!receipt.provesComplete(generation!)) throw StateError('speech recognition terminal unconfirmed');
          final stopped = await port.probe();
          if (!stopped.provesCaptureStopped(generation!) || stopped.taskRetained || !stopped.recognitionTerminal) {
            throw StateError('speech capture or recognition task stop unconfirmed');
          }
          // Local retained text reaches the same still-active page only after
          // terminal + physical acknowledgement, never a retired modal/account.
          final finalJSON = receipt.finalResultJSON;
          if (!_publicationRetired && parent.isCurrent && finalJSON != null) {
            _onFinalResultRetained?.call(finalJSON);
          }
        },
      );
      if (!started) await stopListening(expected: transport);
      return started;
    } catch (error) {
      try {
        onStartFailure?.call(error);
      } catch (_) {/* Observation never owns cleanup. */}
      _gate.retire();
      // Failed native start also needs its exact physical receipt. A failed
      // stop retains child/parent; no automatic replacement is admitted.
      await stopListening(expected: transport);
      rethrow;
    }
  }

  Future<void> stopListening({AudioCaptureTransport? expected}) async {
    final transport = expected ?? _transport;
    if (expected != null && !identical(expected, _transport)) return;
    _cancelTimers();
    _gate.retire();
    final stopping = transport?.stop() ?? Future<void>.value();
    final early = _requestEarlyOwnedStop(transport);
    // Both futures have error handlers before any await; early cleanup never
    // replaces the existing raw-start settlement and fresh terminal proof.
    await Future.wait<void>([stopping, early]);
    if (identical(_transport, transport)) {
      _transport = null;
      _nativeGeneration = null;
    }
  }

  Future<void> end() async {
    final parent = _parent;
    _publicationRetired = true;
    _gate.retire();
    _cancelTimers();
    final stopping = parent?.stop() ?? Future<void>.value();
    final early = _requestEarlyOwnedStop(_transport);
    await Future.wait<void>([stopping, early]);
    if (identical(_parent, parent)) {
      _parent = null;
      _transport = null;
      _nativeGeneration = null;
      port.setCallback(null);
    }
  }

  Future<void> _requestEarlyOwnedStop(AudioCaptureTransport? transport) {
    if (transport == null || !identical(_transport, transport) || _parent?.isOwner != true) return Future<void>.value();
    final existing = _earlyStopWork;
    if (identical(_earlyStopTransport, transport) && existing != null && !_earlyStopFailed) return existing;
    if (!_listenPending && !_earlyStopFailed) return Future<void>.value();
    final generation = _nativeGeneration;
    if (generation == null) return Future.error(StateError('speech pending generation unavailable'));
    final receipt = Completer<void>();
    _earlyStopTransport = transport;
    _earlyStopWork = receipt.future;
    _earlyStopFailed = false;
    Future<void>.sync(() async {
      final observed = await port.probe();
      // The planned generation alone is insufficient. The actual native probe
      // must identify this exact outstanding listen before any native stop.
      if (observed.generation != generation || !observed.recognitionDrainSupported)
        throw StateError('speech pending native identity unconfirmed');
      final drain = await port.stopAndDrain(generation);
      if (!drain.provesComplete(generation)) throw StateError('speech early terminal unconfirmed');
      final fresh = await port.probe();
      if (!fresh.provesCaptureStopped(generation) || fresh.taskRetained || !fresh.recognitionTerminal)
        throw StateError('speech early stop unconfirmed');
      // No final text publication here. The post-start child stop retains its
      // same-session terminal tail only after its own fresh final proof.
    }).then(receipt.complete, onError: (Object error, StackTrace stack) {
      _earlyStopFailed = true;
      receipt.completeError(error, stack);
    });
    return receipt.future;
  }

  int? get nativeGeneration => _nativeGeneration;

  void _restartPauseTimer() {
    _pauseTimer?.cancel();
    final transport = _transport;
    final pauseFor = _pauseFor;
    if (transport != null && pauseFor != null) _pauseTimer = Timer(pauseFor, () => _timeout(transport));
  }

  void _cancelTimers() {
    _pauseTimer?.cancel();
    _listenTimer?.cancel();
    _pauseTimer = _listenTimer = null;
    _pauseFor = null;
  }

  void _timeout(AudioCaptureTransport transport) {
    if (!identical(_transport, transport) || !transport.isCurrent) return;
    final onEnded = _onListeningEnded;
    unawaited(stopListening(expected: transport).then((_) {
      if (isCurrent && _transport == null) onEnded?.call();
    }).catchError((Object _) {
      // Retain ownership and suppress restart if physical stop was ambiguous.
      if (identical(_transport, transport)) _onStopUnconfirmed?.call();
    }));
  }
}
