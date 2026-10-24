import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/ella_gated_capture_seams.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  for (final sameUid in [false, true]) {
    test('stale unused preflight cannot revoke newer binding sameUID=$sameUid', () async {
      final directory = await Directory.systemTemp.createTemp('preflight_binding_');
      final h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
      final registry = AudioCaptureOwnership();
      try {
        expect(await h.bind(), true);
        final gated = EllaGatedMicRecorderService(
            ArbitratedMic(inner: h.mic, arbiter: MicArbiter(ownership: registry), owner: 'preflight'), h.authority);
        final old = gated.reserveCapturePreflight(hasAuthority: () => true)!;
        h.authority.release();
        if (!sameUid) h.switchAccount(accountB);
        if (!sameUid) grantEllaConsent(h.ellaPreferences, accountB);
        expect(h.authority.bind(sameUid ? accountA : accountB), isTrue);
        final epoch = h.authority.bindingEpoch;
        expect(old.isCurrent, isFalse);
        expect(h.authority.isBound, isTrue);
        expect(h.authority.hasCurrentAuthority, isTrue);
        expect(h.authority.bindingEpoch, epoch);
        expect(h.authority.framesAdmitted, 0, reason: 'permission/owner revalidation is not frame admission');
        expect(h.authority.framesDropped, 0);
        expect(await old.cancelBeforeStart(), isTrue);
        expect(h.authority.hasCurrentAuthority, isTrue);
      } finally {
        await h.dispose();
        if (directory.existsSync()) directory.deleteSync(recursive: true);
      }
    });
  }
}
