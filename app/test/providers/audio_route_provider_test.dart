import 'package:flutter_test/flutter_test.dart';

import 'package:omi/providers/audio_route_provider.dart';

void main() {
  test('parses a private Bluetooth audio route', () {
    final route = AudioRouteState.fromMap({
      'outputName': 'Ella headset',
      'outputType': 'BluetoothA2DPOutput',
      'hasHeadset': true,
      'usesPhoneSpeaker': false,
    });

    expect(route.outputName, 'Ella headset');
    expect(route.hasHeadset, isTrue);
    expect(route.usesPhoneSpeaker, isFalse);
  });

  test('defaults missing native route values conservatively', () {
    final route = AudioRouteState.fromMap(const {});

    expect(route.outputName, 'iPhone speaker');
    expect(route.hasHeadset, isFalse);
  });
}
