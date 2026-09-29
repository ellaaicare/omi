/// Engines the backend mode registry can run an experimental mode on.
enum ExperimentalModeEngine { omiRealtimeTemplate, hermes }

/// How an experimental mode's output reaches the user.
enum ExperimentalModeOutputChannel { nativePushNotification, whisperAudio }

/// Metadata for an experimental mode preview.
///
/// This mirrors the id/engine/output_channel fields of the backend mode
/// registry (companion PR, out of scope here). These modes remain unavailable
/// until the server provides authoritative activation and rate-limit support.
class ExperimentalMode {
  final String id;
  final ExperimentalModeEngine engine;
  final ExperimentalModeOutputChannel outputChannel;

  const ExperimentalMode({required this.id, required this.engine, required this.outputChannel});

  /// Static preview list matching the backend registry ids.
  ///
  /// Known follow-up: fetch this from the backend mode registry API once it
  /// exists on this branch, instead of hardcoding it here.
  static const List<ExperimentalMode> registry = [
    ExperimentalMode(
      id: 'einstein',
      engine: ExperimentalModeEngine.omiRealtimeTemplate,
      outputChannel: ExperimentalModeOutputChannel.nativePushNotification,
    ),
    ExperimentalMode(
      id: 'cyborg',
      engine: ExperimentalModeEngine.hermes,
      outputChannel: ExperimentalModeOutputChannel.nativePushNotification,
    ),
  ];
}
