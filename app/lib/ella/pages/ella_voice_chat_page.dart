import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:just_audio/just_audio.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/providers/capture_provider.dart';
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
  String _statusText = 'Tap to Start';
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

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
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

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        debugPrint('[VoiceChat] Speech error: ${error.errorMsg}');
        // "error_no_match" means silence detected — treat as end of speech
        if (error.errorMsg == 'error_no_match' && _orbState == VoiceOrbState.listening) {
          if (_currentWords.isNotEmpty) {
            _processTranscript(_currentWords);
          } else if (_voiceModeActive) {
            // Silence with no words — restart listening
            debugPrint('[VoiceChat] No match, restarting listening');
            _startListening();
          } else {
            _returnToIdle();
          }
        }
      },
      onStatus: (status) {
        debugPrint('[VoiceChat] Speech status: $status');
        // "notListening" means recognition stopped (silence timeout)
        if (status == 'notListening' && _orbState == VoiceOrbState.listening) {
          if (_currentWords.isNotEmpty) {
            _processTranscript(_currentWords);
          } else if (_voiceModeActive) {
            // Silence timeout with no words — restart listening
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
    _playerSub?.cancel();
    _audioPlayer.dispose();
    if (_speech.isListening) {
      _speech.stop();
    }
    super.dispose();
  }

  void _returnToIdle() {
    if (!mounted) return;
    if (_voiceModeActive) {
      // Continuous mode — restart listening instead of going idle
      debugPrint('[VoiceChat] Voice mode active, restarting listening');
      _startListening();
      return;
    }
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = 'Tap to Start';
      _audioLevel = 0.0;
    });
  }

  Future<void> _onOrbTap() async {
    debugPrint('[VoiceChat] Orb tapped, state: $_orbState, voiceMode: $_voiceModeActive');
    HapticFeedback.mediumImpact();

    if (!_voiceModeActive) {
      // Start voice mode
      _voiceModeActive = true;
      await _startListening();
      return;
    }

    // Voice mode is active — tap means stop
    switch (_orbState) {
      case VoiceOrbState.idle:
        // Shouldn't normally be idle when voice mode is active, but handle it
        _stopVoiceMode();
        break;
      case VoiceOrbState.listening:
        // Tap while listening: send what we have, then stop after response
        await _speech.stop();
        if (_currentWords.isNotEmpty) {
          _processTranscript(_currentWords);
        }
        _stopVoiceMode();
        break;
      case VoiceOrbState.speaking:
        // Interrupt playback and stop voice mode
        await _audioPlayer.stop();
        _stopVoiceMode();
        break;
      case VoiceOrbState.processing:
        // Let it finish, but stop after
        _stopVoiceMode();
        break;
    }
  }

  void _stopVoiceMode() {
    debugPrint('[VoiceChat] Stopping voice mode');
    _voiceModeActive = false;
    if (_speech.isListening) {
      _speech.stop();
    }
    if (!mounted) return;
    setState(() {
      _orbState = VoiceOrbState.idle;
      _statusText = 'Tap to Start';
      _audioLevel = 0.0;
    });
  }

  Future<void> _startListening() async {
    debugPrint('[VoiceChat] _startListening called');

    // Prevent concurrent restart attempts
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
    // Check permissions
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
    setState(() {
      _orbState = VoiceOrbState.listening;
      _statusText = 'Listening...';
    });

    debugPrint('[VoiceChat] Starting speech recognition');
    try {
      await _speech.listen(
        onResult: _onSpeechResult,
        onSoundLevelChange: (level) {
          if (!mounted || _orbState != VoiceOrbState.listening) return;
          // speech_to_text levels are in dB, normalize to 0-1
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
      // Retry once after a longer delay
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

  void _onSpeechResult(SpeechRecognitionResult result) {
    if (!mounted) return;
    debugPrint('[VoiceChat] Speech result: final=${result.finalResult}, text="${result.recognizedWords}"');

    _currentWords = result.recognizedWords;

    if (result.finalResult && _currentWords.isNotEmpty) {
      // Speech recognition finished (silence detected) — process
      _processTranscript(_currentWords);
    } else {
      // Partial result — update UI to show what's being heard
      setState(() {
        _lastUserText = _currentWords;
      });
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
      // Send to Ella chat (streaming) and collect full reply
      debugPrint('[VoiceChat] Sending to Ella chat...');
      final replyBuffer = StringBuffer();
      await for (var chunk in sendEllaMessageStream(transcript)) {
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

      setState(() {
        _lastEllaText = fullReply;
        _statusText = 'Ella is speaking...';
        _orbState = VoiceOrbState.speaking;
      });

      // TTS — cap at 500 chars for speech
      final ttsText = fullReply.length > 500 ? fullReply.substring(0, 500) : fullReply;
      debugPrint('[VoiceChat] Synthesizing TTS (${ttsText.length} chars)...');
      final audioPath = await ElevenLabsTts.synthesize(ttsText);
      debugPrint('[VoiceChat] TTS result: $audioPath');

      if (!mounted) return;
      if (audioPath == null) {
        if (_voiceModeActive) {
          _startListening();
        } else {
          _returnToIdle();
        }
        return;
      }

      // Play audio
      debugPrint('[VoiceChat] Playing audio...');
      await _audioPlayer.setFilePath(audioPath);
      await _audioPlayer.play();
      // playerStateStream listener handles return to idle on completion
    } catch (e, st) {
      debugPrint('[VoiceChat] Error in voice flow: $e\n$st');
      if (mounted) {
        if (_voiceModeActive) {
          // Error but voice mode still on — restart listening
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

  Widget _buildTranscriptText() {
    // While listening — show live transcription
    if (_orbState == VoiceOrbState.listening && _currentWords.isNotEmpty) {
      return Text(
        _currentWords,
        style: TextStyle(fontSize: 16, color: EllaColors.textTertiary.withOpacity(0.7)),
        textAlign: TextAlign.center,
      );
    }
    // While processing/speaking/idle — show last exchange
    if (_lastEllaText.isNotEmpty) {
      return Text(
        _lastEllaText,
        style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary),
        textAlign: TextAlign.center,
      );
    }
    if (_lastUserText.isNotEmpty) {
      return Text(
        _lastUserText,
        style: TextStyle(fontSize: 16, color: EllaColors.textTertiary.withOpacity(0.7)),
        textAlign: TextAlign.center,
      );
    }
    return const SizedBox.shrink();
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
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // Voice orb
              EllaVoiceOrb(
                state: _orbState,
                audioLevel: _audioLevel,
                onTap: _onOrbTap,
              ),
              const SizedBox(height: 24),
              // Status text in capsule
              Container(
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
              const SizedBox(height: 16),
              // Transcript area — shows live speech OR last exchange, same location
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 100),
                  child: SingleChildScrollView(
                    reverse: true,
                    physics: const NeverScrollableScrollPhysics(),
                    child: _buildTranscriptText(),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
