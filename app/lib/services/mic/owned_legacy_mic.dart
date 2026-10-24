import 'dart:async';
import 'dart:typed_data';

import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/logger.dart';

/// An owner-scoped view over the legacy recorder. Its stop Future is the
/// foreground/background implementation's physical acknowledgement contract.
class OwnedLegacyMic implements IMicRecorderService {
  OwnedLegacyMic(
      {required this.inner,
      required this.source,
      required this.hasAuthority,
      this.onOwnedTail,
      AudioCaptureOwnership? ownership})
      : ownership = ownership ?? AudioCaptureOwnership.shared;

  final IMicRecorderService inner;
  final String source;
  final bool Function() hasAuthority;
  final void Function(Uint8List bytes)? onOwnedTail;
  final AudioCaptureOwnership ownership;
  AudioCaptureLease? _lease;

  AudioCaptureLease? get reservation => _lease;

  bool get hasReservation => _lease?.isOwner == true;
  bool get isCurrent => _lease?.isCurrent == true;

  void adoptReservation(AudioCaptureLease lease) {
    if (_lease?.isOwner == true || !lease.isCurrent || lease.hasAttemptedStart) {
      throw StateError('microphone cannot adopt reservation');
    }
    _lease = lease;
  }

  bool reserveBeforePermission() {
    if (_lease != null && !_lease!.isOwner) _lease = null;
    if (_lease != null) return false;
    _lease = ownership.reserve(source: source, hasAuthority: hasAuthority);
    return _lease != null;
  }

  @override
  Future<void> start(
      {required Function(Uint8List bytes) onByteReceived,
      Function()? onRecording,
      Function()? onStop,
      Function()? onInitializing}) {
    if (_lease == null && !reserveBeforePermission()) return Future.error(StateError('microphone is owned'));
    final lease = _lease!;
    return lease.start(
      start: () => inner.start(
        onByteReceived: (bytes) {
          if (lease.isCurrent) {
            onByteReceived(bytes);
          } else if (lease.mayRetainOwnedFrames) {
            onOwnedTail?.call(bytes);
          }
        },
        onRecording: () {
          if (lease.isCurrent) onRecording?.call();
        },
        onInitializing: () {
          if (lease.isCurrent) onInitializing?.call();
        },
        onStop: () {
          if (!lease.isCurrent || !identical(_lease, lease)) return;
          unawaited(lease.stop().then((_) {
            if (!identical(_lease, lease)) return;
            _lease = null;
            onStop?.call();
          }).catchError((Object error) {
            Logger.error('[OwnedMic] physical stop remains unconfirmed: $error');
          }));
        },
      ),
      stopAndConfirm: inner.stop,
    );
  }

  @override
  Future<void> stop() async {
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
  Future<void> stopForAccountTransition() => stop();

  @override
  void resumeAfterAccountTransition() {}
}
