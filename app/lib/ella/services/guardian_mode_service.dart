import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef GuardianWhisperAuthorityProvider = ExactAccountAuthorityVerifier? Function();
typedef GuardianWhisperSnapshot = ({bool enabled, bool modeVerified, bool nativeReconciled});

class GuardianWhisperOperation {
  GuardianWhisperOperation._(this._fence, this.revision, this.authority);

  final GuardianWhisperStateFence _fence;
  final int revision;
  final ExactAccountAuthorityVerifier authority;
  bool get isCurrent => revision == _fence.revision && authority.isExactCurrent();
}

/// Ordering and authority fence for the existing native owner, not a second
/// playback service. Reads remain concurrent; native work and mode writes do not.
class GuardianWhisperStateFence extends ChangeNotifier {
  GuardianWhisperStateFence() {
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(invalidate);
  }

  int revision = 0;
  bool choicePending = false;
  GuardianWhisperOperation? _snapshotOperation;
  GuardianWhisperSnapshot? _snapshot;
  Future<void>? _inFlight;
  bool _accountTransitionPending = false;
  ExactAccountAuthorityVerifier? _outgoingAuthority;
  int? _outgoingAuthorityGeneration;

  GuardianWhisperSnapshot? get snapshot => _snapshotOperation?.isCurrent == true ? _snapshot : null;

  GuardianWhisperOperation? observe(GuardianWhisperAuthorityProvider authorityProvider) {
    final authority = authorityProvider();
    if (authority == null || !_admitAuthority(authority) || choicePending) return null;
    return GuardianWhisperOperation._(this, revision, authority);
  }

  GuardianWhisperOperation? choose(GuardianWhisperAuthorityProvider authorityProvider, bool enabled) {
    final authority = authorityProvider();
    if (authority == null || !_admitAuthority(authority)) return null;
    final operation = GuardianWhisperOperation._(this, ++revision, authority);
    choicePending = true;
    _snapshotOperation = operation;
    _snapshot = (enabled: enabled, modeVerified: false, nativeReconciled: false);
    notifyListeners();
    return operation;
  }

  void publish(GuardianWhisperOperation operation, GuardianWhisperSnapshot snapshot) {
    if (!operation.isCurrent) return;
    choicePending = false;
    _snapshotOperation = operation;
    _snapshot = snapshot;
    notifyListeners();
  }

  void abandon(GuardianWhisperOperation operation) {
    if (choicePending && !_accountTransitionPending && operation.isCurrent) invalidate();
  }

  void invalidate() {
    revision++;
    choicePending = _accountTransitionPending;
    _snapshotOperation = null;
    _snapshot = null;
    notifyListeners();
  }

  bool _admitAuthority(ExactAccountAuthorityVerifier authority) {
    if (!authority.isExactCurrent()) return false;
    if (!_accountTransitionPending) return true;
    final outgoing = _outgoingAuthority;
    if (outgoing != null
        ? outgoing.isExactCurrent()
        : SharedPreferencesUtil().aiConsentAuthorityGeneration == _outgoingAuthorityGeneration) {
      return false;
    }
    _accountTransitionPending = false;
    _outgoingAuthority = null;
    _outgoingAuthorityGeneration = null;
    choicePending = false;
    return true;
  }

  @visibleForTesting
  void resetForTesting() {
    _accountTransitionPending = false;
    _outgoingAuthority = null;
    _outgoingAuthorityGeneration = null;
    invalidate();
  }

  Future<T?> serialize<T>(GuardianWhisperOperation operation, Future<T> Function() action) {
    Future<T?> run() async {
      if (!operation.isCurrent) return null;
      return action();
    }

    final result = _inFlight?.then<T?>((_) => run()) ?? run();
    _track(result);
    return result;
  }

  Future<void> stopAfterInFlight(
    Future<void> Function() stop, {
    GuardianWhisperAuthorityProvider authorityProvider = WalOwnerAuthority.active,
  }) {
    _outgoingAuthority = authorityProvider();
    _outgoingAuthorityGeneration = SharedPreferencesUtil().aiConsentAuthorityGeneration;
    _accountTransitionPending = true;
    invalidate();
    final result = _inFlight?.then((_) => stop()) ?? stop();
    _track(result);
    return result;
  }

  void _track(Future<Object?> result) {
    final tail = result.then<void>((_) {}, onError: (Object error, StackTrace stack) {});
    _inFlight = tail;
    tail.then((_) {
      if (identical(_inFlight, tail)) _inFlight = null;
    });
  }

  @override
  void dispose() {
    SharedPreferencesUtil.aiConsentAuthorityChanges.removeListener(invalidate);
    super.dispose();
  }
}

enum GuardianModeState {
  idle,
  active,
  error,
}

class GuardianModeService {
  static final GuardianModeService _instance = GuardianModeService._internal();
  factory GuardianModeService() => _instance;
  GuardianModeService._internal();
  static final whisperStateFence = GuardianWhisperStateFence();

  static const MethodChannel _channel = MethodChannel('com.ellaaicare.ella/guardian_mode');

  StreamController<GuardianModeState>? _stateController;
  Stream<GuardianModeState> get stateStream =>
      (_stateController ??= StreamController<GuardianModeState>.broadcast()).stream;

  GuardianModeState _currentState = GuardianModeState.idle;
  GuardianModeState get currentState => _currentState;
  bool get isAvailable => allowsGuardianSurface();

  Timer? _testAudioTimer;
  int _testClipCounter = 0;

  // Bundled MP3 test files (simulating server audio responses)
  static const List<String> _testAudioFiles = [
    'test_audio_0.mp3',
    'test_audio_1.mp3',
    'test_audio_2.mp3',
    'test_audio_3.mp3',
    'test_audio_4.mp3',
  ];

  /// Start Guardian Mode
  Future<void> start() async {
    if (!isAvailable) {
      _updateState(GuardianModeState.idle);
      throw StateError('Guardian is unavailable in this build');
    }
    if (_currentState == GuardianModeState.active) {
      print('GuardianMode: Already active');
      return;
    }

    try {
      await _channel.invokeMethod('configureAvailability', {'enabled': true});
      // Call iOS native to start silent loop
      await _channel.invokeMethod('start');
      print('GuardianMode: Native started');

      _updateState(GuardianModeState.active);

      // Start test audio injection timer (every 5 seconds)
      // _startTestAudioTimer(); // Disabled - using polling service instead
    } catch (e) {
      try {
        await _channel.invokeMethod('configureAvailability', {'enabled': false});
      } catch (_) {
        // Native setup may already be unavailable; local state still fails closed.
      }
      print('GuardianMode: Error starting: $e');
      _updateState(GuardianModeState.error);
      rethrow;
    }
  }

  /// Stop Guardian Mode
  Future<void> stop() async {
    try {
      // Stop test audio timer
      _stopTestAudioTimer();

      // Disable native availability even when Flutter already considers the
      // service idle. This prevents a failed or interrupted start from leaving
      // polling/playback enabled.
      await _channel.invokeMethod('configureAvailability', {'enabled': false});
      print('GuardianMode: Stopped');

      _updateState(GuardianModeState.idle);

      // Clean up resources (singleton pattern - no dispose() method)
      _cleanup();
    } catch (e) {
      print('GuardianMode: Error stopping: $e');
      _updateState(GuardianModeState.error);
      rethrow;
    }
  }

  Future<void> stopForAccountTransition() => whisperStateFence.stopAfterInFlight(_stopForAccountTransition);

  Future<void> _stopForAccountTransition() async {
    _stopTestAudioTimer();
    try {
      await _channel.invokeMethod('configureAvailability', {'enabled': false});
      await _channel.invokeMethod('stop');
    } catch (_) {
      // The native side may not be initialized; local state still fails closed.
    }
    _updateState(GuardianModeState.idle);
    _cleanup();
  }

  /// Start timer to inject test audio clips
  // ignore: unused_element
  void _startTestAudioTimer() {
    _testClipCounter = 0;
    _testAudioTimer?.cancel();

    // Wait 2 seconds before first clip (let silent loop initialize)
    Future.delayed(const Duration(seconds: 2), () {
      if (_currentState == GuardianModeState.active) {
        _injectNextTestClip();
      }
    });

    _testAudioTimer = Timer.periodic(const Duration(seconds: 5), (timer) async {
      await _injectNextTestClip();
    });
  }

  /// Stop test audio timer
  void _stopTestAudioTimer() {
    _testAudioTimer?.cancel();
    _testAudioTimer = null;
    _testClipCounter = 0;
  }

  /// Inject next test audio clip from bundled MP3 files
  Future<void> _injectNextTestClip() async {
    // Check if we're still in active state (prevents race conditions)
    if (!isAvailable || _currentState != GuardianModeState.active) {
      print('GuardianMode: Skipping clip injection - not in active state');
      return;
    }

    try {
      // Cycle through the test audio files
      final fileName = _testAudioFiles[_testClipCounter % _testAudioFiles.length];
      _testClipCounter++;

      print('GuardianMode: Injecting bundled test clip $_testClipCounter: $fileName');

      // Pass filename to native side - it will look it up in app bundle
      await _channel.invokeMethod('injectBundledAudioClip', {'fileName': fileName});
      print('GuardianMode: Injected clip $_testClipCounter');
    } catch (e) {
      print('GuardianMode: Error injecting test clip: $e');
    }
  }

  /// Update state and notify listeners
  void _updateState(GuardianModeState newState) {
    _currentState = newState;
    (_stateController ??= StreamController<GuardianModeState>.broadcast()).add(newState);
  }

  /// Clean up resources
  /// Note: This is a singleton, so dispose() is not appropriate.
  /// Resources are cleaned up when stop() is called instead.
  void _cleanup() {
    //     _stateController?.close();
    //     _stateController = null;
  }
}
