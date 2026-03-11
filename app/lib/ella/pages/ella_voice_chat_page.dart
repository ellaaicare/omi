import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:just_audio/just_audio.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/utils/enums.dart';

/// Voice-to-voice chat page for Ella.
///
/// Flow: Tap orb → always-listen via on-device speech recognition →
/// auto-detect silence → send text to Ella chat → TTS → play audio → repeat.
class EllaVoiceChatPage extends StatefulWidget {
  const EllaVoiceChatPage({super.key});

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
  bool _speechAvailable = false;
  bool _didPauseCaptureRecording = false;
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
  bool _isV2VMode = false;

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
  static bool _isV2VProvider(String provider) => provider == 'grok-voice' || provider == 'gemini-live';

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
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
    _initSpeech();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Auto-start listening when user navigates TO the Voice tab (index 2)
    try {
      final homeProvider = Provider.of<HomeProvider>(context);
      final isOnVoiceTab = homeProvider.selectedIndex == 2;
      if (isOnVoiceTab && !_wasOnVoiceTab && !_voiceModeActive) {
        debugPrint('[VoiceChat] Voice tab became active, auto-starting');
        _voiceModeActive = true;
        final provider = SharedPreferencesUtil().ttsProvider;
        Future.delayed(const Duration(milliseconds: 300), () {
          if (mounted && _orbState == VoiceOrbState.idle) {
            if (_isV2VProvider(provider)) {
              _startV2V(provider);
            } else {
              _startListening();
            }
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
    _voiceModeActive = false;
    _typewriterTimer?.cancel();
    _playerSub?.cancel();
    _audioPlayer.dispose();
    _transcriptScrollController.dispose();
    if (_speech.isListening) {
      _speech.stop();
    }
    _v2vClient?.disconnect();
    _v2vClient = null;
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

  Future<void> _onOrbTap() async {
    debugPrint('[VoiceChat] Orb tapped, state: $_orbState, voiceMode: $_voiceModeActive, v2v: $_isV2VMode');
    HapticFeedback.mediumImpact();

    if (!_voiceModeActive) {
      // Resume voice mode — check if V2V provider is selected
      _voiceModeActive = true;
      final provider = SharedPreferencesUtil().ttsProvider;
      if (_isV2VProvider(provider)) {
        await _startV2V(provider);
      } else {
        _isV2VMode = false;
        await _startListening();
      }
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

  void _pauseVoiceMode() {
    debugPrint('[VoiceChat] Pausing voice mode');
    _voiceModeActive = false;
    _isV2VMode = false;
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

  Future<void> _startListening() async {
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
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Microphone permission is required for voice chat')),
        );
      }
      return;
    }

    final speechStatus = await Permission.speech.request();
    if (!speechStatus.isGranted) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Speech recognition permission is required')),
      );
      return;
    }

    if (!_speechAvailable) {
      await _initSpeech();
      if (!_speechAvailable) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Speech recognition is not available on this device')),
        );
        return;
      }
    }

    // Stop any existing mic recording from CaptureProvider
    if (mounted) {
      final captureProvider = Provider.of<CaptureProvider>(context, listen: false);
      if (captureProvider.recordingState == RecordingState.record) {
        debugPrint('[VoiceChat] Pausing capture recording to free mic');
        await captureProvider.stopStreamRecording();
        _didPauseCaptureRecording = true;
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

  Future<void> _startV2V(String provider) async {
    debugPrint('[VoiceChat] Starting V2V mode with provider: $provider');
    _isV2VMode = true;

    // Stop any existing mic recording from CaptureProvider
    if (mounted) {
      final captureProvider = Provider.of<CaptureProvider>(context, listen: false);
      if (captureProvider.recordingState == RecordingState.record) {
        debugPrint('[VoiceChat] Pausing capture recording for V2V');
        await captureProvider.stopStreamRecording();
        _didPauseCaptureRecording = true;
      }
    }

    setState(() {
      _orbState = VoiceOrbState.processing;
      _statusText = 'Connecting...';
      _audioLevel = 0.0;
    });

    // Stop any playing audio
    try {
      await _audioPlayer.stop();
    } catch (_) {}

    _v2vClient = V2VClient(
      onEvent: _onV2VEvent,
      onConnectionChanged: (connected) {
        if (!mounted) return;
        if (!connected && _isV2VMode) {
          debugPrint('[VoiceChat] V2V disconnected unexpectedly');
          setState(() {
            _orbState = VoiceOrbState.idle;
            _statusText = 'Disconnected — Tap to Reconnect';
            _isV2VMode = false;
            _voiceModeActive = false;
          });
        }
      },
    );

    final success = await _v2vClient!.connect(provider: provider);
    if (!mounted) return;

    if (!success) {
      debugPrint('[VoiceChat] V2V connect failed, falling back to STT mode');
      _isV2VMode = false;
      _v2vClient = null;
      // Fall back to standard STT→LLM→TTS
      _startListening();
      return;
    }

    setState(() {
      _orbState = VoiceOrbState.listening;
      _statusText = 'V2V Active — Tap to Stop';
    });
  }

  Future<void> _stopV2V() async {
    debugPrint('[VoiceChat] Stopping V2V mode');
    await _v2vClient?.disconnect();
    _v2vClient = null;
    _pauseVoiceMode();
  }

  void _onV2VEvent(V2VEvent event) {
    if (!mounted) return;

    switch (event.type) {
      case 'user_transcript':
        setState(() {
          _lastUserText = event.text ?? '';
          _ellaDisplayText = '';
        });
        _scrollToBottom();
        break;
      case 'transcript':
        final text = event.text ?? '';
        _lastEllaText = text;
        setState(() {
          _ellaDisplayText = text;
          _orbState = VoiceOrbState.speaking;
          _statusText = 'Ella is speaking...';
        });
        _scrollToBottom();
        // Inject into chat history
        if (_lastUserText.isNotEmpty && text.isNotEmpty) {
          _injectVoiceMessages(_lastUserText, text);
        }
        break;
      case 'audio_done':
        if (_isV2VMode) {
          setState(() {
            _orbState = VoiceOrbState.listening;
            _statusText = 'V2V Active — Tap to Stop';
          });
        }
        break;
      case 'speech_started':
        // User started talking — interrupt playback
        setState(() {
          _orbState = VoiceOrbState.listening;
          _statusText = 'Listening...';
          _ellaDisplayText = '';
        });
        break;
      case 'error':
        debugPrint('[VoiceChat] V2V error: ${event.text}');
        break;
      case 'session_end':
        debugPrint('[VoiceChat] V2V session ended: ${event.text}');
        if (_isV2VMode) {
          _stopV2V();
        }
        break;
    }
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
    setState(() {
      _orbState = VoiceOrbState.processing;
      _statusText = 'Ella is thinking...';
      _audioLevel = 0.0;
      _lastUserText = transcript;
    });

    try {
      // Send via Ella's chat endpoint
      debugPrint('[VoiceChat] Sending to Ella chat...');
      final replyBuffer = StringBuffer();
      await for (var chunk in sendEllaChatStream(transcript)) {
        if (chunk.type == MessageChunkType.data) {
          replyBuffer.write(chunk.text);
        } else if (chunk.type == MessageChunkType.done && chunk.message != null) {
          if (chunk.message!.text.isNotEmpty) {
            replyBuffer.clear();
            replyBuffer.write(chunk.message!.text);
          }
        }
      }

      final fullReply = replyBuffer.toString().trim();
      debugPrint(
          '[VoiceChat] Ella reply (${fullReply.length} chars): "${fullReply.substring(0, math.min(100, fullReply.length))}"');
      if (!mounted) return;
      if (fullReply.isEmpty) {
        if (_voiceModeActive) {
          _startListening();
        } else {
          _returnToIdle();
        }
        return;
      }

      // Inject both messages into chat history so Chat tab shows them
      _injectVoiceMessages(transcript, fullReply);

      // Store full reply and start typewriter reveal
      _lastEllaText = fullReply;
      _startTypewriter(fullReply);

      setState(() {
        _statusText = 'Ella is speaking...';
        _orbState = VoiceOrbState.speaking;
      });

      // Strip emojis before TTS — prevents the TTS engine from reading emoji names aloud
      final ttsText = _stripEmojis(fullReply.length > 500 ? fullReply.substring(0, 500) : fullReply);
      debugPrint('[VoiceChat] Synthesizing TTS (${ttsText.length} chars)...');
      final audioPath = await ElevenLabsTts.synthesize(ttsText);
      debugPrint('[VoiceChat] TTS result: $audioPath');

      if (!mounted) return;
      if (audioPath == null) {
        // Backend unavailable — fall back to on-device TTS
        debugPrint('[VoiceChat] Backend TTS unavailable, using on-device TTS');
        await ElevenLabsTts.speakOnDevice(ttsText);
        if (!mounted) return;
        if (_voiceModeActive) {
          _startListening();
        } else {
          _returnToIdle();
        }
        return;
      }

      // Play audio
      debugPrint('[VoiceChat] Playing audio: $audioPath');
      await _audioPlayer.setFilePath(audioPath);
      await _audioPlayer.play();
      // playerStateStream listener handles return to idle on completion
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

  /// Strip emojis from text so TTS doesn't read them aloud.
  String _stripEmojis(String text) {
    return text.replaceAll(_emojiRegex, '').replaceAll(RegExp(r'  +'), ' ').trim();
  }

  /// Inject voice exchange into MessageProvider so Chat tab shows it.
  void _injectVoiceMessages(String userText, String ellaReply) {
    try {
      final msgProvider = Provider.of<MessageProvider>(context, listen: false);
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
      msgProvider.addMessage(userMsg);

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
      msgProvider.addMessage(aiMsg);
      debugPrint('[VoiceChat] Injected voice messages into chat history');
    } catch (e) {
      debugPrint('[VoiceChat] Failed to inject messages: $e');
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

  String get _transcriptContent {
    if (_orbState == VoiceOrbState.listening && _currentWords.isNotEmpty) {
      return _currentWords;
    }
    if (_ellaDisplayText.isNotEmpty) return _ellaDisplayText;
    if (_lastUserText.isNotEmpty) return _lastUserText;
    return '';
  }

  Color get _transcriptColor {
    if (_orbState == VoiceOrbState.listening && _currentWords.isNotEmpty) {
      return EllaColors.textTertiary.withOpacity(0.7);
    }
    if (_ellaDisplayText.isNotEmpty) return EllaColors.textSecondary;
    return EllaColors.textTertiary.withOpacity(0.7);
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        backgroundColor: EllaColors.bgPrimary,
        title: const Text(
          'Voice Chat',
          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
        ),
        elevation: 0,
        centerTitle: true,
      ),
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Spacer(flex: 2),
            Center(
              child: EllaVoiceOrb(
                state: _orbState,
                audioLevel: _audioLevel,
                onTap: _onOrbTap,
              ),
            ),
            const SizedBox(height: 24),
            Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                decoration: BoxDecoration(
                  color: EllaColors.bgSecondary,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                  border: Border.all(color: EllaColors.primary.withOpacity(0.22), width: 1),
                ),
                child: Text(
                  _statusText,
                  style: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                    color: EllaColors.textSecondary,
                  ),
                  textAlign: TextAlign.center,
                ),
              ),
            ),
            const SizedBox(height: 16),
            SizedBox(
              height: 100,
              width: double.infinity,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: _transcriptContent.isEmpty
                    ? const SizedBox.shrink()
                    : SingleChildScrollView(
                        controller: _transcriptScrollController,
                        child: Text(
                          _transcriptContent,
                          style: TextStyle(fontSize: 16, color: _transcriptColor),
                          textAlign: TextAlign.center,
                        ),
                      ),
              ),
            ),
            const Spacer(flex: 3),
          ],
        ),
      ),
    );
  }
}
