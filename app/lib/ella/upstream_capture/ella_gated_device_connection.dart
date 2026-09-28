import 'dart:async';

import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/devices.dart';
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';
import 'package:omi/upstream_capture/services/devices/transports/device_transport.dart';

/// Decorates upstream's `deviceConnectionLoader` seam so every connection the
/// capture controller receives is an [EllaGatedDeviceConnection].
Future<DeviceConnection?> Function(String deviceId) ellaGatedDeviceConnectionLoader(
  Future<DeviceConnection?> Function(String deviceId) inner,
  EllaCaptureAuthority authority,
) {
  return (deviceId) async {
    final connection = await inner(deviceId);
    if (connection == null) return null;
    if (connection is EllaGatedDeviceConnection) return connection;
    return EllaGatedDeviceConnection(connection, authority);
  };
}

/// Necklace (BLE pendant) audio gate over an upstream [DeviceConnection].
///
/// Upstream's controller subscribes to pendant audio exclusively through the
/// connection returned by its `deviceConnectionLoader` seam
/// (`getBleAudioBytesListener`). This decorator wraps that callback so every
/// BLE audio packet is checked with [EllaCaptureAuthority.admitsFrame] BEFORE
/// upstream processes it into its WAL copy, voice-command buffer, or socket.
/// Every other member forwards unchanged to the real upstream connection.
///
/// Known limitation: code that type-tests the concrete connection class (the
/// controller's Limitless batch-mode toggle, `connection is
/// LimitlessDeviceConnection`) sees the decorator instead; Ella's necklace is
/// an Omi device, so that Limitless-only branch is not exercised.
class EllaGatedDeviceConnection implements DeviceConnection {
  EllaGatedDeviceConnection(this.inner, this._authority);

  final DeviceConnection inner;
  final EllaCaptureAuthority _authority;
  int _droppedAudioPackets = 0;

  int get droppedAudioPackets => _droppedAudioPackets;

  void Function(List<int>) _gate(void Function(List<int>) onAudioBytesReceived) {
    return (bytes) {
      if (!_authority.admitsFrame()) {
        _droppedAudioPackets++;
        return;
      }
      onAudioBytesReceived(bytes);
    };
  }

  @override
  Future<StreamSubscription?> getBleAudioBytesListener({required void Function(List<int>) onAudioBytesReceived}) =>
      inner.getBleAudioBytesListener(onAudioBytesReceived: _gate(onAudioBytesReceived));

  @override
  Future<StreamSubscription?> performGetBleAudioBytesListener({
    required void Function(List<int>) onAudioBytesReceived,
  }) =>
      inner.performGetBleAudioBytesListener(onAudioBytesReceived: _gate(onAudioBytesReceived));

  // ---- Everything below forwards unchanged to the upstream connection. ----

  @override
  BtDevice get device => inner.device;

  @override
  set device(BtDevice value) => inner.device = value;

  @override
  DeviceTransport get transport => inner.transport;

  @override
  set transport(DeviceTransport value) => inner.transport = value;

  @override
  DeviceConnectionState get status => inner.status;

  @override
  DeviceConnectionState get connectionState => inner.connectionState;

  @override
  set connectionState(DeviceConnectionState state) => inner.connectionState = state;

  @override
  DateTime? get pongAt => inner.pongAt;

  @override
  Future<void> connect({void Function(String deviceId, DeviceConnectionState state)? onConnectionStateChanged}) =>
      inner.connect(onConnectionStateChanged: onConnectionStateChanged);

  @override
  Future<void> disconnect() => inner.disconnect();

  @override
  Future<void> unpair() => inner.unpair();

  @override
  Future<bool> ping() => inner.ping();

  @override
  void read() => inner.read();

  @override
  void write() => inner.write();

  @override
  Future<bool> isConnected() => inner.isConnected();

  @override
  Future<int> retrieveBatteryLevel() => inner.retrieveBatteryLevel();

  @override
  Future<int> performRetrieveBatteryLevel() => inner.performRetrieveBatteryLevel();

  @override
  Future<StreamSubscription<List<int>>?> getBleBatteryLevelListener({void Function(int)? onBatteryLevelChange}) =>
      inner.getBleBatteryLevelListener(onBatteryLevelChange: onBatteryLevelChange);

  @override
  Future<StreamSubscription<List<int>>?> performGetBleBatteryLevelListener(
          {void Function(int)? onBatteryLevelChange}) =>
      inner.performGetBleBatteryLevelListener(onBatteryLevelChange: onBatteryLevelChange);

  @override
  Future<List<int>> getBleButtonState() => inner.getBleButtonState();

  @override
  Future<List<int>> performGetButtonState() => inner.performGetButtonState();

  @override
  Future<StreamSubscription?> getBleButtonListener({required void Function(List<int>) onButtonReceived}) =>
      inner.getBleButtonListener(onButtonReceived: onButtonReceived);

  @override
  Future<StreamSubscription?> performGetBleButtonListener({required void Function(List<int>) onButtonReceived}) =>
      inner.performGetBleButtonListener(onButtonReceived: onButtonReceived);

  @override
  Future<BleAudioCodec> getAudioCodec() => inner.getAudioCodec();

  @override
  Future<BleAudioCodec> performGetAudioCodec() => inner.performGetAudioCodec();

  @override
  Future<bool> playFindDevicePattern() => inner.playFindDevicePattern();

  @override
  Future<bool> performPlayToSpeakerHaptic(int mode) => inner.performPlayToSpeakerHaptic(mode);

  @override
  Future<StorageStatus?> getStorageFileStats() => inner.getStorageFileStats();

  @override
  Future<StorageStatus?> performGetStorageFileStats() => inner.performGetStorageFileStats();

  @override
  Future<List<StorageFileInfo>> listStorageFiles() => inner.listStorageFiles();

  @override
  Future<List<StorageFileInfo>> performListStorageFiles() => inner.performListStorageFiles();

  @override
  Future<bool> deleteStorageFile(int fileIndex) => inner.deleteStorageFile(fileIndex);

  @override
  Future<bool> performDeleteStorageFile(int fileIndex) => inner.performDeleteStorageFile(fileIndex);

  @override
  Future<bool> stopStorageSync() => inner.stopStorageSync();

  @override
  Future<bool> performStopStorageSync() => inner.performStopStorageSync();

  @override
  Future<RingStatus?> getRingStatus() => inner.getRingStatus();

  @override
  Future<RingStatus?> performGetRingStatus() => inner.performGetRingStatus();

  @override
  Future<RingInfo?> getRingInfo() => inner.getRingInfo();

  @override
  Future<RingInfo?> performGetRingInfo() => inner.performGetRingInfo();

  @override
  Future<bool> readRingFromSeq(int startSeq, {int? packetCount}) =>
      inner.readRingFromSeq(startSeq, packetCount: packetCount);

  @override
  Future<bool> performReadRingFromSeq(int startSeq, {int? packetCount}) =>
      inner.performReadRingFromSeq(startSeq, packetCount: packetCount);

  @override
  Future<bool> advanceRing(int newReadSeq) => inner.advanceRing(newReadSeq);

  @override
  Future<bool> performAdvanceRing(int newReadSeq) => inner.performAdvanceRing(newReadSeq);

  @override
  Future<bool> clearRing() => inner.clearRing();

  @override
  Future<bool> performClearRing() => inner.performClearRing();

  @override
  Future<List<int>> getStorageList() => inner.getStorageList();

  @override
  Future<List<int>> performGetStorageList() => inner.performGetStorageList();

  @override
  Future<bool> performWriteToStorage(int numFile, int command, int offset) =>
      inner.performWriteToStorage(numFile, command, offset);

  @override
  Future<bool> writeToStorage(int numFile, int command, int offset) => inner.writeToStorage(numFile, command, offset);

  @override
  Future<StreamSubscription?> getBleStorageBytesListener({required void Function(List<int>) onStorageBytesReceived}) =>
      inner.getBleStorageBytesListener(onStorageBytesReceived: onStorageBytesReceived);

  @override
  Future<StreamSubscription?> performGetBleStorageBytesListener({
    required void Function(List<int>) onStorageBytesReceived,
  }) =>
      inner.performGetBleStorageBytesListener(onStorageBytesReceived: onStorageBytesReceived);

  @override
  Future cameraStartPhotoController() => inner.cameraStartPhotoController();

  @override
  Future performCameraStartPhotoController() => inner.performCameraStartPhotoController();

  @override
  Future cameraStopPhotoController() => inner.cameraStopPhotoController();

  @override
  Future performCameraStopPhotoController() => inner.performCameraStopPhotoController();

  @override
  Future<bool> hasPhotoStreamingCharacteristic() => inner.hasPhotoStreamingCharacteristic();

  @override
  Future<bool> performHasPhotoStreamingCharacteristic() => inner.performHasPhotoStreamingCharacteristic();

  @override
  Future<StreamSubscription?> getImageListener({required void Function(OrientedImage orientedImage) onImageReceived}) =>
      inner.getImageListener(onImageReceived: onImageReceived);

  @override
  Future<StreamSubscription?> performGetImageListener({
    required void Function(OrientedImage orientedImage) onImageReceived,
  }) =>
      inner.performGetImageListener(onImageReceived: onImageReceived);

  @override
  Future<StreamSubscription<List<int>>?> getAccelListener({void Function(int)? onAccelChange}) =>
      inner.getAccelListener(onAccelChange: onAccelChange);

  @override
  Future<StreamSubscription<List<int>>?> performGetAccelListener({void Function(int)? onAccelChange}) =>
      inner.performGetAccelListener(onAccelChange: onAccelChange);

  @override
  Future<int> getFeatures() => inner.getFeatures();

  @override
  Future<int> performGetFeatures() => inner.performGetFeatures();

  @override
  Future<void> setLedDimRatio(int ratio) => inner.setLedDimRatio(ratio);

  @override
  Future<void> performSetLedDimRatio(int ratio) => inner.performSetLedDimRatio(ratio);

  @override
  Future<int?> getLedDimRatio() => inner.getLedDimRatio();

  @override
  Future<int?> performGetLedDimRatio() => inner.performGetLedDimRatio();

  @override
  Future<void> setMicGain(int gain) => inner.setMicGain(gain);

  @override
  Future<void> performSetMicGain(int gain) => inner.performSetMicGain(gain);

  @override
  Future<int?> getMicGain() => inner.getMicGain();

  @override
  Future<int?> performGetMicGain() => inner.performGetMicGain();

  @override
  Future<void> onNetworkSocketReconnected() => inner.onNetworkSocketReconnected();
}
