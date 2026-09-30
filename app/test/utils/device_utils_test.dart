import 'package:flutter_test/flutter_test.dart';

import 'package:omi/utils/device.dart';

void main() {
  group('DeviceUtils.shouldUpdateFirmware', () {
    test('short-circuits when current firmware is unknown', () async {
      final result = await DeviceUtils.shouldUpdateFirmware(
        currentFirmware: '',
        latestFirmwareDetails: {
          'version': '3.0.19',
          'draft': false,
          'min_version': '3.0.6',
        },
      );

      expect(result.$1, 'Unable to determine current firmware version');
      expect(result.$2, false);
      expect(result.$3, '');
    });
  });
}
