/// Ella Extensions - Single entry point for all Ella-specific functionality
///
/// This is the main integration point between OMI core and Ella extensions.
/// Initialize once at app startup AFTER OMI core is ready.
///
/// Usage in main.dart:
/// ```dart
/// import 'package:omi/ella/extensions.dart';
///
/// void main() async {
///   // ... OMI initialization ...
///
///   // Initialize Ella extensions
///   await EllaExtensions().initialize();
///
///   // Wire up wake word -> voice call
///   EllaExtensions().wakeWord.onWakeWordDetected = () {
///     EllaExtensions().voiceV2V.startCall();
///   };
///
///   runApp(MyApp());
/// }
/// ```
library ella_extensions;

import 'package:flutter/foundation.dart';

import 'plugins/base_plugin.dart';
import 'plugins/wake_word/wake_word_plugin.dart';
import 'plugins/voice_v2v/voice_v2v_plugin.dart';
import 'plugins/tts/ella_tts_plugin.dart';
import 'plugins/audio_push/audio_push_plugin.dart';
import 'config/ella_config.dart';

export 'plugins/base_plugin.dart';
export 'plugins/wake_word/wake_word_plugin.dart';
export 'plugins/voice_v2v/voice_v2v_plugin.dart';
export 'plugins/tts/ella_tts_plugin.dart';
export 'plugins/audio_push/audio_push_plugin.dart';
export 'config/ella_config.dart';

/// Main entry point for all Ella extensions
///
/// Singleton pattern ensures consistent state across the app.
class EllaExtensions {
  static final EllaExtensions _instance = EllaExtensions._internal();
  factory EllaExtensions() => _instance;
  EllaExtensions._internal();

  // Initialization state
  bool _initialized = false;
  bool get isInitialized => _initialized;

  // Plugins
  late final WakeWordPlugin wakeWord;
  late final VoiceV2VPlugin voiceV2V;
  late final EllaTtsPlugin tts;
  late final AudioPushPlugin audioPush;

  // All plugins for iteration
  List<EllaPlugin> get _allPlugins => [wakeWord, voiceV2V, tts, audioPush];

  /// Initialize all Ella extensions
  ///
  /// Call this once at app startup AFTER OMI core initializes.
  /// Safe to call multiple times - will only initialize once.
  Future<void> initialize() async {
    if (_initialized) {
      debugPrint('[EllaExtensions] Already initialized, skipping');
      return;
    }

    debugPrint('[EllaExtensions] Initializing Ella extensions...');

    // Load configuration
    await EllaConfig().load();

    // Create plugin instances
    wakeWord = WakeWordPlugin();
    voiceV2V = VoiceV2VPlugin();
    tts = EllaTtsPlugin();
    audioPush = AudioPushPlugin();

    // Initialize each plugin
    for (final plugin in _allPlugins) {
      try {
        debugPrint('[EllaExtensions] Initializing ${plugin.name} v${plugin.version}...');
        await plugin.initialize();
        debugPrint('[EllaExtensions] ${plugin.name} initialized');
      } catch (e) {
        debugPrint('[EllaExtensions] ERROR initializing ${plugin.name}: $e');
        // Continue with other plugins even if one fails
      }
    }

    // Setup default wiring: wake word triggers voice call
    _setupDefaultWiring();

    _initialized = true;
    debugPrint('[EllaExtensions] All extensions initialized');
  }

  /// Setup default plugin wiring
  void _setupDefaultWiring() {
    // Wake word detection starts a voice call
    wakeWord.onWakeWordDetected = () {
      debugPrint('[EllaExtensions] Wake word detected, starting voice call...');
      wakeWord.setInCall(true);
      voiceV2V.startCall().then((success) {
        if (!success) {
          wakeWord.setInCall(false);
        }
      });
    };

    // Voice call end re-enables wake word
    voiceV2V.onCallEnded = () {
      debugPrint('[EllaExtensions] Voice call ended, re-enabling wake word');
      wakeWord.setInCall(false);
    };
  }

  /// Notify all plugins that OMI core is ready
  void notifyOmiReady() {
    for (final plugin in _allPlugins) {
      plugin.onOmiReady();
    }
  }

  /// Forward transcript to all plugins (for wake word detection, etc.)
  void onTranscriptReceived(String text) {
    for (final plugin in _allPlugins) {
      plugin.onTranscriptReceived(text);
    }
  }

  /// Forward conversation start to all plugins
  void onConversationStarted() {
    for (final plugin in _allPlugins) {
      plugin.onConversationStarted();
    }
  }

  /// Forward conversation end to all plugins
  void onConversationEnded() {
    for (final plugin in _allPlugins) {
      plugin.onConversationEnded();
    }
  }

  /// Forward device connected to all plugins
  void onDeviceConnected(String deviceId) {
    for (final plugin in _allPlugins) {
      plugin.onDeviceConnected(deviceId);
    }
  }

  /// Forward device disconnected to all plugins
  void onDeviceDisconnected() {
    for (final plugin in _allPlugins) {
      plugin.onDeviceDisconnected();
    }
  }

  /// Get status of all plugins (for debugging)
  Map<String, dynamic> getStatus() {
    return {
      'initialized': _initialized,
      'config': EllaConfig().toJson(),
      'plugins': {
        for (final plugin in _allPlugins) plugin.name: plugin.getStatus(),
      },
    };
  }

  /// Cleanup all plugins
  Future<void> dispose() async {
    debugPrint('[EllaExtensions] Disposing all extensions...');

    for (final plugin in _allPlugins) {
      try {
        await plugin.dispose();
        debugPrint('[EllaExtensions] ${plugin.name} disposed');
      } catch (e) {
        debugPrint('[EllaExtensions] ERROR disposing ${plugin.name}: $e');
      }
    }

    _initialized = false;
    debugPrint('[EllaExtensions] All extensions disposed');
  }
}
