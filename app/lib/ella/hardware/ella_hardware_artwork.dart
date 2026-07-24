import 'package:omi/backend/schema/bt_device/bt_device.dart';

enum EllaHardwareArtworkState { on, off, reconnecting, lowBattery }

class EllaHardwareArtwork {
  EllaHardwareArtwork._();

  static const _assetRoot = 'assets/images/ella-hardware/png';

  static String? forDeviceType(DeviceType deviceType, EllaHardwareArtworkState state) {
    final prefix = switch (deviceType) {
      DeviceType.omi => 'necklace-omi',
      _ => null,
    };
    return prefix == null ? null : '$_assetRoot/$prefix-${_stateName(state)}@3x.png';
  }

  static String forWhisperHeadset(EllaHardwareArtworkState state) =>
      '$_assetRoot/headset-whisper-${_stateName(state)}@3x.png';

  static String? glyphForDeviceType(DeviceType deviceType) => switch (deviceType) {
        DeviceType.omi => '$_assetRoot/necklace-omi-glyph@3x.png',
        _ => null,
      };

  static const whisperHeadsetGlyph = '$_assetRoot/headset-whisper-glyph@3x.png';

  static String _stateName(EllaHardwareArtworkState state) => switch (state) {
        EllaHardwareArtworkState.on => 'on',
        EllaHardwareArtworkState.off => 'off',
        EllaHardwareArtworkState.reconnecting => 'reconnecting',
        EllaHardwareArtworkState.lowBattery => 'low-battery',
      };
}
