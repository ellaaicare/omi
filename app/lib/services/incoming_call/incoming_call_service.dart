import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/services/incoming_call/voice_answer_detector.dart';
import 'package:omi/services/voice_mode_v2/voice_mode_v2_service.dart';

/// Incoming call states
enum IncomingCallState {
  idle,        // No incoming call
  ringing,     // Call incoming, waiting for answer
  answering,   // User answered, connecting
  declined,    // User declined, playing voicemail
  timeout,     // Call timed out
}

/// Incoming call data from push notification
class IncomingCallData {
  final String callId;
  final String reason;
  final String reasonDisplay;
  final String priority;
  final bool autoAnswer;
  final int timeoutSeconds;
  final String? voicemailText;
  final String? voicemailAudioUrl;
  final Map<String, dynamic>? context;

  IncomingCallData({
    required this.callId,
    required this.reason,
    required this.reasonDisplay,
    this.priority = 'normal',
    this.autoAnswer = false,
    this.timeoutSeconds = 30,
    this.voicemailText,
    this.voicemailAudioUrl,
    this.context,
  });

  factory IncomingCallData.fromPushData(Map<String, dynamic> data) {
    return IncomingCallData(
      callId: data['call_id'] ?? DateTime.now().millisecondsSinceEpoch.toString(),
      reason: data['reason'] ?? 'unknown',
      reasonDisplay: data['reason_display'] ?? data['reason'] ?? 'Ella is calling',
      priority: data['priority'] ?? 'normal',
      autoAnswer: data['auto_answer'] == true || data['auto_answer'] == 'true',
      timeoutSeconds: int.tryParse(data['timeout_seconds']?.toString() ?? '30') ?? 30,
      voicemailText: data['voicemail_text'],
      voicemailAudioUrl: data['voicemail_audio_url'],
      context: data['context'] is Map ? Map<String, dynamic>.from(data['context']) : null,
    );
  }
}

/// Service to handle incoming calls from Ella
class IncomingCallService extends ChangeNotifier {
  static final IncomingCallService _instance = IncomingCallService._internal();
  factory IncomingCallService() => _instance;
  IncomingCallService._internal();

  // State
  IncomingCallState _state = IncomingCallState.idle;
  IncomingCallState get state => _state;
  bool get isRinging => _state == IncomingCallState.ringing;

  // Current call data
  IncomingCallData? _currentCall;
  IncomingCallData? get currentCall => _currentCall;

  // Timeout
  Timer? _timeoutTimer;
  int _remainingSeconds = 0;
  int get remainingSeconds => _remainingSeconds;

  // Audio
  final AudioPlayer _ringtonePlayer = AudioPlayer();
  final AudioPlayer _voicemailPlayer = AudioPlayer();

  // Voice detection
  final VoiceAnswerDetector _voiceDetector = VoiceAnswerDetector();

  // Callbacks for UI
  VoidCallback? onCallStarted;
  VoidCallback? onCallEnded;
  Function(String)? onError;

  /// Handle incoming call from push notification
  Future<void> handleIncomingCall(Map<String, dynamic> pushData) async {
    if (_state != IncomingCallState.idle) {
      debugPrint('IncomingCallService: Already handling a call, ignoring');
      return;
    }

    final callData = IncomingCallData.fromPushData(pushData);
    debugPrint('IncomingCallService: Incoming call - ${callData.reasonDisplay} (priority: ${callData.priority})');

    _currentCall = callData;
    _state = IncomingCallState.ringing;
    _remainingSeconds = callData.timeoutSeconds;
    notifyListeners();

    // Haptic feedback
    await HapticFeedback.heavyImpact();

    // Check auto-answer
    final autoAnswerEnabled = SharedPreferencesUtil().getBool('auto_answer_calls') ?? false;
    if (callData.autoAnswer || (autoAnswerEnabled && callData.priority == 'urgent')) {
      debugPrint('IncomingCallService: Auto-answering call');
      await Future.delayed(const Duration(milliseconds: 500));
      await answerCall();
      return;
    }

    // Play ringtone
    await _playRingtone();

    // Start voice detection
    _voiceDetector.startListening(
      onAnswer: answerCall,
      onDecline: declineCall,
    );

    // Start timeout timer
    _startTimeoutTimer();

    onCallStarted?.call();
  }

  /// Answer the call - start V2 voice mode
  Future<void> answerCall() async {
    if (_state != IncomingCallState.ringing) return;

    debugPrint('IncomingCallService: Answering call ${_currentCall?.callId}');

    _state = IncomingCallState.answering;
    notifyListeners();

    // Stop ringtone and voice detection
    await _stopRingtone();
    _voiceDetector.stopListening();
    _timeoutTimer?.cancel();

    // Haptic feedback
    await HapticFeedback.mediumImpact();

    // Start V2 voice mode
    final success = await VoiceModeV2Service().start();

    if (success) {
      debugPrint('IncomingCallService: Call connected');
      // Notify backend of answer
      await _sendCallResponse('answered');
    } else {
      debugPrint('IncomingCallService: Failed to connect call');
      onError?.call('Failed to connect call');
      await _playVoicemail();
    }

    _cleanup();
  }

  /// Decline the call - play voicemail
  Future<void> declineCall() async {
    if (_state != IncomingCallState.ringing) return;

    debugPrint('IncomingCallService: Declining call ${_currentCall?.callId}');

    _state = IncomingCallState.declined;
    notifyListeners();

    // Stop ringtone and voice detection
    await _stopRingtone();
    _voiceDetector.stopListening();
    _timeoutTimer?.cancel();

    // Haptic feedback
    await HapticFeedback.lightImpact();

    // Play voicemail
    await _playVoicemail();

    // Notify backend
    await _sendCallResponse('declined');

    _cleanup();
  }

  /// Handle timeout
  Future<void> _handleTimeout() async {
    if (_state != IncomingCallState.ringing) return;

    debugPrint('IncomingCallService: Call timed out');

    _state = IncomingCallState.timeout;
    notifyListeners();

    // Stop ringtone and voice detection
    await _stopRingtone();
    _voiceDetector.stopListening();

    // Play voicemail
    await _playVoicemail();

    // Notify backend
    await _sendCallResponse('timeout');

    _cleanup();
  }

  /// Play ringtone
  Future<void> _playRingtone() async {
    try {
      // TODO: Use custom ringtone asset
      // For now, just use haptic pulses
      _startHapticPulse();
    } catch (e) {
      debugPrint('IncomingCallService: Ringtone error: $e');
    }
  }

  Timer? _hapticTimer;
  void _startHapticPulse() {
    _hapticTimer = Timer.periodic(const Duration(milliseconds: 1500), (timer) {
      if (_state == IncomingCallState.ringing) {
        HapticFeedback.heavyImpact();
      } else {
        timer.cancel();
      }
    });
  }

  Future<void> _stopRingtone() async {
    _hapticTimer?.cancel();
    try {
      await _ringtonePlayer.stop();
    } catch (_) {}
  }

  /// Play voicemail
  Future<void> _playVoicemail() async {
    final call = _currentCall;
    if (call == null) return;

    debugPrint('IncomingCallService: Playing voicemail');

    try {
      if (call.voicemailAudioUrl != null && call.voicemailAudioUrl!.isNotEmpty) {
        // Play pre-generated audio
        await _voicemailPlayer.setUrl(call.voicemailAudioUrl!);
        await _voicemailPlayer.play();
        await _voicemailPlayer.processingStateStream.firstWhere(
          (state) => state == ProcessingState.completed,
        );
      } else if (call.voicemailText != null && call.voicemailText!.isNotEmpty) {
        // TODO: Use TTS service to speak voicemail text
        debugPrint('IncomingCallService: Voicemail text: ${call.voicemailText}');
      }
    } catch (e) {
      debugPrint('IncomingCallService: Voicemail playback error: $e');
    }
  }

  /// Start timeout timer
  void _startTimeoutTimer() {
    _timeoutTimer?.cancel();
    _timeoutTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (_state != IncomingCallState.ringing) {
        timer.cancel();
        return;
      }

      _remainingSeconds--;
      notifyListeners();

      if (_remainingSeconds <= 0) {
        timer.cancel();
        _handleTimeout();
      }
    });
  }

  /// Send call response to backend via simple chat message
  /// This keeps it simple - no new endpoints needed, agent processes naturally
  Future<void> _sendCallResponse(String status) async {
    final call = _currentCall;
    if (call == null) return;

    debugPrint('IncomingCallService: Sending response - $status for call ${call.callId}');

    // Send as a simple chat message - agent processes naturally
    // This avoids needing new backend endpoints
    String message;
    switch (status) {
      case 'answered':
        // No message needed - V2 voice call handles the conversation
        return;
      case 'declined':
        message = '[Call Response] User declined the call for "${call.reasonDisplay}". Voicemail was played.';
        break;
      case 'timeout':
        message = '[Call Response] Call for "${call.reasonDisplay}" timed out after ${call.timeoutSeconds} seconds. Voicemail was played.';
        break;
      default:
        message = '[Call Response] Call ended with status: $status';
    }

    // Send via existing chat infrastructure
    // This goes to the agent who can decide what to do next
    try {
      // Use existing message sending mechanism
      // For now, just log - backend team can hook this up
      debugPrint('IncomingCallService: Would send chat message: $message');
      // TODO: Hook up to existing chat send mechanism
      // await MessageProvider().sendMessage(message, isSystemMessage: true);
    } catch (e) {
      debugPrint('IncomingCallService: Failed to send response: $e');
    }
  }

  /// Cleanup after call ends
  void _cleanup() {
    _timeoutTimer?.cancel();
    _hapticTimer?.cancel();
    _voiceDetector.stopListening();

    // Reset state after short delay (for UI transition)
    Future.delayed(const Duration(seconds: 2), () {
      _state = IncomingCallState.idle;
      _currentCall = null;
      _remainingSeconds = 0;
      notifyListeners();
      onCallEnded?.call();
    });
  }

  /// Force cancel any ongoing call
  void forceCancel() {
    debugPrint('IncomingCallService: Force cancel');
    _stopRingtone();
    _voiceDetector.stopListening();
    _timeoutTimer?.cancel();
    _hapticTimer?.cancel();
    _state = IncomingCallState.idle;
    _currentCall = null;
    _remainingSeconds = 0;
    notifyListeners();
  }

  @override
  void dispose() {
    _timeoutTimer?.cancel();
    _hapticTimer?.cancel();
    _ringtonePlayer.dispose();
    _voicemailPlayer.dispose();
    _voiceDetector.dispose();
    super.dispose();
  }
}
