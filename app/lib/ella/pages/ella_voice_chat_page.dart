import 'dart:async';
import 'dart:math' as math;

import 'package:audio_session/audio_session.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:just_audio/just_audio.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';
import 'package:omi/ella/services/standard_voice_turn.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/services/voice_session_startup_guard.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/ella/widgets/memory_correction_receipt.dart';
import 'package:omi/ella/widgets/v2v_fallback_dialog.dart';
import 'package:omi/ella/widgets/voice_modal_scaffold.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Voice-to-voice chat page for Ella.
///
/// Flow: Tap orb → always-listen via on-device speech recognition →
/// auto-detect silence → send text to Ella chat → TTS → play audio → repeat.
@immutable
class EllaVoiceDemoState {
  const EllaVoiceDemoState({required this.quota, this.policyReason, this.technicalFailure = false});

  final EllaQuota quota;
  final EllaVoicePolicyReason? policyReason;
  final bool technicalFailure;
}

class EllaVoiceChatPage extends StatefulWidget {
  const EllaVoiceChatPage({
    super.key,
    this.sessionScope,
    this.memoryTitle,
    this.onMemorySessionEnded,
    this.modalPresentation = false,
    this.demoState,
  });

  final V2VSessionScope? sessionScope;
  final String? memoryTitle;
  final ValueChanged<MemoryReceiptDiscoveryRequest>? onMemorySessionEnded;
  final bool modalPresentation;
  final EllaVoiceDemoState? demoState;

  @visibleForTesting
  static bool shouldInjectVoiceTurns(V2VSessionScope? sessionScope) => sessionScope == null;

  @visibleForTesting
  static bool shouldInitializeSpeech(EllaVoiceDemoState? demoState) => demoState == null;

  @visibleForTesting
  static V2VSessionScope refreshedMemoryScope(V2VSessionScope current, String? activeSummaryVersionId) =>
      current.withExpectedActiveSummaryVersionId(activeSummaryVersionId);

  @visibleForTesting
  static bool shouldCloseRouteAfterV2VFailure(V2VFailureChoice choice, {required bool modalPresentation}) =>
      modalPresentation && choice == V2VFailureChoice.stop;

  @override
  State<EllaVoiceChatPage> createState() => _EllaVoiceChatPageState();
}

class _EllaVoiceChatPageState extends State<EllaVoiceChatPage> with AutomaticKeepAliveClientMixin {
  VoiceOrbState _orbState = VoiceOrbState.idle;
  double _audioLevel = 0.0;
  String _statusText = 'Tap to Pause';
  String _lastUserText = '';
  String _lastEllaText = '';

  /// When true, voice chat is in continuous mode — after TTS finishes,
  /// it automatically restarts listening. Set to false to stop the loop.
  bool _voiceModeActive = false;

  final AudioPlayer _audioPlayer = AudioPlayer();
  StreamSubscription? _playerSub;
  final SpeechToText _speech = SpeechToText();
  final StandardVoiceTurnCoordinator _standardVoiceTurn = const StandardVoiceTurnCoordinator();
  bool _speechAvailable = false;
  String _currentWords = '';

  /// Guard against concurrent _startListening() calls
  bool _isRestarting = false;

  /// ScrollController for the transcript area
  final ScrollController _transcriptScrollController = ScrollController();

  /// Typewriter effect state
  String _ellaDisplayText = '';
  Timer? _typewriterTimer;

  /// Track tab visibility
  bool _wasOnVoiceTab = false;

  /// V2V client for WebSocket-based voice-to-voice mode
  V2VClient? _v2vClient;
  AiConsentActiveSessionLease? _standardVoiceConsentLease;
  final VoiceSessionStartupGuard _voiceStartupGuard = VoiceSessionStartupGuard();
  bool _isV2VMode = false;
  String _activeV2VProvider = '';
  bool _usingElevenLabsFallback = false;

  /// Track whether we've injected chat messages for the current V2V turn
  bool _v2vTurnInjected = false;
  late V2VSessionScope? _sessionScope;
  String _activeSessionId = '';
  bool _consentPromptActive = false;
  MemoryReinterpretationEvent? _memoryReinterpretationEvent;
  ConversationCorrectionReceipt? _memoryCorrectionReceipt;
  Timer? _memoryReceiptPollTimer;
  int _memoryReceiptPollAttempts = 0;
  String? _memorySessionNotificationKey;
  EllaVoicePolicyReason? _policyReason;
  DateTime? _policyResetsAt;
  DateTime? _voiceSessionStartedAt;
  EllaQuota? _liveQuota;
  bool _authoritativeSoftWarning = false;
  Timer? _quotaClock;
  bool _endingForPolicy = false;

  /// Regex to strip emojis from text before sending to TTS
  static final _emojiRegex = RegExp(
    r'[\u{1F600}-\u{1F64F}]|[\u{1F300}-\u{1F5FF}]|[\u{1F680}-\u{1F6FF}]|'
    r'[\u{1F1E0}-\u{1F1FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]|'
    r'[\u{FE00}-\u{FE0F}]|[\u{1F900}-\u{1F9FF}]|[\u{1FA00}-\u{1FA6F}]|'
    r'[\u{1FA70}-\u{1FAFF}]|[\u{200D}]|[\u{20E3}]|[\u{E0020}-\u{E007F}]|'
    r'[\u{2328}]|[\u{23CF}]|[\u{23E9}-\u{23F3}]|[\u{23F8}-\u{23FA}]|'
    r'[\u{1F004}]|[\u{1F0CF}]|[\u{1F18E}]|[\u{1F191}-\u{1F19A}]|'
    r'[\u{1F201}-\u{1F251}]|[\u{203C}]|[\u{2049}]|[\u{2122}]|[\u{2139}]|'
    r'[\u{2194}-\u{21AA}]|[\u{231A}-\u{231B}]|[\u{25AA}-\u{25AB}]|'
    r'[\u{25B6}]|[\u{25C0}]|[\u{25FB}-\u{25FE}]|[\u{2614}-\u{2615}]|'
    r'[\u{2648}-\u{2653}]|[\u{267F}]|[\u{2693}]|[\u{26A1}]|[\u{26AA}-\u{26AB}]|'
    r'[\u{26BD}-\u{26BE}]|[\u{26C4}-\u{26C5}]|[\u{26CE}]|[\u{26D4}]|'
    r'[\u{26EA}]|[\u{26F2}-\u{26F3}]|[\u{26F5}]|[\u{26FA}]|[\u{26FD}]',
    unicode: true,
  );

  /// Check if current TTS provider is a V2V provider.
  static bool _isV2VProvider(String provider) => V2VClient.isSessionProvider(provider);

  String get _effectiveVoiceProvider => V2VClient.resolveEffectiveProvider(
        provisionedProvider: isHermesProvisioningGateEnabled ? SharedPreferencesUtil().ellaProvisionedVoiceMode : '',
        selectedProvider: SharedPreferencesUtil().ttsProvider,
      );

  @override
  bool get wantKeepAlive => widget.sessionScope == null;

  @override
  void initState() {
    super.initState();
    _sessionScope = widget.sessionScope;
    _policyReason = widget.demoState?.policyReason;
    // Voice mode auto-starts when the Voice tab becomes active (see didChangeDependencies)
    _playerSub = _audioPlayer.playerStateStream.listen((state) {
      if (state.processingState == ProcessingState.completed && _orbState == VoiceOrbState.speaking) {
        // Immediately transition out of speaking to prevent duplicate triggers
        _orbState = VoiceOrbState.idle;
        if (_voiceModeActive) {
          debugPrint('[VoiceChat] TTS done, auto-restarting listening');
          _startListening();
        } else {
          _returnToIdle();
        }
      }
    });
    if (EllaVoiceChatPage.shouldInitializeSpeech(widget.demoState)) {
      _initSpeech();
    }
    if (widget.sessionScope != null && widget.demoState == null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        Future.delayed(const Duration(milliseconds: 300), () {
          if (mounted && !_voiceModeActive) _activateSelectedVoice();
        });
      });
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _applyDemoStatus();
    if (widget.sessionScope != null) return;
    // Auto-start listening when user navigates TO the Voice tab (index 2)
    try {
      final homeProvider = Provider.of<HomeProvider>(context);
      final isOnVoiceTab = homeProvider.selectedIndex == 2;
      if (isOnVoiceTab && !_wasOnVoiceTab && !_voiceModeActive) {
        debugPrint('[VoiceChat] Voice tab became active, auto-starting');
        Future.delayed(const Duration(milliseconds: 300), () {
          if (mounted && _orbState == VoiceOrbState.idle) {
            _activateSelectedVoice();
          }
        });
      } else if (!isOnVoiceTab && _wasOnVoiceTab && _voiceModeActive) {
        // Navigated away — pause voice mode
        debugPrint('[VoiceChat] Left voice tab, pausing');
        if (_isV2VMode) {
          _stopV2V();
        } else {
          _pauseVoiceMode();
        }
      }
      _wasOnVoiceTab = isOnVoiceTab;
    } catch (_) {
      // HomeProvider not available
    }
  }

  @override
  void didUpdateWidget(covariant EllaVoiceChatPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.demoState != widget.demoState) {
      _policyReason = widget.demoState?.policyReason;
      _policyResetsAt = null;
      _applyDemoStatus();
    }
  }

  void _applyDemoStatus() {
    final demoState = widget.demoState;
    if (demoState == null) return;
    if (demoState.technicalFailure) {
      _statusText = context.l10n.ellaVoiceTechnicalFailure;
    } else if (demoState.policyReason != null) {
      _statusText = _policyStatus(context, demoState.policyReason!);
    } else {
      _statusText = context.l10n.ellaVoiceDemoPreview;
    }
  }

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        debugPrint('[VoiceChat] Speech error: ${error.errorMsg}');
        if (error.errorMsg == 'error_no_match' && _orbState == VoiceOrbState.listening) {
          if (_currentWords.isNotEmpty) {
            _processTranscript(_currentWords);
          } else if (_voiceModeActive) {
            debugPrint('[VoiceChat] No match, restarting listening');
            _startListening();
          } else {
            _returnToIdle();
          }
        }
      },
      onStatus: (status) {
        debugPrint('[VoiceChat] Speech status: $status');
        if (status == 'notListening' && _orbState == VoiceOrbState.listening) {
          if (_currentWords.isNotEmpty) {
            _processTranscript(_currentWords);
          } else if (_voiceModeActive) {
            debugPrint('[VoiceChat] Silence timeout, restarting listening');
            _startListening();
          }
        }
      },
    );
    debugPrint('[VoiceChat] Speech available: $_speechAvailable');
  }

  @override
  void dispose() {
    _notifyMemorySessionEnded(_activeSessionId);
    _voiceStartupGuard.dispose();
    _voiceModeActive = false;
    _typewriterTimer?.cancel();
    _quotaClock?.cancel();
    _memoryReceiptPollTimer?.cancel();
    _playerSub?.cancel();
    _audioPlayer.dispose();
    _transcriptScrollController.dispose();
    if (_speech.isListening) {
      _speech.stop();
    }
    final client = _v2vClient;
    _v2vClient = null;
    if (client != null) unawaited(client.disconnect());
    _standardVoiceConsentLease?.stop();
    _standardVoiceConsentLease = null;
    super.dispose();
  }

  void _returnToIdle() {
    if (!mounted) return;
    if (_voiceModeActive) {
      debugPrint('[VoiceChat] Voice mode active, restarting listening');
      _startListening();
      return;
    }
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = 'Paused — Tap to Resume';
      _audioLevel = 0.0;
    });
  }

  Future<bool> _ensureVoiceConsent() async {
    if (_consentPromptActive || !mounted) return false;

    _consentPromptActive = true;
    try {
      return await AiConsentCoordinator.ensure(context);
    } finally {
      _consentPromptActive = false;
    }
  }

  Future<void> _activateSelectedVoice() async {
    if (widget.demoState != null) return;
    if (!await _ensureVoiceConsent() || !mounted) return;

    final quota = isEllaEntitlementGateEnabled ? context.read<EllaEntitlementProvider>().quota : null;
    if (quota?.isHardStop == true) {
      final reason = quota!.dailyFraction >= 1 ? EllaVoicePolicyReason.quotaDaily : EllaVoicePolicyReason.quotaMonthly;
      _showPolicyStop(reason, resetsAt: quota.resetsAt);
      return;
    }
    _policyReason = null;
    _policyResetsAt = null;

    final provider = _effectiveVoiceProvider;
    if (_sessionScope != null && !V2VClient.isMemoryScopedProvider(provider)) {
      setState(() {
        _voiceModeActive = false;
        _isV2VMode = false;
        _orbState = VoiceOrbState.idle;
        _statusText = context.l10n.memoryTalkProviderRequired;
      });
      return;
    }

    if (_isV2VProvider(provider)) {
      final startupGeneration = _beginV2VStartup(provider);
      await _startV2V(provider, startupGeneration: startupGeneration);
    } else {
      _startStandardVoiceConsentLease();
      setState(() {
        _voiceModeActive = true;
        _isV2VMode = false;
      });
      await _startListening();
    }
  }

  int _beginV2VStartup(String provider) {
    _standardVoiceConsentLease?.stop();
    _standardVoiceConsentLease = null;
    final startupGeneration = _voiceStartupGuard.begin();
    final providerName = localizedV2VProviderName(context, V2VClient.normalizeProvider(provider));
    setState(() {
      _voiceModeActive = true;
      _isV2VMode = true;
      _usingElevenLabsFallback = false;
      _orbState = VoiceOrbState.processing;
      _statusText = context.l10n.voiceV2vConnecting(providerName);
      _audioLevel = 0.0;
      _policyReason = null;
      _policyResetsAt = null;
      _liveQuota = null;
      _authoritativeSoftWarning = false;
    });
    return startupGeneration;
  }

  bool _isCurrentV2VStartup(int startupGeneration) => mounted && _voiceStartupGuard.isCurrent(startupGeneration);

  Future<void> _onOrbTap() async {
    debugPrint('[VoiceChat] Orb tapped, state: $_orbState, voiceMode: $_voiceModeActive, v2v: $_isV2VMode');
    HapticFeedback.mediumImpact();

    if (!_voiceModeActive) {
      await _activateSelectedVoice();
      return;
    }

    // Voice mode is active — tap means pause
    if (_isV2VMode) {
      await _stopV2V();
      return;
    }

    switch (_orbState) {
      case VoiceOrbState.idle:
        _pauseVoiceMode();
        break;
      case VoiceOrbState.listening:
        await _speech.stop();
        if (_currentWords.isNotEmpty) {
          _processTranscript(_currentWords);
        }
        _pauseVoiceMode();
        break;
      case VoiceOrbState.speaking:
        await _audioPlayer.stop();
        await ElevenLabsTts.stopOnDevice();
        _pauseVoiceMode();
        break;
      case VoiceOrbState.processing:
        _pauseVoiceMode();
        break;
    }
  }

  void _pauseVoiceMode({bool cancelStartup = true}) {
    debugPrint('[VoiceChat] Pausing voice mode');
    if (cancelStartup) _voiceStartupGuard.cancel();
    _voiceModeActive = false;
    _isV2VMode = false;
    _standardVoiceConsentLease?.stop();
    _standardVoiceConsentLease = null;
    _typewriterTimer?.cancel();
    if (_speech.isListening) {
      _speech.stop();
    }
    if (!mounted) return;
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = 'Paused — Tap to Resume';
      _audioLevel = 0.0;
    });
  }

  void _startStandardVoiceConsentLease() {
    _standardVoiceConsentLease?.stop();
    _standardVoiceConsentLease = AiConsentActiveSessionLease(
      uid: SharedPreferencesUtil().uid,
      onAuthorityLost: _handleStandardVoiceConsentAuthorityLost,
    )..start();
  }

  Future<void> _handleStandardVoiceConsentAuthorityLost() async {
    _standardVoiceConsentLease = null;
    _voiceModeActive = false;
    _isV2VMode = false;
    if (_speech.isListening) {
      await _speech.stop();
    }
    try {
      await _audioPlayer.stop();
      await ElevenLabsTts.stopOnDevice();
    } catch (_) {}
    if (!mounted) return;
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = context.l10n.aiConsentActiveAudioStopped;
      _audioLevel = 0.0;
    });
  }

  Future<void> _startListening() async {
    if (!SharedPreferencesUtil().aiConsentAccepted) return;
    debugPrint('[VoiceChat] _startListening called');

    if (_isRestarting) {
      debugPrint('[VoiceChat] Already restarting, skipping');
      return;
    }
    _isRestarting = true;

    try {
      await _startListeningInner();
    } finally {
      _isRestarting = false;
    }
  }

  Future<void> _startListeningInner() async {
    final micStatus = await Permission.microphone.request();
    if (!micStatus.isGranted) {
      if (!mounted) return;
      if (micStatus.isPermanentlyDenied) {
        showDialog(
          context: context,
          builder: (ctx) => AlertDialog(
            backgroundColor: EllaColors.bgSecondary,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
            title: const Text(
              'Microphone Access',
              style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: EllaColors.textPrimary),
            ),
            content: const Text(
              'Ella needs microphone access for voice chat. Please enable it in Settings.',
              style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(),
                child: const Text('Cancel', style: TextStyle(fontSize: 18, color: EllaColors.textTertiary)),
              ),
              TextButton(
                onPressed: () {
                  Navigator.of(ctx).pop();
                  openAppSettings();
                },
                child: const Text('Open Settings', style: TextStyle(fontSize: 18, color: EllaColors.primary)),
              ),
            ],
          ),
        );
      } else {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Microphone permission is required for voice chat')));
      }
      return;
    }

    final speechStatus = await Permission.speech.request();
    if (!speechStatus.isGranted) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Speech recognition permission is required')));
      return;
    }

    if (!_speechAvailable) {
      await _initSpeech();
      if (!_speechAvailable) {
        if (!mounted) return;
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Speech recognition is not available on this device')));
        return;
      }
    }

    // Stop any existing mic recording from CaptureProvider
    if (mounted) {
      final captureProvider = Provider.of<CaptureProvider>(context, listen: false);
      if (captureProvider.recordingState == RecordingState.record) {
        debugPrint('[VoiceChat] Pausing capture recording to free mic');
        await captureProvider.stopStreamRecording();
      }
    }

    // Stop audio player to release audio session before mic starts
    try {
      await _audioPlayer.stop();
    } catch (_) {}

    // Ensure previous speech session is fully stopped
    if (_speech.isListening) {
      debugPrint('[VoiceChat] Stopping previous speech session');
      await _speech.stop();
    }

    // Small delay to let iOS audio session switch from playback to recording
    await Future.delayed(const Duration(milliseconds: 300));
    if (!mounted || !_voiceModeActive) return;

    _currentWords = '';
    _typewriterTimer?.cancel();
    setState(() {
      _orbState = VoiceOrbState.listening;
      _statusText = 'Listening...';
      _lastEllaText = '';
      _lastUserText = '';
      _ellaDisplayText = '';
    });

    debugPrint('[VoiceChat] Starting speech recognition');
    try {
      await _speech.listen(
        onResult: _onSpeechResult,
        onSoundLevelChange: (level) {
          if (!mounted || _orbState != VoiceOrbState.listening) return;
          final normalized = ((level + 2) / 12).clamp(0.0, 1.0);
          setState(() {
            _audioLevel = normalized;
          });
        },
        listenMode: ListenMode.dictation,
        pauseFor: const Duration(seconds: 3),
        listenFor: const Duration(seconds: 60),
        cancelOnError: false,
        partialResults: true,
      );
      debugPrint('[VoiceChat] speech.listen() started');
    } catch (e) {
      debugPrint('[VoiceChat] speech.listen() failed: $e — retrying in 500ms');
      await Future.delayed(const Duration(milliseconds: 500));
      if (!mounted || !_voiceModeActive) return;
      try {
        await _speech.listen(
          onResult: _onSpeechResult,
          onSoundLevelChange: (level) {
            if (!mounted || _orbState != VoiceOrbState.listening) return;
            final normalized = ((level + 2) / 12).clamp(0.0, 1.0);
            setState(() {
              _audioLevel = normalized;
            });
          },
          listenMode: ListenMode.dictation,
          pauseFor: const Duration(seconds: 3),
          listenFor: const Duration(seconds: 60),
          cancelOnError: false,
          partialResults: true,
        );
        debugPrint('[VoiceChat] speech.listen() retry succeeded');
      } catch (e2) {
        debugPrint('[VoiceChat] speech.listen() retry also failed: $e2');
        if (mounted && _voiceModeActive) {
          setState(() {
            _statusText = 'Mic unavailable. Tap to retry.';
            _orbState = VoiceOrbState.idle;
          });
          _voiceModeActive = false;
        }
      }
    }
  }

  // --- V2V (Voice-to-Voice) WebSocket mode ---

  Future<bool> _refreshMemoryScope(int startupGeneration) async {
    final currentScope = _sessionScope;
    if (currentScope == null || !_isCurrentV2VStartup(startupGeneration)) return false;
    setState(() {
      _orbState = VoiceOrbState.processing;
      _statusText = context.l10n.memoryTalkStale;
    });
    final refreshed = await getConversationById(currentScope.conversationId);
    if (!_isCurrentV2VStartup(startupGeneration) || refreshed == null) return false;
    _sessionScope = EllaVoiceChatPage.refreshedMemoryScope(currentScope, refreshed.activeSummaryVersionId);
    return true;
  }

  Future<void> _startV2V(String provider, {required int startupGeneration, bool allowScopeRefresh = true}) async {
    final authority = AiConsentAuthoritySnapshot.capture(expectedUid: SharedPreferencesUtil().uid);
    bool hasCurrentStartupAuthority() => _isCurrentV2VStartup(startupGeneration) && authority?.isCurrent() == true;
    if (!hasCurrentStartupAuthority()) return;
    provider = V2VClient.normalizeProvider(provider);
    final providerName = localizedV2VProviderName(context, provider);
    debugPrint('[VoiceChat] Starting V2V mode with provider: $provider');

    // Stop any existing mic recording from CaptureProvider
    final captureProvider = Provider.of<CaptureProvider>(context, listen: false);
    if (captureProvider.recordingState == RecordingState.record) {
      debugPrint('[VoiceChat] Pausing capture recording for V2V');
      await captureProvider.stopStreamRecording();
      if (!hasCurrentStartupAuthority()) return;
    }

    setState(() {
      _orbState = VoiceOrbState.processing;
      _statusText = context.l10n.voiceV2vConnecting(providerName);
      _audioLevel = 0.0;
    });

    // Stop any playing audio
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    if (!hasCurrentStartupAuthority()) return;

    late final V2VClient client;
    client = V2VClient(
      onEvent: (event) {
        if (!_isCurrentV2VStartup(startupGeneration) || !identical(_v2vClient, client)) return;
        _onV2VEvent(event);
      },
      onConnectionChanged: (connected) {
        if (!_isCurrentV2VStartup(startupGeneration) || !identical(_v2vClient, client)) return;
        if (!connected && _isV2VMode && !_endingForPolicy) {
          final endedSessionId = _activeSessionId;
          debugPrint('[VoiceChat] V2V disconnected unexpectedly');
          _voiceStartupGuard.cancel();
          _v2vClient = null;
          setState(() {
            _orbState = VoiceOrbState.idle;
            _statusText = 'Disconnected — Tap to Reconnect';
            _isV2VMode = false;
            _voiceModeActive = false;
          });
          _notifyMemorySessionEnded(endedSessionId);
          _activeSessionId = '';
        }
      },
    );
    if (!hasCurrentStartupAuthority()) {
      await client.disconnect();
      return;
    }
    _v2vClient = client;

    final receipt = await client.connect(
      provider: provider,
      sessionScope: _sessionScope,
      shouldContinue: () => hasCurrentStartupAuthority() && identical(_v2vClient, client),
    );
    if (!_isCurrentV2VStartup(startupGeneration) || !identical(_v2vClient, client)) {
      await client.disconnect();
      return;
    }

    if (!receipt.connected) {
      debugPrint('[VoiceChat] V2V connect failed: ${receipt.toDebugFields()}');
      _v2vClient = null;
      await client.disconnect();
      if (!_isCurrentV2VStartup(startupGeneration)) return;
      if (allowScopeRefresh && receipt.shouldRefreshMemoryScope && await _refreshMemoryScope(startupGeneration)) {
        await _startV2V(provider, startupGeneration: startupGeneration, allowScopeRefresh: false);
        return;
      }
      if (receipt.isPolicyDenial) {
        _voiceStartupGuard.complete(startupGeneration);
        _showPolicyStop(receipt.policyReason!);
        return;
      }
      _voiceStartupGuard.complete(startupGeneration);
      setState(() {
        _isV2VMode = false;
        _voiceModeActive = false;
        _orbState = VoiceOrbState.idle;
        _statusText = _sessionScope != null && receipt.errorCode == 'voice_session_scope_unavailable'
            ? context.l10n.memoryTalkUnavailable
            : receipt.safeDetail;
        _audioLevel = 0.0;
      });

      if (!mounted) return;
      final choice = await showV2VFallbackDialog(context, receipt, allowStandardFallback: _sessionScope == null);
      if (!mounted) return;
      switch (choice) {
        case V2VFailureChoice.retry:
          final retryGeneration = _beginV2VStartup(provider);
          await _startV2V(provider, startupGeneration: retryGeneration);
          break;
        case V2VFailureChoice.useElevenLabs:
          if (_sessionScope != null) break;
          _usingElevenLabsFallback = true;
          _startStandardVoiceConsentLease();
          setState(() {
            _voiceModeActive = true;
            _isV2VMode = false;
            _statusText = context.l10n.voiceElevenLabsFallbackActive;
          });
          await _startListening();
          break;
        case V2VFailureChoice.stop:
          await _cancelFailedVoiceAttempt();
          if (EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(choice, modalPresentation: widget.modalPresentation) &&
              mounted) {
            Navigator.of(context).pop();
          }
          break;
      }
      return;
    }

    _voiceStartupGuard.complete(startupGeneration);
    _activeSessionId = receipt.sessionId;
    _memorySessionNotificationKey = null;
    _memoryReinterpretationEvent = null;
    _memoryReceiptPollTimer?.cancel();
    _activeV2VProvider = providerName;
    _usingElevenLabsFallback = false;
    _voiceSessionStartedAt = DateTime.now();
    _startQuotaClock();
    setState(() {
      _memoryCorrectionReceipt = null;
      _orbState = VoiceOrbState.listening;
      _statusText = context.l10n.voiceV2vActive(providerName);
    });
  }

  Future<void> _cancelFailedVoiceAttempt() async {
    _voiceStartupGuard.cancel();
    final client = _v2vClient;
    _v2vClient = null;
    _voiceModeActive = false;
    _isV2VMode = false;
    _usingElevenLabsFallback = false;
    _activeV2VProvider = '';
    _activeSessionId = '';
    _standardVoiceConsentLease?.stop();
    _standardVoiceConsentLease = null;
    _quotaClock?.cancel();
    if (client != null) {
      try {
        await client.disconnect();
      } catch (_) {}
    }
    if (_speech.isListening) {
      try {
        await _speech.stop();
      } catch (_) {}
    }
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    try {
      await ElevenLabsTts.stopOnDevice();
    } catch (_) {}
    if (!mounted) return;
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = context.l10n.voiceTapToTalk;
      _audioLevel = 0.0;
    });
  }

  Future<void> _stopV2V() async {
    debugPrint('[VoiceChat] Stopping V2V mode');
    _voiceStartupGuard.cancel();
    final endedSessionId = _activeSessionId;
    final client = _v2vClient;
    _v2vClient = null;
    _voiceModeActive = false;
    _isV2VMode = false;
    if (client != null) await client.disconnect();
    _notifyMemorySessionEnded(endedSessionId);
    _activeSessionId = '';
    _activeV2VProvider = '';
    _voiceSessionStartedAt = null;
    _liveQuota = null;
    _authoritativeSoftWarning = false;
    _quotaClock?.cancel();
    _pauseVoiceMode(cancelStartup: false);
  }

  bool get _hasActiveVoiceSession =>
      _voiceStartupGuard.isStarting ||
      _voiceModeActive ||
      _isV2VMode ||
      _speech.isListening ||
      _orbState != VoiceOrbState.idle;

  Future<bool> _endModalVoiceSession() async {
    if (_voiceStartupGuard.isStarting || _isV2VMode || _v2vClient != null) {
      await _stopV2V();
    } else {
      _voiceModeActive = false;
      _typewriterTimer?.cancel();
      if (_speech.isListening) {
        try {
          await _speech.stop();
        } catch (_) {}
      }
      try {
        await _audioPlayer.stop();
      } catch (_) {}
      try {
        await ElevenLabsTts.stopOnDevice();
      } catch (_) {}
      _pauseVoiceMode();
    }
    return !_hasActiveVoiceSession;
  }

  void _notifyMemorySessionEnded(String sessionId) {
    final scope = _sessionScope;
    if (scope == null || sessionId.isEmpty) return;
    final request = MemoryReceiptDiscoveryRequest(conversationId: scope.conversationId, sessionId: sessionId);
    if (_memorySessionNotificationKey == request.key) return;
    _memorySessionNotificationKey = request.key;
    widget.onMemorySessionEnded?.call(request);
  }

  void _onV2VEvent(V2VEvent event) {
    if (!mounted) return;

    switch (event.type) {
      case 'user_transcript':
        _v2vTurnInjected = false; // new turn started
        _lastEllaText = ''; // reset accumulator for next response
        setState(() {
          _lastUserText = event.text ?? '';
          _ellaDisplayText = '';
        });
        _scrollToBottom();
        break;
      case 'transcript':
        // Proxy sends streaming deltas — accumulate them
        final delta = event.text ?? '';
        _lastEllaText += delta;
        setState(() {
          _ellaDisplayText = _lastEllaText;
          _orbState = VoiceOrbState.speaking;
          _statusText = 'Ella is speaking...';
        });
        _scrollToBottom();
        break;
      case 'audio_done':
        // Inject into chat history once per turn (using final transcript)
        if (EllaVoiceChatPage.shouldInjectVoiceTurns(_sessionScope) &&
            !_v2vTurnInjected &&
            _lastUserText.isNotEmpty &&
            _lastEllaText.isNotEmpty) {
          _injectVoiceMessages(_lastUserText, _lastEllaText);
          _v2vTurnInjected = true;
        }
        // Audio is now being played via just_audio — wait for playback_complete
        setState(() {
          _statusText = 'Playing audio...';
        });
        break;
      case 'playback_complete':
        // WAV file finished playing — transition back to listening
        if (_isV2VMode) {
          setState(() {
            _orbState = VoiceOrbState.listening;
            _statusText = context.l10n.voiceV2vActive(_activeV2VProvider);
          });
        }
        break;
      case 'speech_started':
        // User started talking — interrupt playback, reset turn tracking
        _v2vTurnInjected = false;
        _lastEllaText = '';
        setState(() {
          _orbState = VoiceOrbState.listening;
          _statusText = 'Listening...';
          _ellaDisplayText = '';
        });
        break;
      case 'function_calling':
        setState(() {
          _orbState = VoiceOrbState.processing;
          _statusText = 'Ella is thinking...';
        });
        break;
      case 'function_executed':
        setState(() {
          _statusText = 'Generating response...';
        });
        break;
      case 'memory_reinterpretation':
        final memoryReinterpretation = event.memoryReinterpretation;
        if (memoryReinterpretation != null) {
          _handleMemoryReinterpretation(memoryReinterpretation);
        }
        break;
      case 'quota_state':
        if (event.policyReason != null) {
          _showPolicyStop(event.policyReason!, resetsAt: event.resetsAt);
          break;
        }
        setState(() {
          _liveQuota = event.quota ?? _liveQuota;
          _authoritativeSoftWarning = event.quotaState == 'soft_warning';
        });
        break;
      case 'v2v_debug':
        // Debug info from V2V client — show on screen temporarily
        setState(() {
          _statusText = event.text ?? '';
        });
        break;
      case 'connection_receipt':
        // The connect caller owns the explicit success/failure UI.
        break;
      case 'error':
        if (event.policyReason != null) {
          _showPolicyStop(event.policyReason!, resetsAt: event.resetsAt);
          break;
        }
        debugPrint('[VoiceChat] V2V error: ${event.text}');
        setState(() {
          _statusText = context.l10n.ellaVoiceTechnicalFailure;
        });
        break;
      case 'consent_authority_lost':
        final endedSessionId = _activeSessionId;
        _voiceStartupGuard.cancel();
        _v2vClient = null;
        _voiceModeActive = false;
        _isV2VMode = false;
        _activeSessionId = '';
        _activeV2VProvider = '';
        _notifyMemorySessionEnded(endedSessionId);
        setState(() {
          _orbState = VoiceOrbState.idle;
          _statusText = context.l10n.aiConsentActiveAudioStopped;
          _audioLevel = 0.0;
        });
        break;
      case 'session_end':
        debugPrint('[VoiceChat] V2V session ended: ${event.text}');
        if (event.policyReason != null) {
          _showPolicyStop(event.policyReason!, resetsAt: event.resetsAt);
        } else if (_isV2VMode) {
          _stopV2V();
        }
        break;
    }
  }

  void _showPolicyStop(EllaVoicePolicyReason reason, {DateTime? resetsAt}) {
    _endingForPolicy = true;
    _voiceStartupGuard.cancel();
    final client = _v2vClient;
    _v2vClient = null;
    _voiceModeActive = false;
    _isV2VMode = false;
    _activeSessionId = '';
    _activeV2VProvider = '';
    _voiceSessionStartedAt = null;
    _authoritativeSoftWarning = false;
    _quotaClock?.cancel();
    if (client != null) unawaited(client.disconnect());
    if (mounted) {
      setState(() {
        _policyReason = reason;
        _policyResetsAt = resetsAt;
        _orbState = VoiceOrbState.idle;
        _audioLevel = 0;
        _statusText = _policyStatus(context, reason);
      });
    }
    _endingForPolicy = false;
  }

  void _startQuotaClock() {
    _quotaClock?.cancel();
    _quotaClock = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted) setState(() {});
    });
  }

  EllaQuota? _visibleQuota(BuildContext context) {
    if (widget.demoState != null) return widget.demoState!.quota;
    if (_liveQuota != null) return _liveQuota;
    if (!isEllaEntitlementGateEnabled) return null;
    return context.watch<EllaEntitlementProvider>().quota;
  }

  int _remainingVoiceSeconds(EllaQuota quota) {
    final elapsed = _voiceSessionStartedAt == null ? 0 : DateTime.now().difference(_voiceSessionStartedAt!).inSeconds;
    return (quota.voiceRemainingSeconds - elapsed).clamp(0, quota.voiceRemainingSeconds);
  }

  void _handleMemoryReinterpretation(MemoryReinterpretationEvent event) {
    final scope = _sessionScope;
    if (scope == null ||
        event.conversationId != scope.conversationId ||
        (_activeSessionId.isNotEmpty && event.sessionId != _activeSessionId)) {
      return;
    }
    _memoryReinterpretationEvent = event;
    _memoryReceiptPollAttempts = 0;
    _memoryReceiptPollTimer?.cancel();
    _pollMemoryCorrectionReceipt();
  }

  Future<void> _pollMemoryCorrectionReceipt() async {
    final event = _memoryReinterpretationEvent;
    if (event == null || !mounted) return;
    _memoryReceiptPollAttempts++;

    final receipt = await getConversationCorrectionReceipt(
      conversationId: event.conversationId,
      correctionId: event.correctionId,
    );
    if (!mounted || _memoryReinterpretationEvent != event) return;
    if (receipt != null &&
        receipt.conversationId == event.conversationId &&
        receipt.correctionId == event.correctionId) {
      setState(() => _memoryCorrectionReceipt = receipt);
    }

    if ((receipt == null || receipt.isPending) && _memoryReceiptPollAttempts < 40) {
      _memoryReceiptPollTimer = Timer(event.pollAfter, _pollMemoryCorrectionReceipt);
    }
  }

  Future<ConversationCorrectionReceipt?> _undoMemoryCorrection() async {
    final receipt = _memoryCorrectionReceipt;
    if (receipt == null || !receipt.isApplied) return null;
    final updated = await undoConversationCorrection(
      conversationId: receipt.conversationId,
      correctionId: receipt.correctionId,
    );
    if (mounted && updated != null) {
      setState(() => _memoryCorrectionReceipt = updated);
    }
    return updated;
  }

  void _reviewMemoryCorrection() {
    final receipt = _memoryCorrectionReceipt;
    if (receipt == null || !receipt.isApplied) return;
    showMemoryCorrectionReceiptSheet(context, receipt: receipt, onUndo: _undoMemoryCorrection);
  }

  void _onSpeechResult(SpeechRecognitionResult result) {
    if (!mounted) return;
    debugPrint('[VoiceChat] Speech result: final=${result.finalResult}, text="${result.recognizedWords}"');

    _currentWords = result.recognizedWords;

    if (result.finalResult && _currentWords.isNotEmpty) {
      _processTranscript(_currentWords);
    } else {
      setState(() {
        _lastUserText = _currentWords;
      });
      _scrollToBottom();
    }
  }

  Future<void> _processTranscript(String transcript) async {
    debugPrint('[VoiceChat] Processing transcript: "$transcript"');
    _currentWords = '';

    if (!mounted) return;
    final messageProvider = Provider.of<MessageProvider>(context, listen: false);
    try {
      await messageProvider.runProtectedOperationAtEntry(
        (operation) async {
          if (!mounted || !operation.isCurrent) return;
          setState(() {
            _orbState = VoiceOrbState.processing;
            _statusText = 'Ella is thinking...';
            _audioLevel = 0.0;
            _lastUserText = transcript;
          });

          if (_speech.isListening) {
            debugPrint('[VoiceChat] Stopping speech recognizer before response playback');
            await _speech.stop();
            if (!mounted || !operation.isCurrent) return;
            await Future.delayed(const Duration(milliseconds: 150));
            if (!mounted || !operation.isCurrent) return;
          }

          debugPrint('[VoiceChat] Sending to Ella chat for authority uid=${operation.uid}');
          final result = await _standardVoiceTurn.run(
            transcript: transcript,
            authority: operation.exactAuthority,
            prepareTtsText: _stripEmojis,
            commitMessages: (userText, ellaReply) =>
                !EllaVoiceChatPage.shouldInjectVoiceTurns(_sessionScope) ||
                _injectStandardVoiceMessages(userText, ellaReply, messageProvider, operation),
            onReplyReady: (fullReply) {
              if (!mounted || !operation.isCurrent) return;
              debugPrint(
                '[VoiceChat] Ella reply (${fullReply.length} chars): '
                '"${fullReply.substring(0, math.min(100, fullReply.length))}"',
              );
              _lastEllaText = fullReply;
              _startTypewriter(fullReply);
              setState(() {
                _statusText = 'Ella is speaking...';
                _orbState = VoiceOrbState.speaking;
              });
            },
            speakOnDevice: (ttsText) => ElevenLabsTts.speakOnDevice(ttsText, exactAuthority: operation.exactAuthority),
            playFile: (audioPath) async {
              if (!mounted || !operation.isCurrent) return;
              debugPrint('[VoiceChat] Playing authority-bound audio: $audioPath');
              if (!await _prepareTtsPlaybackSession(isCurrent: () => mounted && operation.isCurrent)) return;
              if (!mounted || !operation.isCurrent) return;
              await _audioPlayer.setVolume(1.0);
              if (!mounted || !operation.isCurrent) return;
              await _audioPlayer.setFilePath(audioPath);
              if (!mounted || !operation.isCurrent) return;
              await _audioPlayer.play();
            },
          );

          if (!mounted || !operation.isCurrent || result.discarded) return;
          if (result.reply.isEmpty || result.usedOnDeviceTts) {
            if (_voiceModeActive) {
              _startListening();
            } else {
              _returnToIdle();
            }
          }
        },
        onInvalidated: _cancelStandardVoiceEffects,
      );
    } catch (e, st) {
      debugPrint('[VoiceChat] Error in voice flow: $e\n$st');
      if (mounted) {
        if (_voiceModeActive) {
          debugPrint('[VoiceChat] Error, but restarting listening');
          _startListening();
        } else {
          setState(() {
            _statusText = 'Something went wrong. Tap to try again.';
            _orbState = VoiceOrbState.idle;
          });
        }
      }
    }
  }

  void _cancelStandardVoiceEffects() {
    _typewriterTimer?.cancel();
    _voiceModeActive = false;
    _currentWords = '';
    unawaited(_audioPlayer.stop());
    unawaited(ElevenLabsTts.stopOnDevice());
    unawaited(_speech.stop());
    if (mounted) {
      setState(() {
        _lastUserText = '';
        _lastEllaText = '';
        _ellaDisplayText = '';
        _orbState = VoiceOrbState.idle;
        _statusText = 'Tap to Start';
      });
    }
  }

  Future<bool> _prepareTtsPlaybackSession({bool Function()? isCurrent}) async {
    final session = await AudioSession.instance;
    if (isCurrent?.call() == false) return false;
    await session.configure(
      AudioSessionConfiguration(
        avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
        avAudioSessionCategoryOptions: AVAudioSessionCategoryOptions.defaultToSpeaker |
            AVAudioSessionCategoryOptions.allowBluetooth |
            AVAudioSessionCategoryOptions.allowBluetoothA2dp |
            AVAudioSessionCategoryOptions.allowAirPlay,
        avAudioSessionMode: AVAudioSessionMode.defaultMode,
        avAudioSessionRouteSharingPolicy: AVAudioSessionRouteSharingPolicy.defaultPolicy,
        avAudioSessionSetActiveOptions: AVAudioSessionSetActiveOptions.none,
      ),
    );
    if (isCurrent?.call() == false) return false;
    await session.setActive(true);
    if (isCurrent?.call() == false) return false;
    debugPrint('[VoiceChat] Audio session prepared for TTS playback');
    return true;
  }

  /// Strip emojis from text so TTS doesn't read them aloud.
  String _stripEmojis(String text) {
    return text.replaceAll(_emojiRegex, '').replaceAll(RegExp(r'  +'), ' ').trim();
  }

  /// Inject voice exchange into MessageProvider so Chat tab shows it.
  bool _injectStandardVoiceMessages(
    String userText,
    String ellaReply,
    MessageProvider msgProvider,
    MessageProtectedOperation operation,
  ) {
    try {
      final now = DateTime.now();

      final userMsg = ServerMessage(
        const Uuid().v4(),
        now,
        userText,
        MessageSender.human,
        MessageType.text,
        null,
        false,
        [],
        [],
        [],
        fromVoice: true,
      );
      final aiMsg = ServerMessage(
        const Uuid().v4(),
        now.add(const Duration(milliseconds: 100)),
        ellaReply,
        MessageSender.ai,
        MessageType.text,
        null,
        false,
        [],
        [],
        [],
        fromVoice: true,
      );
      final committed = msgProvider.addVoiceMessagesForProtectedOperation(userMsg, aiMsg, operation);
      if (committed) debugPrint('[VoiceChat] Injected authority-bound voice messages into chat history');
      return committed;
    } catch (e) {
      debugPrint('[VoiceChat] Failed to inject messages: $e');
      return false;
    }
  }

  void _injectVoiceMessages(String userText, String ellaReply) {
    try {
      final msgProvider = Provider.of<MessageProvider>(context, listen: false);
      final now = DateTime.now();
      msgProvider.addMessage(
        ServerMessage(
          const Uuid().v4(),
          now,
          userText,
          MessageSender.human,
          MessageType.text,
          null,
          false,
          [],
          [],
          [],
          fromVoice: true,
        ),
      );
      msgProvider.addMessage(
        ServerMessage(
          const Uuid().v4(),
          now.add(const Duration(milliseconds: 100)),
          ellaReply,
          MessageSender.ai,
          MessageType.text,
          null,
          false,
          [],
          [],
          [],
          fromVoice: true,
        ),
      );
    } catch (e) {
      debugPrint('[VoiceChat] Failed to inject V2V messages: $e');
    }
  }

  /// Progressively reveals text with a typewriter effect.
  void _startTypewriter(String fullText) {
    _typewriterTimer?.cancel();
    _ellaDisplayText = '';
    int charIndex = 0;
    _typewriterTimer = Timer.periodic(const Duration(milliseconds: 50), (timer) {
      if (!mounted || charIndex >= fullText.length) {
        timer.cancel();
        if (mounted) {
          setState(() {
            _ellaDisplayText = fullText;
          });
        }
        return;
      }
      charIndex++;
      setState(() {
        _ellaDisplayText = fullText.substring(0, charIndex);
      });
      _scrollToBottom();
    });
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_transcriptScrollController.hasClients) {
        _transcriptScrollController.animateTo(
          _transcriptScrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final quota = _visibleQuota(context);
    final policyReason = _policyReason;

    final body = SafeArea(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_sessionScope != null && widget.memoryTitle?.trim().isNotEmpty == true) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 8, 24, 0),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                decoration: BoxDecoration(
                  color: EllaColors.card,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                  border: Border.all(color: EllaColors.cardDeep),
                ),
                child: Row(
                  children: [
                    const Icon(Icons.auto_stories_outlined, size: 20, color: EllaColors.primary),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        context.l10n.memoryTalkContext(widget.memoryTitle!.trim()),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w600),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
          const Spacer(flex: 2),
          Center(
            child: EllaVoiceOrb(state: _orbState, audioLevel: _audioLevel, onTap: _onOrbTap),
          ),
          if (quota != null && policyReason == null) ...[
            const SizedBox(height: 10),
            Text(
              context.l10n.ellaVoiceTimeRemaining(_compactVoiceTime(_remainingVoiceSeconds(quota))),
              style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
              textAlign: TextAlign.center,
            ),
          ],
          if ((_authoritativeSoftWarning || quota?.isSoftWarning == true) && policyReason == null) ...[
            const SizedBox(height: 12),
            _VoiceLimitNotice(text: context.l10n.ellaVoiceSoftWarning),
          ],
          if (policyReason != null) ...[
            const SizedBox(height: 12),
            _VoiceLimitNotice(text: _policyBody(context, policyReason, resetsAt: _policyResetsAt)),
          ],
          const SizedBox(height: 24),
          Center(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              decoration: BoxDecoration(
                color: EllaColors.card,
                borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                border: Border.all(color: EllaColors.cardDeep),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (_orbState == VoiceOrbState.listening) ...[const EllaBreathingDot(), const SizedBox(width: 10)],
                  Flexible(
                    child: Text(
                      _orbState == VoiceOrbState.listening
                          ? _usingElevenLabsFallback
                              ? context.l10n.voiceElevenLabsFallbackActive
                              : _isV2VMode && _activeV2VProvider.isNotEmpty
                                  ? context.l10n.voiceV2vActive(_activeV2VProvider)
                                  : context.l10n.voiceListening
                          : _statusText,
                      style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                      textAlign: TextAlign.center,
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (_memoryCorrectionReceipt != null) ...[
            const SizedBox(height: 12),
            MemoryCorrectionReceiptChip(receipt: _memoryCorrectionReceipt!, onReview: _reviewMemoryCorrection),
          ],
          const SizedBox(height: 16),
          Flexible(
            flex: 2,
            child: SizedBox(
              height: 100,
              width: double.infinity,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: _lastUserText.isEmpty && _ellaDisplayText.isEmpty && _currentWords.isEmpty
                    ? const SizedBox.shrink()
                    : SingleChildScrollView(
                        controller: _transcriptScrollController,
                        child: Column(
                          children: [
                            if (_lastUserText.isNotEmpty)
                              Opacity(
                                opacity: 0.6,
                                child: Text(
                                  _lastUserText,
                                  style: EllaTextStyles.secondary,
                                  textAlign: TextAlign.center,
                                ),
                              ),
                            if (_lastUserText.isNotEmpty && (_ellaDisplayText.isNotEmpty || _currentWords.isNotEmpty))
                              const SizedBox(height: 8),
                            if (_ellaDisplayText.isNotEmpty || _currentWords.isNotEmpty)
                              Text(
                                _ellaDisplayText.isNotEmpty ? _ellaDisplayText : _currentWords,
                                style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
                                textAlign: TextAlign.center,
                              ),
                          ],
                        ),
                      ),
              ),
            ),
          ),
          const Spacer(flex: 3),
        ],
      ),
    );
    if (widget.modalPresentation) {
      return VoiceModalScaffold(
        voiceActive: _hasActiveVoiceSession,
        onEnd: _endModalVoiceSession,
        title: context.l10n.voiceChatTitle,
        child: body,
      );
    }

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        automaticallyImplyLeading: widget.sessionScope != null || widget.demoState != null,
        backgroundColor: EllaColors.bgPrimary,
        title: Text(
          context.l10n.voiceChatTitle,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
        ),
        elevation: 0,
        centerTitle: true,
      ),
      body: body,
    );
  }
}

String _compactVoiceTime(int seconds) {
  if (seconds < 60) return '<1 min';
  final minutes = (seconds / 60).ceil();
  if (minutes < 60) return '$minutes min';
  final hours = minutes ~/ 60;
  final remainder = minutes % 60;
  return remainder == 0 ? '$hours hr' : '$hours hr $remainder min';
}

String _policyStatus(BuildContext context, EllaVoicePolicyReason reason) => switch (reason) {
      EllaVoicePolicyReason.quotaDaily => context.l10n.ellaVoiceDailyRestTitle,
      EllaVoicePolicyReason.quotaMonthly => context.l10n.ellaVoiceMonthlyRestTitle,
      EllaVoicePolicyReason.concurrent => context.l10n.ellaVoiceConcurrentTitle,
      EllaVoicePolicyReason.suspended => context.l10n.ellaVoicePausedTitle,
      EllaVoicePolicyReason.sessionMax => context.l10n.ellaVoiceSessionCompleteTitle,
    };

String _policyBody(BuildContext context, EllaVoicePolicyReason reason, {DateTime? resetsAt}) => switch (reason) {
      EllaVoicePolicyReason.quotaDaily => context.l10n.ellaVoiceDailyRestBody,
      EllaVoicePolicyReason.quotaMonthly => context.l10n.ellaVoiceMonthlyRestBody,
      EllaVoicePolicyReason.concurrent => context.l10n.ellaVoiceConcurrentBody,
      EllaVoicePolicyReason.suspended => context.l10n.ellaVoicePausedBody,
      EllaVoicePolicyReason.sessionMax => context.l10n.ellaVoiceSessionCompleteBody,
    };

class _VoiceLimitNotice extends StatelessWidget {
  const _VoiceLimitNotice({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 28),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
        decoration: BoxDecoration(
          color: EllaColors.card,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
          border: Border.all(color: EllaColors.cardEdge),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Icon(Icons.schedule_rounded, color: EllaColors.tealDeep, size: 20),
            const SizedBox(width: 10),
            Expanded(
              child: Text(text, style: EllaTextStyles.caption.copyWith(color: EllaColors.ink)),
            ),
          ],
        ),
      ),
    );
  }
}
