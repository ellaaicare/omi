// ellaaicare/ella-ai#1287 RUN-020: static regression coverage for the necklace-connect
// location prompt. capture_coordinator.dart (upstream-owned, patched — see
// UPSTREAM_PATCHES.md patch Six) used to construct one StartDeviceSessionStage with
// `promptLocation: device != null`, the one call site in the flag-ON necklace-connect
// graph that requested `Geolocator.requestPermission()` (an OS location prompt).
// Location is not part of Ella's consent model and is not needed for BLE, so every
// StartDeviceSessionStage construction must pass a literal `promptLocation: false`.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  group('Ella upstream capture: no location prompt on necklace connect', () {
    late String source;

    setUpAll(() {
      source = File('lib/upstream_capture/services/capture/capture_coordinator.dart').readAsStringSync();
    });

    test('capture_coordinator.dart never constructs StartDeviceSessionStage with a non-false promptLocation', () {
      // Matches only call sites (`deviceRequested: <expr>, promptLocation: <expr>`), never the
      // class's own `StartDeviceSessionStage({required this.deviceRequested, this.promptLocation
      // = false})` constructor declaration, which has no `deviceRequested:`/`promptLocation:`
      // named-argument colons to match.
      final constructions =
          RegExp(r'StartDeviceSessionStage\(\s*deviceRequested:\s*[^,]+,\s*promptLocation:\s*([^,)]+)\)')
              .allMatches(source)
              .toList();
      expect(constructions, isNotEmpty, reason: 'sanity: the pattern must match real constructions in this file');

      for (final construction in constructions) {
        expect(
          construction.group(1)!.trim(),
          'false',
          reason: 'necklace/device session start must never request an OS location prompt — location is not part of '
              "Ella's consent model and is not needed for BLE (see UPSTREAM_PATCHES.md patch Six): "
              '${construction.group(0)}',
        );
      }
    });

    test('regression: the pendant-session-start call site no longer derives promptLocation from device != null', () {
      expect(source, isNot(contains('promptLocation: device != null')));
    });
  });
}
