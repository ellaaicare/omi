import 'package:flutter_test/flutter_test.dart';

import 'package:omi/services/connectivity_service.dart';

void main() {
  group('ConnectivityService battery policy', () {
    test('uses a one-minute idle reachability interval', () {
      expect(
        ConnectivityService.idleCheckInterval,
        const Duration(seconds: 60),
      );
    });

    test('keeps reachability checks bounded by a short timeout', () {
      expect(ConnectivityService.checkTimeout, const Duration(seconds: 3));
    });
  });
}
