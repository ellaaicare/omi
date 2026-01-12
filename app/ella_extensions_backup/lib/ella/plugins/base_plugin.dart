/// Base plugin interface for all Ella extensions
///
/// All Ella plugins should extend this class to ensure consistent
/// lifecycle management and integration with OMI core.
///
/// Example:
/// ```dart
/// class MyPlugin extends EllaPlugin {
///   @override String get name => 'MyPlugin';
///   @override String get version => '1.0.0';
///
///   @override
///   Future<void> initialize() async {
///     // Setup code
///   }
/// }
/// ```
abstract class EllaPlugin {
  /// Plugin display name
  String get name;

  /// Plugin version (semver)
  String get version;

  /// Initialize the plugin
  /// Called once at app startup after OMI core is ready
  Future<void> initialize();

  /// Cleanup resources
  /// Called when app is terminating
  Future<void> dispose();

  /// Called when OMI core is fully ready
  /// Override to hook into OMI lifecycle
  void onOmiReady() {}

  /// Called when a transcript segment is received
  /// Override to process transcripts (e.g., wake word detection)
  void onTranscriptReceived(String text) {}

  /// Called when a conversation/recording starts
  void onConversationStarted() {}

  /// Called when a conversation/recording ends
  void onConversationEnded() {}

  /// Called when device connects via BLE
  void onDeviceConnected(String deviceId) {}

  /// Called when device disconnects
  void onDeviceDisconnected() {}

  /// Plugin status for debugging
  Map<String, dynamic> getStatus() {
    return {
      'name': name,
      'version': version,
    };
  }
}
