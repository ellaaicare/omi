import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/discovery/device_discoverer.dart';

class _BlockingDiscoverer extends DeviceDiscoverer {
  final Completer<void> started = Completer<void>();
  final Completer<DeviceDiscoveryResult> result = Completer<DeviceDiscoveryResult>();

  @override
  bool get isSupported => true;

  @override
  String get name => 'blocking';

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) {
    started.complete();
    return result.future;
  }

  @override
  Future<void> stop() async {}
}

void main() {
  test('explicit cancellation fences discovery before it can create a late connection', () async {
    final discoverer = _BlockingDiscoverer();
    var connectionCreations = 0;
    final service = DeviceService(
      discoverers: [discoverer],
      connectionCreator: (_) {
        connectionCreations++;
        return null;
      },
    )..start();

    final discovery = service.discover(desirableDeviceId: 'necklace-1');
    await discoverer.started.future;

    await service.cancelPendingConnection();
    discoverer.result.complete(
      DeviceDiscoveryResult(
        devices: [BtDevice(name: 'Friend', id: 'necklace-1', type: DeviceType.omi, rssi: -30)],
      ),
    );
    await discovery;

    expect(service.status, DeviceServiceStatus.ready);
    expect(connectionCreations, 0);
  });
}
