// Extracted verbatim from BasedHardware/omi @ 3219f05ced7fb518619abe92df856175b0a96985
// (app/lib/services/devices.dart): `DeviceConnectionState` and `OmiFeatures`
// only. Ella's own app/lib/services/devices.dart defines the same two names
// with different members (no `connecting` state; no upstream feature bits
// added after Ella forked) and is used pervasively by the legacy capture
// path via exhaustive switches Ella does not want this pass to touch. Rather
// than add members to that shared enum (which would make every existing
// exhaustive switch over it a compile error across the app), the promoted
// upstream_capture/ tree gets its own copy of just these two symbols. See
// UPSTREAM_PATCHES.md.
enum DeviceConnectionState { connected, connecting, disconnected }

/// Feature flags for Omi device capabilities.
/// Must match the firmware definitions in features.h.
class OmiFeatures {
  static const int speaker = 1 << 0;
  static const int accelerometer = 1 << 1;
  static const int button = 1 << 2;
  static const int battery = 1 << 3;
  static const int usb = 1 << 4;
  static const int haptic = 1 << 5;
  static const int offlineStorage = 1 << 6;
  static const int ledDimming = 1 << 7;
  static const int micGain = 1 << 8;
}
