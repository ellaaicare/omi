import 'dart:async';

import 'package:audio_session/audio_session.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:just_audio/just_audio.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/memory_talk_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef MemoryTalkCorrectionSubmitter = Future<ConversationCorrectionSubmission?> Function({
  required String conversationId,
  required String correctionText,
  String? summaryTitle,
  String? summaryOverview,
  String? appSummary,
});

typedef MemoryTalkCorrectionReceiptLoader = Future<ConversationCorrectionReceipt?> Function({
  required String conversationId,
  required String correctionId,
});

typedef MemoryTalkAmbientCapturePauser = Future<bool> Function();
typedef MemoryTalkAmbientCaptureResumer = Future<void> Function();

class MemoryTalkSheetResult {
  final bool discussed;
  final MemoryTalkReceipt? receipt;

  const MemoryTalkSheetResult({
    required this.discussed,
    this.receipt,
  });
}

class _TalkTurn {
  final bool isElla;
  String text;

  _TalkTurn({required this.isElla, required this.text});
}

enum _TalkInputMode { voice, keyboard }

Future<MemoryTalkSheetResult?> showMemoryTalkSheet(
  BuildContext context, {
  required ServerConversation conversation,
  MemoryTalkAmbientCapturePauser? pauseAmbientCapture,
  MemoryTalkAmbientCaptureResumer? resumeAmbientCapture,
  MemoryTalkCorrectionSubmitter? correctionSubmitter,
  MemoryTalkCorrectionReceiptLoader? correctionReceiptLoader,
}) async {
  if (!SharedPreferencesUtil().demoMode && !SharedPreferencesUtil().aiConsentAccepted) {
    final accepted = await AiConsentSheet.show(context);
    if (accepted != true || !context.mounted) return null;
  }

  if (!context.mounted) return null;
  CaptureProvider? captureProvider;
  try {
    captureProvider = Provider.of<CaptureProvider>(context, listen: false);
  } catch (_) {
    // Memory detail can be rendered without the app provider tree in tests and previews.
  }

  final pause = pauseAmbientCapture ??
      () async {
        if (captureProvider?.recordingState != RecordingState.record) return false;
        await captureProvider!.stopStreamRecording();
        return true;
      };
  final resume = resumeAmbientCapture ??
      () async {
        await captureProvider?.streamRecording();
      };
  final resourcesReleased = Completer<void>();
  var shouldResumeCapture = false;

  try {
    shouldResumeCapture = await pause();
    if (!context.mounted) return null;
    final result = await showModalBottomSheet<MemoryTalkSheetResult>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      barrierColor: EllaColors.ink.withValues(alpha: 0.35),
      builder: (_) => MemoryTalkSheet(
        conversation: conversation,
        correctionSubmitter: correctionSubmitter,
        correctionReceiptLoader: correctionReceiptLoader,
        onResourcesReleased: () {
          if (!resourcesReleased.isCompleted) resourcesReleased.complete();
        },
      ),
    );
    await resourcesReleased.future;
    return result;
  } finally {
    if (shouldResumeCapture) await resume();
  }
}

class MemoryTalkSheet extends StatefulWidget {
  final ServerConversation conversation;
  final MemoryTalkCorrectionSubmitter? correctionSubmitter;
  final MemoryTalkCorrectionReceiptLoader? correctionReceiptLoader;
  final VoidCallback? onResourcesReleased;

  const MemoryTalkSheet({
    super.key,
    required this.conversation,
    this.correctionSubmitter,
    this.correctionReceiptLoader,
    this.onResourcesReleased,
  });

  @override
  State<MemoryTalkSheet> createState() => _MemoryTalkSheetState();
}

class _MemoryTalkSheetState extends State<MemoryTalkSheet> {
  final _controller = TextEditingController();
  final _focusNode = FocusNode();
  final _scrollController = ScrollController();
  final _speech = SpeechToText();
  final _audioPlayer = AudioPlayer();
  final List<_TalkTurn> _turns = [];

  _TalkInputMode _inputMode = _TalkInputMode.voice;
  VoiceOrbState _orbState = VoiceOrbState.listening;
  MemoryCorrectionClaim? _pendingCorrection;
  Timer? _openingTimer;
  Timer? _demoSpeechTimer;
  Completer<void>? _demoSpeechCompleter;
  bool _hasDiscussed = false;
  bool _isSending = false;
  bool _voiceModeActive = true;
  bool _speechAvailable = false;
  bool _isStartingListening = false;
  bool _isProcessingVoiceTurn = false;
  bool _openingSpoken = false;
  bool _isOpeningSpeaking = false;
  double _audioLevel = 0;
  String _currentWords = '';
  String _liveTranscript = '';
  int _ambiguousReplyCount = 0;
  Future<void>? _resourceRelease;
  bool _didNotifyResourcesReleased = false;

  bool get _isDemoMode => SharedPreferencesUtil().demoMode;

  @override
  void initState() {
    super.initState();
    _loadHistory();
    if (!_isDemoMode) unawaited(_initSpeech());
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      setState(() => _orbState = VoiceOrbState.listening);
      _openingTimer = Timer(const Duration(milliseconds: 450), () {
        if (mounted) unawaited(_speakOpening());
      });
    });
  }

  Future<void> _loadHistory() async {
    if (!widget.conversation.memoryTalkState.hasDiscussion) return;
    final history = await fetchMemoryTalkHistory(widget.conversation.id);
    if (!mounted || history.isEmpty || _turns.isNotEmpty || _hasDiscussed) return;
    setState(() {
      _turns
        ..clear()
        ..addAll(
          history.map(
            (turn) => _TalkTurn(
              isElla: turn.role == 'assistant',
              text: turn.text,
            ),
          ),
        );
      _hasDiscussed = true;
    });
    _scrollToEnd();
  }

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        if (!mounted || _inputMode != _TalkInputMode.voice) return;
        if (error.errorMsg == 'error_no_match' && _currentWords.isNotEmpty) {
          unawaited(_processVoiceTranscript(_currentWords));
        } else if (_voiceModeActive && _openingSpoken && !_isSending) {
          unawaited(_startListening());
        }
      },
      onStatus: (status) {
        if (!mounted || _inputMode != _TalkInputMode.voice) return;
        if (status == 'notListening' && _orbState == VoiceOrbState.listening) {
          if (_currentWords.isNotEmpty) {
            unawaited(_processVoiceTranscript(_currentWords));
          } else if (_voiceModeActive && _openingSpoken && !_isSending) {
            unawaited(_startListening());
          }
        }
      },
    );
  }

  @override
  void dispose() {
    _voiceModeActive = false;
    _openingTimer?.cancel();
    _cancelDemoSpeech();
    _controller.dispose();
    _focusNode.dispose();
    _scrollController.dispose();
    unawaited(_releaseAudioResources());
    super.dispose();
  }

  Future<void> _releaseAudioResources() {
    return _resourceRelease ??= () async {
      try {
        if (!_isDemoMode) {
          if (_speech.isListening) await _speech.stop();
          await _audioPlayer.stop();
          await ElevenLabsTts.stopOnDevice();
          await _audioPlayer.dispose();
        }
      } finally {
        if (!_didNotifyResourcesReleased) {
          _didNotifyResourcesReleased = true;
          widget.onResourcesReleased?.call();
        }
      }
    }();
  }

  Future<void> _closeWithResult(MemoryTalkSheetResult result) async {
    _voiceModeActive = false;
    _openingTimer?.cancel();
    _cancelDemoSpeech();
    await _releaseAudioResources();
    if (mounted) Navigator.of(context).pop(result);
  }

  String _whenLabel() {
    final date = widget.conversation.startedAt ?? widget.conversation.createdAt;
    final now = DateTime.now();
    final sameDay = date.year == now.year && date.month == now.month && date.day == now.day;
    if (sameDay && date.hour < 12) return context.l10n.memoryTalkThisMorning;
    if (sameDay) return context.l10n.memoryTalkEarlierToday;
    return context.l10n.memoryTalkOnDate('${date.month}/${date.day}');
  }

  String _openingTitle() {
    final title = widget.conversation.structured.title;
    final withIndex = title.toLowerCase().lastIndexOf(' with ');
    if (withIndex > 0) return title.substring(0, withIndex).toLowerCase();
    return title.toLowerCase();
  }

  String get _openingLine => context.l10n.memoryTalkOpening(_whenLabel(), _openingTitle());

  String _correctionContextPhrase() {
    return _openingTitle().contains('garden') ? context.l10n.memoryTalkCorrectionAtTheGarden : '';
  }

  void _addEllaTurn(String text) {
    if (!mounted) return;
    setState(() => _turns.add(_TalkTurn(isElla: true, text: text)));
    _scrollToEnd();
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _speakOpening() async {
    if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive || _openingSpoken) return;
    _openingSpoken = true;
    setState(() {
      _isOpeningSpeaking = true;
      _orbState = VoiceOrbState.speaking;
      _audioLevel = 0;
    });

    if (_isDemoMode) {
      await _demoSpeechDelay(const Duration(seconds: 8));
    } else {
      await ElevenLabsTts.speakOnDevice(_openingLine);
    }

    if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
    setState(() => _isOpeningSpeaking = false);
    await _startListening();
  }

  Future<void> _startListening() async {
    if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive || _isSending) return;
    if (_isStartingListening) return;
    _isStartingListening = true;

    try {
      if (_isDemoMode) {
        if (mounted) {
          setState(() {
            _orbState = VoiceOrbState.listening;
            _audioLevel = 0;
            _liveTranscript = '';
          });
        }
        return;
      }
      if (!SharedPreferencesUtil().aiConsentAccepted) return;

      final micStatus = await Permission.microphone.request();
      final speechStatus = await Permission.speech.request();
      if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
      if (!micStatus.isGranted || !speechStatus.isGranted) {
        setState(() => _orbState = VoiceOrbState.idle);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.voiceMicPermissionRequired)),
        );
        return;
      }

      if (!_speechAvailable) await _initSpeech();
      if (!mounted || !_speechAvailable) {
        if (mounted) {
          setState(() => _orbState = VoiceOrbState.idle);
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(context.l10n.voiceError)),
          );
        }
        return;
      }

      await _audioPlayer.stop();
      if (_speech.isListening) await _speech.stop();
      await Future<void>.delayed(const Duration(milliseconds: 250));
      if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;

      _currentWords = '';
      setState(() {
        _orbState = VoiceOrbState.listening;
        _audioLevel = 0;
        _liveTranscript = '';
      });

      await _listenWithSpeechRecognizer();
    } finally {
      _isStartingListening = false;
    }
  }

  Future<void> _listenWithSpeechRecognizer() async {
    Future<void> listen() {
      return _speech.listen(
        onResult: _onSpeechResult,
        onSoundLevelChange: (level) {
          if (!mounted || _orbState != VoiceOrbState.listening) return;
          setState(() => _audioLevel = ((level + 2) / 12).clamp(0.0, 1.0));
        },
        pauseFor: const Duration(seconds: 3),
        listenFor: const Duration(seconds: 60),
        listenOptions: SpeechListenOptions(
          listenMode: ListenMode.dictation,
          cancelOnError: false,
          partialResults: true,
        ),
      );
    }

    try {
      await listen();
    } catch (_) {
      await Future<void>.delayed(const Duration(milliseconds: 500));
      if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
      try {
        await listen();
      } catch (_) {
        if (!mounted) return;
        setState(() {
          _orbState = VoiceOrbState.idle;
          _voiceModeActive = false;
        });
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.voiceError)),
        );
      }
    }
  }

  void _onSpeechResult(SpeechRecognitionResult result) {
    if (!mounted || _inputMode != _TalkInputMode.voice) return;
    _currentWords = result.recognizedWords.trim();
    setState(() => _liveTranscript = _currentWords);
    if (result.finalResult && _currentWords.isNotEmpty) {
      unawaited(_processVoiceTranscript(_currentWords));
    }
  }

  Future<void> _processVoiceTranscript(String transcript) async {
    if (_isProcessingVoiceTurn || transcript.trim().isEmpty) return;
    _isProcessingVoiceTurn = true;
    _currentWords = '';
    if (_speech.isListening) await _speech.stop();
    if (mounted) {
      setState(() {
        _orbState = VoiceOrbState.processing;
        _audioLevel = 0;
        _liveTranscript = '';
      });
    }
    try {
      await _submitText(transcript.trim(), fromVoice: true);
    } finally {
      _isProcessingVoiceTurn = false;
    }
  }

  Future<void> _submitKeyboardText() async {
    final text = _controller.text.trim();
    if (text.isEmpty) return;
    _controller.clear();
    await _submitText(text);
  }

  Future<void> _submitText(String text, {bool fromVoice = false}) async {
    if (text.trim().isEmpty || _isSending) return;
    HapticFeedback.lightImpact();
    setState(() {
      _turns.add(_TalkTurn(isElla: false, text: text));
      _hasDiscussed = true;
      _isSending = true;
      if (fromVoice) _orbState = VoiceOrbState.processing;
    });
    _scrollToEnd();

    String? spokenResponse;
    final pending = _pendingCorrection;
    if (pending != null) {
      spokenResponse = await _handleConfirmation(text, pending, fromVoice: fromVoice);
    } else {
      final claim = extractCorrectionClaim(text, widget.conversation);
      if (claim != null) {
        _pendingCorrection = claim;
        _ambiguousReplyCount = 0;
        final confirmation = context.l10n.memoryTalkCorrectionConfirmation(
          claim.newValue,
          _correctionContextPhrase(),
          claim.oldValue,
        );
        _addEllaTurn(confirmation);
        await _persistCorrectionExchange(text, confirmation);
        spokenResponse = confirmation;
      } else {
        spokenResponse = await _sendScopedChat(text);
      }
    }

    if (!mounted) return;
    setState(() => _isSending = false);
    if (fromVoice && spokenResponse != null && spokenResponse.trim().isNotEmpty) {
      await _speakEllaResponse(spokenResponse);
    } else if (fromVoice && _inputMode == _TalkInputMode.voice && _voiceModeActive) {
      await _startListening();
    }
  }

  Future<String?> _handleConfirmation(
    String reply,
    MemoryCorrectionClaim claim, {
    required bool fromVoice,
  }) async {
    switch (classifyCorrectionReply(reply)) {
      case CorrectionReplyIntent.affirmative:
        await _applyCorrection(claim, reply, speakAcknowledgement: fromVoice);
        return null;
      case CorrectionReplyIntent.negative:
        _pendingCorrection = null;
        _ambiguousReplyCount = 0;
        final response = context.l10n.memoryTalkCorrectionDiscarded;
        _addEllaTurn(response);
        await _persistCorrectionExchange(reply, response);
        return response;
      case CorrectionReplyIntent.ambiguous:
        late final String response;
        if (_ambiguousReplyCount == 0) {
          _ambiguousReplyCount = 1;
          response = context.l10n.memoryTalkCorrectionReprompt;
        } else {
          _pendingCorrection = null;
          _ambiguousReplyCount = 0;
          response = context.l10n.memoryTalkCorrectionNotChanged;
        }
        _addEllaTurn(response);
        await _persistCorrectionExchange(reply, response);
        return response;
    }
  }

  Future<void> _persistCorrectionExchange(String userText, String ellaText) async {
    await appendMemoryTalkTurns(
      widget.conversation.id,
      [
        MemoryTalkHistoryTurn(role: 'user', text: userText, createdAt: DateTime.now()),
        MemoryTalkHistoryTurn(role: 'assistant', text: ellaText, createdAt: DateTime.now()),
      ],
    );
  }

  Future<String> _sendScopedChat(String text) async {
    if (_isDemoMode) {
      final response = context.l10n.memoryTalkDemoReply;
      await Future<void>.delayed(const Duration(milliseconds: 260));
      if (mounted) _addEllaTurn(response);
      return response;
    }

    final responseTurn = _TalkTurn(isElla: true, text: '');
    setState(() => _turns.add(responseTurn));
    try {
      await for (final chunk in sendEllaChatStream(text, conversationId: widget.conversation.id)) {
        if (!mounted) return '';
        if (chunk.type == MessageChunkType.data) {
          setState(() => responseTurn.text += chunk.text);
        } else if (chunk.type == MessageChunkType.done && responseTurn.text.isEmpty) {
          setState(() => responseTurn.text = chunk.message?.text ?? chunk.text);
        } else if (chunk.type == MessageChunkType.error) {
          setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
        }
        _scrollToEnd();
      }
      if (mounted && responseTurn.text.trim().isEmpty) {
        setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
      }
    } catch (_) {
      if (mounted) setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
    }
    return responseTurn.text;
  }

  Future<void> _speakEllaResponse(String text, {bool resumeListening = true}) async {
    if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
    setState(() {
      _orbState = VoiceOrbState.speaking;
      _audioLevel = 0;
    });

    if (_isDemoMode) {
      await _demoSpeechDelay(const Duration(milliseconds: 900));
    } else {
      final audioPath = await ElevenLabsTts.synthesize(text);
      if (!mounted || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
      if (audioPath == null) {
        await ElevenLabsTts.speakOnDevice(text);
      } else {
        try {
          await _prepareTtsPlaybackSession();
          await _audioPlayer.setVolume(1);
          await _audioPlayer.setFilePath(audioPath);
          await _audioPlayer.play();
        } catch (_) {
          await ElevenLabsTts.speakOnDevice(text);
        }
      }
    }

    if (!mounted || !resumeListening || _inputMode != _TalkInputMode.voice || !_voiceModeActive) return;
    await _startListening();
  }

  Future<void> _prepareTtsPlaybackSession() async {
    final session = await AudioSession.instance;
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
    await session.setActive(true);
  }

  Future<void> _demoSpeechDelay(Duration duration) {
    _cancelDemoSpeech();
    final completer = Completer<void>();
    _demoSpeechCompleter = completer;
    _demoSpeechTimer = Timer(duration, () {
      if (!completer.isCompleted) completer.complete();
      if (identical(_demoSpeechCompleter, completer)) {
        _demoSpeechCompleter = null;
        _demoSpeechTimer = null;
      }
    });
    return completer.future;
  }

  void _cancelDemoSpeech() {
    _demoSpeechTimer?.cancel();
    _demoSpeechTimer = null;
    final completer = _demoSpeechCompleter;
    _demoSpeechCompleter = null;
    if (completer != null && !completer.isCompleted) completer.complete();
  }

  Future<void> _applyCorrection(
    MemoryCorrectionClaim claim,
    String reply, {
    required bool speakAcknowledgement,
  }) async {
    final response = context.l10n.memoryTalkApplyingCorrection;
    _addEllaTurn(response);
    await _persistCorrectionExchange(reply, response);
    if (speakAcknowledgement) {
      await _speakEllaResponse(response, resumeListening: false);
    }

    if (_isDemoMode && widget.correctionSubmitter == null) {
      await Future<void>.delayed(const Duration(milliseconds: 320));
      final beforeTitle = widget.conversation.structured.title;
      final beforeOverview = widget.conversation.structured.overview;
      final receipt = MemoryTalkReceipt(
        correctionId: 'demo-${DateTime.now().millisecondsSinceEpoch}',
        oldValue: claim.oldValue,
        newValue: claim.newValue,
        beforeTitle: beforeTitle,
        beforeOverview: beforeOverview,
        afterTitle: replaceMemoryValue(beforeTitle, claim),
        afterOverview: replaceMemoryValue(beforeOverview, claim),
        appliedAt: DateTime.now(),
        propagated: true,
      );
      if (!mounted) return;
      await _closeWithResult(MemoryTalkSheetResult(discussed: true, receipt: receipt));
      return;
    }

    final submission = await (widget.correctionSubmitter ?? submitConversationCorrection)(
      conversationId: widget.conversation.id,
      correctionText: claim.correctionText,
      summaryTitle: widget.conversation.structured.title,
      summaryOverview: widget.conversation.structured.overview,
      appSummary: widget.conversation.structured.overview,
    );
    if (!mounted) return;
    if (submission == null || submission.correctionId.isEmpty) {
      _pendingCorrection = null;
      final failed = context.l10n.memoryTalkCorrectionFailed;
      _addEllaTurn(failed);
      if (speakAcknowledgement) await _speakEllaResponse(failed);
      return;
    }

    final receipt = await _waitForReceipt(submission.correctionId);
    if (!mounted) return;
    if (receipt == null || !receipt.isApplied) {
      _pendingCorrection = null;
      // A terminal failed receipt will never continue applying, so it must use
      // failure copy rather than the "still working" (timeout) message.
      final isTerminalFailure = receipt != null && receipt.status.toLowerCase().contains('failed');
      final message =
          isTerminalFailure ? context.l10n.memoryTalkCorrectionFailed : context.l10n.memoryTalkCorrectionStillWorking;
      _addEllaTurn(message);
      if (speakAcknowledgement) await _speakEllaResponse(message);
      return;
    }
    await _closeWithResult(
      MemoryTalkSheetResult(
        discussed: true,
        receipt: MemoryTalkReceipt.fromApi(receipt, claim: claim),
      ),
    );
  }

  Future<ConversationCorrectionReceipt?> _waitForReceipt(String correctionId) async {
    for (var attempt = 0; attempt < 12; attempt += 1) {
      final receipt = await (widget.correctionReceiptLoader ?? getConversationCorrectionReceipt)(
        conversationId: widget.conversation.id,
        correctionId: correctionId,
      );
      if (receipt != null && (receipt.isApplied || receipt.status.contains('failed'))) {
        return receipt;
      }
      await Future<void>.delayed(const Duration(seconds: 2));
    }
    return null;
  }

  Future<void> _toggleInputMode() async {
    HapticFeedback.selectionClick();
    if (_inputMode == _TalkInputMode.voice) {
      _voiceModeActive = false;
      _openingTimer?.cancel();
      _cancelDemoSpeech();
      if (_speech.isListening) await _speech.stop();
      await _audioPlayer.stop();
      await ElevenLabsTts.stopOnDevice();
      if (!mounted) return;
      setState(() {
        _inputMode = _TalkInputMode.keyboard;
        _orbState = VoiceOrbState.idle;
        _isOpeningSpeaking = false;
        _liveTranscript = '';
      });
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _focusNode.requestFocus();
      });
      return;
    }

    _focusNode.unfocus();
    setState(() {
      _inputMode = _TalkInputMode.voice;
      _voiceModeActive = true;
      _orbState = VoiceOrbState.listening;
      _audioLevel = 0;
    });
    if (_openingSpoken) {
      await _startListening();
    } else {
      await _speakOpening();
    }
  }

  Future<void> _finish() async {
    await _closeWithResult(MemoryTalkSheetResult(discussed: _hasDiscussed));
  }

  double _sheetFraction() {
    if (_inputMode == _TalkInputMode.keyboard) return 0.60;
    if (_pendingCorrection != null || _turns.length >= 2) return 0.70;
    if (_isOpeningSpeaking || _orbState == VoiceOrbState.speaking) return 0.64;
    return 0.56;
  }

  Widget _buildVoiceBody() {
    final isConfirmation = _pendingCorrection != null;
    final coreSize = isConfirmation
        ? 72.0
        : _isOpeningSpeaking
            ? 96.0
            : 110.0;
    final containerSize = isConfirmation
        ? 82.0
        : _isOpeningSpeaking
            ? 106.0
            : 150.0;
    final status = _orbState == VoiceOrbState.speaking
        ? context.l10n.voiceEllaSpeaking
        : _orbState == VoiceOrbState.processing
            ? context.l10n.voiceEllaThinking
            : context.l10n.voiceListening;

    return Expanded(
      child: SingleChildScrollView(
        child: Column(
          children: [
            SizedBox(height: isConfirmation ? 20 : (_isOpeningSpeaking ? 24 : 26)),
            Semantics(
              label: status,
              button: true,
              child: EllaVoiceOrb(
                state: _orbState,
                audioLevel: _audioLevel,
                onTap: () {
                  if (_orbState == VoiceOrbState.idle) {
                    _voiceModeActive = true;
                    unawaited(_startListening());
                  }
                },
                size: containerSize,
                coreSize: coreSize,
                ring1Size: 132,
                ring2Size: 146,
                iconSize: isConfirmation ? 22 : 28,
                compactRings: true,
                showRings: !isConfirmation && !_isOpeningSpeaking,
              ),
            ),
            if (_isOpeningSpeaking) ...[
              const SizedBox(height: 24),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 300),
                child: Text(
                  _openingLine,
                  textAlign: TextAlign.center,
                  style: EllaTextStyles.noteBody,
                ),
              ),
            ] else if (isConfirmation) ...[
              const SizedBox(height: 22),
              _buildConfirmationTranscript(),
            ] else if (_turns.isNotEmpty) ...[
              const SizedBox(height: 16),
              for (final turn in _turns) _TalkBubble(turn: turn),
            ] else if (_liveTranscript.isNotEmpty) ...[
              const SizedBox(height: 18),
              Text(
                _liveTranscript,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
              ),
            ],
            if (!isConfirmation || _turns.isEmpty)
              Padding(
                padding: EdgeInsets.only(top: _isOpeningSpeaking ? 18 : 24),
                child: _VoiceStatus(text: status, live: _orbState == VoiceOrbState.listening),
              ),
            if (isConfirmation)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: _VoiceStatus(text: status, live: _orbState == VoiceOrbState.listening),
              ),
          ],
        ),
      ),
    );
  }

  Widget _buildConfirmationTranscript() {
    final userTurns = _turns.where((turn) => !turn.isElla).toList();
    final ellaTurns = _turns.where((turn) => turn.isElla).toList();
    final latestElla = ellaTurns.isEmpty ? null : ellaTurns.last;
    final visibleUsers = userTurns.length > 2 ? userTurns.sublist(userTurns.length - 2) : userTurns;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var index = 0; index < visibleUsers.length; index += 1)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Opacity(
              opacity: index == visibleUsers.length - 1 ? 1 : 0.6,
              child: Text(
                visibleUsers[index].text,
                textAlign: TextAlign.right,
                style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
              ),
            ),
          ),
        if (latestElla != null) ...[
          const SizedBox(height: 10),
          Text(
            latestElla.text,
            textAlign: TextAlign.center,
            style: EllaTextStyles.noteBody,
          ),
        ],
      ],
    );
  }

  Widget _buildKeyboardBody() {
    return Expanded(
      child: LayoutBuilder(
        builder: (context, constraints) => SingleChildScrollView(
          controller: _scrollController,
          padding: EdgeInsets.zero,
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: constraints.maxHeight),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                SizedBox(height: _turns.isEmpty ? 28 : 18),
                Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 300),
                    child: Text(
                      _openingLine,
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.noteBody,
                    ),
                  ),
                ),
                if (_turns.isNotEmpty) ...[
                  const SizedBox(height: 18),
                  for (final turn in _turns) _TalkBubble(turn: turn),
                ],
                SizedBox(height: _turns.isEmpty ? 24 : 10),
                Container(
                  constraints: const BoxConstraints(minHeight: 48),
                  decoration: BoxDecoration(
                    color: EllaColors.card,
                    borderRadius: BorderRadius.circular(24),
                    border: Border.all(color: _focusNode.hasFocus ? EllaColors.teal : EllaColors.cardDeep, width: 1.5),
                  ),
                  padding: const EdgeInsets.only(left: 18, right: 8),
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _controller,
                          focusNode: _focusNode,
                          enabled: !_isSending,
                          minLines: 1,
                          maxLines: 3,
                          textInputAction: TextInputAction.send,
                          onSubmitted: (_) => _submitKeyboardText(),
                          onTap: () => setState(() {}),
                          style: EllaTextStyles.secondary.copyWith(
                            color: EllaColors.ink,
                            fontSize: 14.5,
                            height: 1.1,
                            letterSpacing: -0.2,
                          ),
                          decoration: InputDecoration(
                            hintText: context.l10n.memoryTalkComposerHint,
                            hintStyle: EllaTextStyles.secondary,
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                            disabledBorder: InputBorder.none,
                            errorBorder: InputBorder.none,
                            focusedErrorBorder: InputBorder.none,
                            isDense: true,
                            contentPadding: EdgeInsets.zero,
                          ),
                        ),
                      ),
                      Semantics(
                        button: true,
                        label: context.l10n.memoryTalkSend,
                        child: IconButton(
                          onPressed: _isSending ? null : _submitKeyboardText,
                          icon: _isSending
                              ? const SizedBox(
                                  width: 20,
                                  height: 20,
                                  child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                                )
                              : const FaIcon(FontAwesomeIcons.paperPlane, size: 18),
                          color: EllaColors.tealDeep,
                          style: IconButton.styleFrom(
                            disabledForegroundColor: EllaColors.inkSoft,
                            padding: EdgeInsets.zero,
                            minimumSize: const Size(36, 36),
                            maximumSize: const Size(36, 36),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                SizedBox(height: _turns.isEmpty ? 76 : 4),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildFooter() {
    final showKeyboardToggle = _inputMode == _TalkInputMode.voice;
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        SizedBox(
          height: 48,
          width: 132,
          child: FilledButton(
            onPressed: _finish,
            style: FilledButton.styleFrom(
              backgroundColor: EllaColors.cardDeep,
              foregroundColor: EllaColors.tealDeep,
              minimumSize: const Size(132, 48),
              maximumSize: const Size(132, 48),
              shape: const StadiumBorder(),
            ),
            child: Text(
              context.l10n.memoryTalkDone,
              style: const TextStyle(
                fontFamily: EllaTextStyles.uiFont,
                fontSize: 17,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ),
        const SizedBox(width: 12),
        SizedBox(
          width: 48,
          height: 48,
          child: DecoratedBox(
            decoration: const BoxDecoration(
              color: EllaColors.cardDeep,
              shape: BoxShape.circle,
            ),
            child: IconButton(
              tooltip: showKeyboardToggle ? context.l10n.memoryTalkUseKeyboard : context.l10n.memoryTalkUseVoice,
              onPressed: _toggleInputMode,
              icon: FaIcon(
                showKeyboardToggle ? FontAwesomeIcons.keyboard : FontAwesomeIcons.microphone,
                size: showKeyboardToggle ? 20 : 18,
                color: EllaColors.tealDeep,
              ),
              style: IconButton.styleFrom(
                backgroundColor: Colors.transparent,
                minimumSize: const Size(48, 48),
                maximumSize: const Size(48, 48),
              ),
            ),
          ),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.viewInsetsOf(context).bottom;
    final view = View.of(context);
    final screenHeight = view.physicalSize.height / view.devicePixelRatio;
    final sheetHeight = (screenHeight * _sheetFraction()).clamp(460.0, 650.0).toDouble();

    return AnimatedPadding(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
      padding: EdgeInsets.only(bottom: bottomInset),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
        height: sheetHeight,
        decoration: const BoxDecoration(
          color: EllaColors.paper,
          borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
        ),
        padding: EdgeInsets.fromLTRB(
          _inputMode == _TalkInputMode.voice ? 28 : 20,
          14,
          _inputMode == _TalkInputMode.voice ? 28 : 20,
          12,
        ),
        child: SafeArea(
          top: false,
          child: Column(
            children: [
              Container(
                width: 44,
                height: 4,
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(999),
                ),
              ),
              const SizedBox(height: 20),
              Text(context.l10n.memoryTalkTalkingAbout, style: EllaTextStyles.eyebrow),
              const SizedBox(height: 4),
              Text(
                widget.conversation.structured.title,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontFamily: EllaTextStyles.uiFont,
                  color: EllaColors.ink,
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                  height: 1.3,
                ),
              ),
              if (_inputMode == _TalkInputMode.voice) _buildVoiceBody() else _buildKeyboardBody(),
              _buildFooter(),
            ],
          ),
        ),
      ),
    );
  }
}

class _VoiceStatus extends StatelessWidget {
  final String text;
  final bool live;

  const _VoiceStatus({
    required this.text,
    required this.live,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        EllaBreathingDot(active: true, live: live),
        const SizedBox(width: 8),
        Text(text, style: EllaTextStyles.caption),
      ],
    );
  }
}

class _TalkBubble extends StatelessWidget {
  final _TalkTurn turn;

  const _TalkBubble({required this.turn});

  @override
  Widget build(BuildContext context) {
    if (turn.isElla) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 14),
        child: Text(
          turn.text,
          textAlign: TextAlign.center,
          style: EllaTextStyles.noteBody,
        ),
      );
    }
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        constraints: const BoxConstraints(maxWidth: 315),
        margin: const EdgeInsets.only(bottom: 14),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color: EllaColors.card,
          borderRadius: BorderRadius.circular(18),
        ),
        child: Text(
          turn.text,
          style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
          textAlign: TextAlign.right,
        ),
      ),
    );
  }
}
