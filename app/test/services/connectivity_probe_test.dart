import 'package:flutter_test/flutter_test.dart';

import 'package:omi/services/connectivity_service.dart';

void main() {
  test('a 200 health response is reachable by itself', () {
    expect(healthStatusIsReachable(200), isTrue);
    expect(healthStatusIsReachable(503), isFalse);
  });

  test('recording a 200 probe does not clear or set the network-interface flag', () {
    final service = ConnectivityService();
    final before = service.isConnected;
    service.applyHealthProbeForTest(statusCode: 200);
    expect(service.backendReachable, isTrue);
    expect(service.lastBackendProbeStatus, 200);
    expect(service.isConnected, before);

    service.applyHealthProbeForTest(error: StateError('secondary host failed'));
    expect(service.backendReachable, isFalse);
    expect(service.isConnected, before);
  });
}
