class BatteryActivityPolicy {
  static bool shouldStartPeriodicBleReconnect({
    required bool hasSavedDevice,
    required bool boundDeviceOnly,
    required bool pairingActive,
    required bool guardianActive,
    required bool isConnected,
    required bool hasConnectedDevice,
  }) {
    if (isConnected || hasConnectedDevice) {
      return false;
    }

    if (boundDeviceOnly && !hasSavedDevice) {
      return false;
    }

    return hasSavedDevice || pairingActive || guardianActive;
  }
}
