import 'dart:async';

import 'package:just_audio/just_audio.dart' as sdk;
import 'package:omi/ella/services/app_output_admission.dart';

export 'package:just_audio/just_audio.dart' hide AudioPlayer;

/// Keeps an exact output permit from the first setup through native disposal.
/// Failed disposal stays held: just_audio's second dispose is a local no-op.
class AudioPlayer extends sdk.AudioPlayer {
  AudioPlayer(
      {bool handleInterruptions = true, bool handleAudioSessionActivation = true, AppOutputAdmission? outputAdmission})
      : _admission = outputAdmission ?? AppOutputAdmission.shared,
        super(handleInterruptions: handleInterruptions, handleAudioSessionActivation: handleAudioSessionActivation);
  final AppOutputAdmission _admission;

  AppOutputPermit? _permit;
  bool _retiring = false;
  Future<void>? _disposal;
  final Set<Future<void>> _setup = {};
  final Set<Future<void>> _plays = {};

  void reserveOutput() {
    if (_retiring) throw StateError('Output player retired');
    _permit ??= _admission.reserveOutput('just_audio');
  }

  Future<T> _effect<T>(Future<T> Function() action, Set<Future<void>> pending) {
    reserveOutput();
    final settled = Completer<void>();
    pending.add(settled.future);
    return Future<T>.sync(action).whenComplete(() {
      pending.remove(settled.future);
      settled.complete();
    });
  }

  @override
  Future<Duration?> setAudioSource(sdk.AudioSource source,
          {bool preload = true, int? initialIndex, Duration? initialPosition}) =>
      _effect(
          () => super
              .setAudioSource(source, preload: preload, initialIndex: initialIndex, initialPosition: initialPosition),
          _setup);

  @override
  Future<Duration?> load() => _effect(super.load, _setup);

  @override
  Future<void> play() => _effect(super.play, _plays);

  @override
  Future<void> dispose() {
    final existing = _disposal;
    if (existing != null) return existing;
    _retiring = true;
    // Publish the same receipt before any reentrant native/observer work.
    final receipt = Completer<void>();
    _disposal = receipt.future;
    () async {
      await Future.wait(_setup.toList());
      // Prevent deferred play's post-AudioSession continuation from starting.
      // This pause is not itself proof of silence; disposal remains mandatory.
      await super.pause();
      await Future.wait(_plays.toList());
      await super.dispose();
      _permit?.releaseAfterConfirmedTeardown();
    }()
        .then(receipt.complete, onError: receipt.completeError);
    return receipt.future;
  }
}
