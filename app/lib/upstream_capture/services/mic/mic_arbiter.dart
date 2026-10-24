import 'dart:async';
import 'dart:typed_data';

import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/mic/confirmed_mic_stop.dart';
import 'package:omi/services/mic/mic_capture_preflight.dart';
import 'package:omi/services/mic/lease_scoped_owned_mic.dart';
import 'package:omi/utils/logger.dart';

/// Shared across recorder stacks. Diagnostic labels never authorize a release.
class MicArbiter {
  MicArbiter({AudioCaptureOwnership? ownership}) : ownership = ownership ?? AudioCaptureOwnership.shared;

  final AudioCaptureOwnership ownership;
  String? get owner => ownership.source;
}

/// Owns an exact reservation from permission through confirmed physical stop.
/// Failed starts and stop receipts retain ownership until an owned retry stops.
class ArbitratedMic implements IMicRecorderService, ConfirmedMicStop, PreflightMicCapture {
  final IMicRecorderService _inner;
  final MicArbiter _arbiter;
  final String _owner;
  AudioCaptureLease? _lease;

  ArbitratedMic({required IMicRecorderService inner, required MicArbiter arbiter, required String owner})
      : _inner = inner,
        _arbiter = arbiter,
        _owner = owner;

  /// High-level permission entrypoints may reserve before their first await.
  bool reserveBeforePermission({required bool Function() hasAuthority}) {
    if (_lease != null && !_lease!.isOwner) _lease = null;
    if (_lease != null) return false;
    _lease = _arbiter.ownership.reserve(source: _owner, hasAuthority: hasAuthority);
    final inner = _inner;
    if (_lease != null && inner is LeaseScopedOwnedMic && (inner as LeaseScopedOwnedMic).ownedStreamConfigured) {
      try {
        (inner as LeaseScopedOwnedMic).reserveOwnedStreamLease(_lease!);
      } catch (_) {
        final held = _lease!;
        unawaited(held.stop().then((_) {
          if (identical(_lease, held)) _lease = null;
        }).catchError((Object error) {
          Logger.error('[MicArbiter] owned preflight cleanup held: ${error.runtimeType}');
        }));
        return false;
      }
    }
    return _lease != null;
  }

  @override
  MicCapturePreflight? reserveCapturePreflight({required bool Function() hasAuthority}) {
    if (!reserveBeforePermission(hasAuthority: hasAuthority)) return null;
    return _ArbitratedPreflight(this, _lease!);
  }

  AudioCaptureLease _ownerForStart() {
    final existing = _lease;
    if (existing != null) {
      if (!existing.isCurrent || existing.hasAttemptedStart) throw StateError('microphone owner already started');
      return existing;
    }
    if (!reserveBeforePermission(hasAuthority: () => true)) {
      throw StateError('Microphone is busy (held by ${_arbiter.owner})');
    }
    return _lease!;
  }

  Future<void> _confirmInnerStop() async {
    final inner = _inner;
    if (inner is! ConfirmedMicStop) {
      // Best-effort teardown is owned, but cannot release the reservation.
      inner.stop();
      throw StateError('microphone stop acknowledgement unavailable');
    }
    await (inner as ConfirmedMicStop).stopAndConfirm();
  }

  void _naturalStop(AudioCaptureLease lease, Function()? onStop) {
    if (!identical(_lease, lease) || !lease.isCurrent) return;
    unawaited(lease.stop().then((_) {
      if (!identical(_lease, lease)) return;
      _lease = null;
      onStop?.call();
    }).catchError((Object error) {
      Logger.error('[MicArbiter] owned stop remains unconfirmed: $error');
    }));
  }

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    Function(bool began)? onInterruption,
  }) {
    final lease = _ownerForStart();
    final inner = _inner;
    if (inner is LeaseScopedOwnedMic && (inner as LeaseScopedOwnedMic).ownedStreamConfigured) {
      return (inner as LeaseScopedOwnedMic).startWithCaptureLease(lease, onByteReceived: (bytes) {
        if (lease.isCurrent) onByteReceived(bytes);
      }, onRecording: () {
        if (lease.isCurrent) onRecording?.call();
      }, onInitializing: () {
        if (lease.isCurrent) onInitializing?.call();
      });
    }
    return lease.start(
      start: () => _inner.start(
        onByteReceived: (bytes) {
          if (lease.isCurrent) onByteReceived(bytes);
        },
        onRecording: () {
          if (lease.isCurrent) onRecording?.call();
        },
        onStop: () => _naturalStop(lease, onStop),
        onInitializing: () {
          if (lease.isCurrent) onInitializing?.call();
        },
        onStalled: () {
          if (lease.isCurrent) onStalled?.call();
        },
        onInterruption: (began) {
          if (lease.isCurrent) onInterruption?.call(began);
        },
      ),
      stopAndConfirm: _confirmInnerStop,
    );
  }

  @override
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  }) {
    final lease = _ownerForStart();
    return lease.start(
      start: () => _inner.startBatch(
        onStop: () => _naturalStop(lease, onStop),
        onInterruption: (began) {
          if (lease.isCurrent) onInterruption?.call(began);
        },
        onBatchStalled: () {
          if (lease.isCurrent) onBatchStalled?.call();
        },
        onError: (code, message) {
          if (lease.isCurrent) onError?.call(code, message);
        },
      ),
      stopAndConfirm: _confirmInnerStop,
    );
  }

  @override
  void stop() {
    unawaited(stopAndConfirm().catchError((Object error) {
      Logger.error('[MicArbiter] owned stop remains unconfirmed: $error');
    }));
  }

  @override
  Future<void> stopAndConfirm() async {
    final lease = _lease;
    if (lease == null) return;
    if (!lease.isOwner) {
      if (identical(_lease, lease)) _lease = null;
      return;
    }
    await lease.stop();
    if (identical(_lease, lease)) _lease = null;
  }

  @override
  void probeStallAfterForeground() {
    if (_lease?.isCurrent == true) _inner.probeStallAfterForeground();
  }
}

class _ArbitratedPreflight implements MicCapturePreflight {
  _ArbitratedPreflight(this._mic, this._lease);
  final ArbitratedMic _mic;
  final AudioCaptureLease _lease;
  @override
  bool get isCurrent => identical(_mic._lease, _lease) && _lease.isCurrent;
  @override
  bool get hasAttemptedStart => _lease.hasAttemptedStart;
  @override
  Future<bool> cancelBeforeStart() async {
    if (!identical(_mic._lease, _lease) || !_lease.isOwner || _lease.hasAttemptedStart) return false;
    await _lease.stop();
    if (identical(_mic._lease, _lease)) _mic._lease = null;
    return true;
  }
}
