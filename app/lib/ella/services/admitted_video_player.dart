import 'dart:async';

import 'package:video_player/video_player.dart' as sdk;
import 'package:omi/ella/services/app_output_admission.dart';

export 'package:video_player/video_player.dart' hide VideoPlayerController;

class VideoPlayerController extends sdk.VideoPlayerController {
  VideoPlayerController.asset(String asset, {AppOutputAdmission? outputAdmission})
      : _admission = outputAdmission ?? AppOutputAdmission.shared,
        super.asset(asset);
  final AppOutputAdmission _admission;
  AppOutputPermit? _permit;
  Future<void>? _initialization;
  Future<void>? _disposal;
  final Set<Future<void>> _plays = {};
  bool _retiring = false;

  void _reserve() {
    if (_retiring) throw StateError('Video output retired');
    _permit ??= _admission.reserveOutput('onboarding-video');
  }

  @override
  Future<void> initialize() {
    _reserve();
    return _initialization ??= super.initialize();
  }

  @override
  Future<void> play() {
    _reserve();
    final settled = Completer<void>();
    _plays.add(settled.future);
    return super.play().whenComplete(() {
      _plays.remove(settled.future);
      settled.complete();
    });
  }

  @override
  Future<void> dispose() {
    if (_disposal != null) return _disposal!;
    _retiring = true;
    final receipt = Completer<void>();
    _disposal = receipt.future;
    () async {
      try {
        await _initialization;
      } catch (_) {
        // Initialization failure alone is not proof of native disposal.
      }
      await Future.wait(_plays.toList());
      await super.dispose();
      _permit?.releaseAfterConfirmedTeardown();
    }()
        .then(receipt.complete, onError: receipt.completeError);
    return receipt.future;
  }
}
