import 'dart:async';

bool _currentAuthority(bool Function() check) {
  try {
    return check();
  } catch (_) {
    return false;
  }
}

/// A synchronous reservation shared by capture sources and microphone stacks.
/// Labels are diagnostic only; authorization uses the exact lease object.
class AudioCaptureOwnership {
  static final shared = AudioCaptureOwnership();

  AudioCaptureLease? _owner;
  AudioCaptureLease? _successor;
  int _generation = 0;

  String? get source => _owner?.source;
  bool get isReserved => _owner != null;

  /// Pure closed state: deliberately does not invoke account authority callbacks.
  Map<String, bool> get dictationAdmissionStates {
    final owner = _owner;
    return Map.unmodifiable({
      'owner_present': owner != null,
      'successor_present': _successor != null,
      'owner_invalidated': owner?._invalidated ?? false,
      'owner_generation_matches': owner != null && owner._generation == _generation,
      'owner_start_attempted': owner?.hasAttemptedStart ?? false,
      'owner_stop_pending': owner?._stop != null,
      'owner_confirmation_outstanding': owner?._stopConfirmations.any((entry) => !entry.completed) ?? false,
    });
  }

  AudioCaptureLease? reserve({required String source, required bool Function() hasAuthority}) {
    if (_owner != null || _successor != null || !_currentAuthority(hasAuthority)) return null;
    final lease = AudioCaptureLease._(this, source, _generation, hasAuthority);
    _owner = lease;
    lease._admission.complete(true);
    return lease;
  }

  AudioCaptureLease? _reserveSuccessor(AudioCaptureLease previous, String source, bool Function() hasAuthority) {
    if (!identical(_owner, previous) || _successor != null || !_currentAuthority(hasAuthority)) return null;
    final lease = AudioCaptureLease._(this, source, _generation, hasAuthority);
    _successor = lease;
    return lease;
  }

  void _release(AudioCaptureLease previous) {
    if (!identical(_owner, previous)) return;
    final next = _successor;
    _successor = null;
    if (next != null &&
        !next._invalidated &&
        next._generation == _generation &&
        _currentAuthority(next._hasAuthority)) {
      _owner = next;
      next._admission.complete(true);
    } else {
      _owner = null;
      if (next != null && !next._admission.isCompleted) next._admission.complete(false);
    }
  }

  /// Invalidation suppresses callbacks synchronously. Physical ownership stays
  /// reserved until the exact owner obtains an acknowledgement from its stop.
  Future<void> quiesceForAccountTransition() {
    _generation++;
    final successor = _successor;
    _successor = null;
    if (successor != null) {
      successor._invalidated = true;
      successor._admission.complete(false);
    }
    return _owner?.stop() ?? Future<void>.value();
  }
}

class AudioCaptureLease {
  AudioCaptureLease._(this._registry, this.source, this._generation, this._hasAuthority);

  final AudioCaptureOwnership _registry;
  final String source;
  final int _generation;
  final bool Function() _hasAuthority;
  final Completer<bool> _admission = Completer<bool>();
  Future<void>? _start;
  Future<void>? _stop;
  Future<void> Function()? _stopAndConfirm;
  bool _invalidated = false;
  final List<AudioCaptureTransport> _transports = [];
  final List<_AudioStopConfirmation> _stopConfirmations = [];

  /// Register an exact lifetime resource before its first asynchronous setup.
  /// The callback must not recursively call stop. Failed confirmations retain
  /// this owner for retry; successful confirmations are not repeated.
  void addStopAndConfirm(Future<void> Function() stopAndConfirm) {
    if (_invalidated || _stop != null) throw StateError('audio owner teardown already began');
    _stopConfirmations.add(_AudioStopConfirmation(stopAndConfirm));
  }

  Future<bool> get admitted => _admission.future;

  /// The exact outgoing owner may queue one successor before asynchronous
  /// teardown. This does not grant capture until physical teardown succeeds.
  AudioCaptureLease? reserveSuccessor({required String source, required bool Function() hasAuthority}) =>
      _registry._reserveSuccessor(this, source, hasAuthority);

  bool get isOwner => identical(_registry._owner, this);
  bool get isCurrent =>
      isOwner && !_invalidated && _generation == _registry._generation && _currentAuthority(_hasAuthority);

  /// A same-account graceful stop may retain its exact local encoder tail.
  /// This never authorizes network/UI publication, replacement or account-lost frames.
  bool get mayRetainOwnedFrames => isOwner && _generation == _registry._generation && _currentAuthority(_hasAuthority);

  bool get hasAttemptedStart => _start != null || _transports.any((transport) => transport.hasAttemptedStart);

  /// A call retains this parent reservation during playback. Each microphone
  /// phase gets an exact child identity; stopping a child never releases the call.
  AudioCaptureTransport createTransport() {
    _transports.removeWhere((transport) => transport.hasStopped);
    if (!isCurrent || _start != null || _transports.any((transport) => !transport.hasStopped)) {
      throw StateError('audio transport cannot replace an unsettled owner');
    }
    final transport = AudioCaptureTransport._(this);
    _transports.add(transport);
    return transport;
  }

  /// Call only after permission and account revalidation. The stopper belongs
  /// to this start attempt, never to a global/unowned microphone.
  Future<void> start({required Future<void> Function() start, required Future<void> Function() stopAndConfirm}) {
    if (!isCurrent || _start != null || _transports.isNotEmpty) {
      return Future.error(StateError('audio owner cannot start'));
    }
    _stopAndConfirm = stopAndConfirm;
    // Publish the future before invoking native code or any synchronous callback.
    final receipt = Completer<void>();
    _start = receipt.future;
    Future<void>.sync(start).then(receipt.complete, onError: receipt.completeError);
    return receipt.future;
  }

  /// Idempotent exact-owner teardown. Failed or ambiguous receipts retain the
  /// reservation so replacement sources cannot overlap a possibly live engine.
  Future<void> stop() {
    if (!isOwner) {
      if (identical(_registry._successor, this)) {
        _invalidated = true;
        _registry._successor = null;
        if (!_admission.isCompleted) _admission.complete(false);
      }
      return Future<void>.value();
    }
    final pending = _stop;
    if (pending != null) return pending;
    _invalidated = true;
    final receipt = Completer<void>();
    _stop = receipt.future;
    () async {
      final start = _start;
      if (start != null) {
        try {
          await start;
        } catch (_) {
          // A thrown start does not establish physical silence.
        }
        await _stopAndConfirm!();
      }
      for (final transport in _transports) {
        await transport.stop();
      }
      for (final confirmation in _stopConfirmations) {
        if (confirmation.completed) continue;
        await confirmation.stopAndConfirm();
        confirmation.completed = true;
      }
      if (isOwner) _registry._release(this);
    }()
        .then(receipt.complete, onError: (Object error, StackTrace stack) {
      _stop = null; // Exact owner may retry its physical teardown.
      receipt.completeError(error, stack);
    });
    return receipt.future;
  }
}

class _AudioStopConfirmation {
  _AudioStopConfirmation(this.stopAndConfirm);
  final Future<void> Function() stopAndConfirm;
  bool completed = false;
}

class AudioCaptureTransport {
  AudioCaptureTransport._(this._parent);

  final AudioCaptureLease _parent;
  Future<void>? _start;
  Future<void>? _stop;
  Future<void> Function()? _stopAndConfirm;
  bool _invalidated = false;
  bool _stopped = false;

  bool get isCurrent => _parent.isCurrent && !_invalidated && !_stopped;
  bool get hasAttemptedStart => _start != null;
  bool get hasStopped => _stopped;

  Future<void> start({required Future<void> Function() start, required Future<void> Function() stopAndConfirm}) {
    if (!isCurrent || _start != null) return Future.error(StateError('audio transport cannot start'));
    _stopAndConfirm = stopAndConfirm;
    final receipt = Completer<void>();
    _start = receipt.future;
    Future<void>.sync(start).then(receipt.complete, onError: receipt.completeError);
    return receipt.future;
  }

  Future<void> stop() {
    if (_stopped || !_parent.isOwner) return Future<void>.value();
    final pending = _stop;
    if (pending != null) return pending;
    _invalidated = true;
    final receipt = Completer<void>();
    _stop = receipt.future;
    () async {
      final start = _start;
      if (start != null) {
        try {
          await start;
        } catch (_) {}
        await _stopAndConfirm!();
      }
      _stopAndConfirm = null;
      _stopped = true;
    }()
        .then(receipt.complete, onError: (Object error, StackTrace stack) {
      _stop = null;
      receipt.completeError(error, stack);
    });
    return receipt.future;
  }
}
