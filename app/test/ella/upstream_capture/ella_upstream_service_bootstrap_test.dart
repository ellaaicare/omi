import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';

void main() {
  test('retries the production partial singleton initialization path after connectivity failure', () async {
    var singletonInstalled = false;
    var managerAttempts = 0;
    var connectivityAttempts = 0;
    final bootstrap = EllaUpstreamServicesBootstrap(
      initializeManager: () async {
        managerAttempts++;
        if (!singletonInstalled) {
          singletonInstalled = true;
          throw StateError('connectivity initialization failed after singleton install');
        }
        throw StateError('Service manager is initiated');
      },
      managerExists: () => singletonInstalled,
      initializeConnectivity: () async {
        connectivityAttempts++;
        if (connectivityAttempts == 1) throw StateError('connectivity still unavailable');
      },
    );

    await expectLater(bootstrap.ensureInitialized(), throwsStateError);
    await bootstrap.ensureInitialized();
    await bootstrap.ensureInitialized();
    expect(managerAttempts, 2);
    expect(connectivityAttempts, 2);
  });

  test('does not mask initialization failure before the singleton exists', () async {
    var connectivityAttempts = 0;
    final bootstrap = EllaUpstreamServicesBootstrap(
      initializeManager: () async => throw StateError('manager construction failed'),
      managerExists: () => false,
      initializeConnectivity: () async {
        connectivityAttempts++;
      },
    );
    await expectLater(bootstrap.ensureInitialized(), throwsStateError);
    expect(connectivityAttempts, 0);
  });
}
