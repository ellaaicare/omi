import 'dart:async';

import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';
import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';

/// Atomically fences Dart producers, then asks the native Guardian coordinator
/// to reserve its autonomous spoken-media producers before input permission.
class NativeDictationOutputAdmission {
  NativeDictationOutputAdmission({
    AppOutputAdmission? admission,
    this.channel = const MethodChannel('com.ellaaicare.ella/guardian_mode'),
    this.deadline = const Duration(seconds: 10),
  }) : admission = admission ?? AppOutputAdmission.shared;
  final AppOutputAdmission admission;
  final MethodChannel channel;
  final Duration deadline;

  DictationOutputLease? tryReserve() {
    final dartLease = admission.tryReserveDictation();
    if (dartLease == null) return null;
    return _NativeOutputLease(dartLease, channel, const Uuid().v4(), deadline);
  }
}

final class NativeDictationOutputBusy extends DictationOutputBusy {
  const NativeDictationOutputBusy();
}

final class _NativeOutputLease implements DictationOutputLease {
  _NativeOutputLease(this.dartLease, this.channel, this.requestId, this.deadline);
  final DartDictationOutputPermit dartLease;
  final MethodChannel channel;
  final String requestId;
  final Duration deadline;
  Future<void>? _preparation, _preparationView, _release, _releaseView;
  bool _retiring = false, _failed = false, _attempted = false, _denied = false;
  bool _releaseConfirmed = false;
  @override
  bool get isCurrent => dartLease.isCurrent && !_retiring && !_failed;

  bool _receiptFlag(Map<String, dynamic>? receipt, String flag) {
    if (receipt == null ||
        receipt.length != 3 ||
        receipt['version'] is! int ||
        receipt['version'] != 1 ||
        receipt['request_id'] != requestId ||
        receipt[flag] is! bool) {
      throw StateError('Native output exclusion receipt unavailable');
    }
    return receipt[flag] as bool;
  }

  void _requireReceipt(Map<String, dynamic>? receipt, String flag) {
    if (!_receiptFlag(receipt, flag)) throw StateError('Native output exclusion not confirmed');
  }

  @override
  Future<void> prepareBeforePermissions() {
    if (_retiring || !dartLease.isCurrent) return Future.error(StateError('Output reservation retired'));
    final existing = _preparation;
    if (existing != null) return _preparationView!;
    final receipt = Completer<void>();
    _preparation = receipt.future;
    _preparationView = receipt.future.timeout(deadline, onTimeout: () {
      _failed = true;
      throw TimeoutException('Native output exclusion acknowledgement pending', deadline);
    });
    _attempted = true;
    () async {
      final arguments = {'request_id': requestId};
      final acquired =
          _receiptFlag(await channel.invokeMapMethod<String, dynamic>('dictationAcquireOutput', arguments), 'held');
      if (!acquired) {
        _denied = true; // Native terminal tombstone: no input effects were admitted.
        throw NativeDictationOutputBusy();
      }
      if (_retiring || _failed || !dartLease.isCurrent)
        throw StateError('Output reservation retired during acquisition');
      _requireReceipt(await channel.invokeMapMethod<String, dynamic>('dictationProofOutput', arguments), 'held');
      if (_retiring || !dartLease.isCurrent) throw StateError('Output reservation retired during proof');
    }()
        .then(receipt.complete, onError: (Object error, StackTrace stack) {
      _failed = true;
      receipt.completeError(error, stack);
    });
    // Deadline bounds the caller, not native work or the retained reservation.
    return _preparationView!;
  }

  @override
  Future<void> releaseAfterInputStopped() {
    _retiring = true;
    // The deadline never proves release. Only completed exact raw cleanup does.
    // Once proved, an explicit retry may observe success without a native call.
    if (_releaseConfirmed) return _release!;
    final existing = _release;
    if (existing != null) return _releaseView!;
    final receipt = Completer<void>();
    _release = receipt.future;
    _releaseView = receipt.future.timeout(deadline);
    () async {
      // Release first also tombstones an acquisition not yet delivered to
      // native. Still retain Dart exclusion until all admitted native work settles.
      if (_attempted && !_denied) {
        _requireReceipt(
            await channel.invokeMapMethod<String, dynamic>('dictationReleaseOutput', {'request_id': requestId}),
            'released');
      }
      try {
        await _preparation;
      } catch (_) {
        // Exact release/terminal denial, never a guessed quiet state.
      }
      await dartLease.releaseAfterInputStopped();
    }()
        .then((_) {
      _releaseConfirmed = true;
      receipt.complete();
    }, onError: (Object error, StackTrace stack) {
      // Exact retry can issue the same token again; never infer release.
      _release = null;
      _releaseView = null;
      receipt.completeError(error, stack);
    });
    return _releaseView!;
  }
}
