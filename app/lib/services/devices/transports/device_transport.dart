import 'dart:async';

/// Abstract transport layer for device communication
/// Provides a unified interface for different communication protocols (BLE, WatchConnectivity, etc.)
abstract class DeviceTransport {
  String get deviceId;

  Future<void> connect();
  Future<void> disconnect();
  Future<bool> isConnected();
  Future<bool> ping();

  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid);

  /// Returns a characteristic stream only after its transport-level
  /// subscription is ready. Transports without an asynchronous subscription
  /// boundary can use the default implementation.
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async {
    return getCharacteristicStream(serviceUuid, characteristicUuid);
  }

  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid);
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data);

  Stream<DeviceTransportState> get connectionStateStream;

  Future<void> dispose();
}

enum DeviceTransportState {
  disconnected,
  connecting,
  connected,
  disconnecting,
}
