import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/voice_mode/voice_mode_manager.dart';
import 'package:omi/services/voice_mode_v2/voice_mode_v2_service.dart';
import 'package:omi/utils/enums.dart';

/// Voice Mode Button Widget
///
/// A circular button to start/stop voice conversations with Ella AI.
/// Shows different states: inactive, listening, thinking, speaking.
///
/// When pressed:
/// - Starts phone mic recording via CaptureProvider
/// - Routes transcripts to VoiceModeManager for voice conversation
/// - Connects to WebSocket for backend voice events
class VoiceModeButton extends StatelessWidget {
  final double size;
  final bool showLabel;
  final bool disabled;

  const VoiceModeButton({
    super.key,
    this.size = 64,
    this.showLabel = false,
    this.disabled = false,
  });

  @override
  Widget build(BuildContext context) {
    // Check if V2 mode enabled in developer settings
    final useV2 = SharedPreferencesUtil().voiceModeV2Enabled;
    if (useV2) {
      return _buildV2Button(context);
    }

    return ListenableBuilder(
      listenable: VoiceModeManager(),
      builder: (context, _) {
        final voiceMode = VoiceModeManager();
        final state = voiceMode.state;
        final isActive = voiceMode.isActive;

        // Use disabled state for visual styling
        final isDisabled = disabled && !isActive; // Don't grey out if already active

        return Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            GestureDetector(
              onTap: () async {
                if (isDisabled) {
                  debugPrint('🎙️ [VoiceModeButton] Button disabled - ambient recording active');
                  return;
                }
                HapticFeedback.mediumImpact();
                await _handleVoiceModeToggle(context, voiceMode);
              },
              child: Opacity(
                opacity: isDisabled ? 0.5 : 1.0,
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  width: size,
                  height: size,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: isDisabled ? Colors.grey.shade600 : _getBackgroundColor(state),
                    border: Border.all(
                      color: Colors.black,
                      width: 4,
                    ),
                    boxShadow: isActive && !isDisabled
                        ? [
                            BoxShadow(
                              color: _getGlowColor(state).withValues(alpha: 0.5),
                              spreadRadius: 2,
                              blurRadius: 8,
                            ),
                          ]
                        : null,
                  ),
                  child: _buildIcon(state, isDisabled: isDisabled),
                ),
              ),
            ),
            if (showLabel) ...[
              const SizedBox(height: 4),
              Text(
                isDisabled ? 'Recording...' : _getLabel(state),
                style: TextStyle(
                  color: Colors.white.withValues(alpha: isDisabled ? 0.5 : 0.8),
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ],
        );
      },
    );
  }

  Widget _buildIcon(VoiceModeState state, {bool isDisabled = false}) {
    final iconColor = isDisabled ? Colors.white54 : Colors.white;

    switch (state) {
      case VoiceModeState.inactive:
        return Icon(
          FontAwesomeIcons.phone,
          color: iconColor,
          size: size * 0.35,
        );
      case VoiceModeState.listening:
        return _PulsingIcon(
          icon: FontAwesomeIcons.microphone,
          color: iconColor,
          size: size * 0.35,
        );
      case VoiceModeState.transcribing:
        return SizedBox(
          width: size * 0.4,
          height: size * 0.4,
          child: CircularProgressIndicator(
            color: iconColor,
            strokeWidth: 2,
          ),
        );
      case VoiceModeState.thinking:
        return _ThinkingDots(size: size * 0.35);
      case VoiceModeState.speaking:
        return _SpeakingWave(size: size * 0.35);
    }
  }

  Color _getBackgroundColor(VoiceModeState state) {
    switch (state) {
      case VoiceModeState.inactive:
        return Colors.blue.shade700;
      case VoiceModeState.listening:
        return Colors.green.shade600;
      case VoiceModeState.transcribing:
        return Colors.orange.shade600;
      case VoiceModeState.thinking:
        return Colors.purple.shade600;
      case VoiceModeState.speaking:
        return Colors.teal.shade600;
    }
  }

  Color _getGlowColor(VoiceModeState state) {
    switch (state) {
      case VoiceModeState.inactive:
        return Colors.blue;
      case VoiceModeState.listening:
        return Colors.green;
      case VoiceModeState.transcribing:
        return Colors.orange;
      case VoiceModeState.thinking:
        return Colors.purple;
      case VoiceModeState.speaking:
        return Colors.teal;
    }
  }

  String _getLabel(VoiceModeState state) {
    switch (state) {
      case VoiceModeState.inactive:
        return 'Talk to Ella';
      case VoiceModeState.listening:
        return 'Listening...';
      case VoiceModeState.transcribing:
        return 'Processing...';
      case VoiceModeState.thinking:
        return 'Thinking...';
      case VoiceModeState.speaking:
        return 'Speaking...';
    }
  }

  /// Build V2 voice mode button (Pipecat)
  Widget _buildV2Button(BuildContext context) {
    return ListenableBuilder(
      listenable: VoiceModeV2Service(),
      builder: (context, _) {
        final v2Service = VoiceModeV2Service();
        final isActive = v2Service.isActive;

        // Use disabled state for visual styling
        final isDisabled = disabled && !isActive;

        return Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            GestureDetector(
              onTap: () async {
                if (isDisabled) {
                  debugPrint('🎙️ [VoiceModeButton] V2 Button disabled');
                  return;
                }
                // Haptic is handled inside toggle handler for better feedback
                await _handleVoiceModeV2Toggle(context, v2Service);
              },
              child: Opacity(
                opacity: isDisabled ? 0.5 : 1.0,
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  width: size,
                  height: size,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    // Red background when active, blue when inactive
                    color: isDisabled
                        ? Colors.grey.shade600
                        : (isActive ? Colors.red.shade700 : Colors.blue.shade700),
                    border: Border.all(
                      color: Colors.black,
                      width: 4,
                    ),
                    boxShadow: isActive && !isDisabled
                        ? [
                            BoxShadow(
                              color: Colors.red.withValues(alpha: 0.5),
                              spreadRadius: 2,
                              blurRadius: 8,
                            ),
                          ]
                        : null,
                  ),
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      // Always show phone icon, pulse when active
                      isActive
                          ? _PulsingIcon(
                              icon: FontAwesomeIcons.phone,
                              color: Colors.white,
                              size: size * 0.35,
                            )
                          : Icon(
                              FontAwesomeIcons.phone,
                              color: isDisabled ? Colors.white54 : Colors.white,
                              size: size * 0.35,
                            ),
                      // V2 indicator badge
                      Positioned(
                        top: 2,
                        right: 2,
                        child: Container(
                          width: 12,
                          height: 12,
                          decoration: BoxDecoration(
                            color: isActive ? Colors.green : Colors.blue,
                            shape: BoxShape.circle,
                            border: Border.all(color: Colors.black, width: 1),
                          ),
                          child: const Center(
                            child: Text('2', style: TextStyle(fontSize: 7, color: Colors.white, fontWeight: FontWeight.bold)),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            if (showLabel) ...[
              const SizedBox(height: 4),
              Text(
                isDisabled ? 'Recording...' : (isActive ? 'Tap to end' : 'Talk to Ella'),
                style: TextStyle(
                  color: Colors.white.withValues(alpha: isDisabled ? 0.5 : 0.8),
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ],
        );
      },
    );
  }

  /// Play system click sound
  Future<void> _playClickSound() async {
    try {
      // Use system sound via platform channel
      await SystemSound.play(SystemSoundType.click);
    } catch (e) {
      debugPrint('🎙️ [VoiceModeButton] Click sound error: $e');
    }
  }

  /// Handle V2 voice mode toggle with robust state management
  Future<void> _handleVoiceModeV2Toggle(BuildContext context, VoiceModeV2Service v2Service) async {
    // Strong haptic feedback on press
    await HapticFeedback.heavyImpact();
    _playClickSound();

    if (v2Service.isActive) {
      // Stop V2 voice mode
      debugPrint('🎙️ [VoiceModeButton] Stopping V2 voice mode');

      // Stop mic first (no more user audio)
      ServiceManager.instance().mic.stop();

      // Send 2 seconds of silence to trigger VAD (backend needs this to know user stopped)
      debugPrint('🎙️ [VoiceModeButton] Sending silence to trigger VAD...');
      final silence = Uint8List(3200); // 100ms of zeros at 16kHz PCM16
      for (int i = 0; i < 20; i++) {  // 2 seconds total, sent quickly
        v2Service.sendAudio(silence);
      }

      // Brief delay then stop - don't block
      await Future.delayed(const Duration(milliseconds: 300));
      await v2Service.stop();

      // Confirm with lighter haptic
      await HapticFeedback.lightImpact();
      debugPrint('🎙️ [VoiceModeButton] V2 voice mode stopped');
    } else {
      // Start V2 voice mode
      debugPrint('🎙️ [VoiceModeButton] Starting V2 voice mode');

      // Force reset if somehow stuck
      if (v2Service.state != VoiceModeV2State.inactive) {
        debugPrint('🎙️ [VoiceModeButton] Force resetting stuck state');
        v2Service.forceReset();
        await Future.delayed(const Duration(milliseconds: 100));
      }

      // 1. Connect WebSocket first
      final success = await v2Service.start();
      if (!success) {
        debugPrint('🎙️ [VoiceModeButton] V2 voice mode failed to start');
        await HapticFeedback.vibrate(); // Error feedback
        return;
      }

      // Success haptic
      await HapticFeedback.mediumImpact();

      // 2. Start mic and route audio to V2 WebSocket
      debugPrint('🎙️ [VoiceModeButton] Starting mic capture for V2');
      ServiceManager.instance().mic.start(
        onByteReceived: (bytes) {
          // Send raw PCM16 audio directly to V2 WebSocket
          v2Service.sendAudio(bytes);
        },
        onRecording: () {
          debugPrint('🎙️ [VoiceModeButton] V2 mic recording started');
        },
        onStop: () {
          debugPrint('🎙️ [VoiceModeButton] V2 mic recording stopped');
        },
        onInitializing: () {
          debugPrint('🎙️ [VoiceModeButton] V2 mic initializing');
        },
      );
    }
  }

  /// Handle voice mode toggle - starts/stops recording and voice mode
  Future<void> _handleVoiceModeToggle(BuildContext context, VoiceModeManager voiceMode) async {
    final captureProvider = Provider.of<CaptureProvider>(context, listen: false);
    final recordingState = captureProvider.recordingState;

    if (voiceMode.isActive) {
      // Stop voice mode
      debugPrint('🎙️ [VoiceModeButton] Stopping voice mode');
      await voiceMode.stop();

      // Stop recording if it was started for voice mode
      if (recordingState == RecordingState.record) {
        await captureProvider.stopStreamRecording();
      }
    } else {
      // Start voice mode
      debugPrint('🎙️ [VoiceModeButton] Starting voice mode');

      // If not already recording, start recording and wait for WebSocket to connect
      // This will connect WebSocket and set up the callback
      if (recordingState != RecordingState.record && recordingState != RecordingState.initialising) {
        debugPrint('🎙️ [VoiceModeButton] Starting mic recording for voice mode');
        await captureProvider.streamRecording();

        // Wait a moment for WebSocket callback to be set up
        await Future.delayed(const Duration(milliseconds: 500));
        debugPrint('🎙️ [VoiceModeButton] WebSocket callback should now be set');
      }

      // Start voice mode (this will send voice_mode_start to backend)
      await voiceMode.startFromButton();
    }
  }
}

/// Pulsing icon animation for listening state
class _PulsingIcon extends StatefulWidget {
  final IconData icon;
  final Color color;
  final double size;

  const _PulsingIcon({
    required this.icon,
    required this.color,
    required this.size,
  });

  @override
  State<_PulsingIcon> createState() => _PulsingIconState();
}

class _PulsingIconState extends State<_PulsingIcon> with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _animation;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 800),
      vsync: this,
    )..repeat(reverse: true);
    _animation = Tween<double>(begin: 0.8, end: 1.2).animate(
      CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _animation,
      builder: (context, child) {
        return Transform.scale(
          scale: _animation.value,
          child: Icon(
            widget.icon,
            color: widget.color,
            size: widget.size,
          ),
        );
      },
    );
  }
}

/// Thinking dots animation
class _ThinkingDots extends StatefulWidget {
  final double size;

  const _ThinkingDots({required this.size});

  @override
  State<_ThinkingDots> createState() => _ThinkingDotsState();
}

class _ThinkingDotsState extends State<_ThinkingDots> with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 1200),
      vsync: this,
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (index) {
            final delay = index * 0.2;
            final progress = (_controller.value + delay) % 1.0;
            final scale = 0.5 + (progress < 0.5 ? progress : 1 - progress) * 1.0;
            return Container(
              margin: const EdgeInsets.symmetric(horizontal: 2),
              width: widget.size * 0.25,
              height: widget.size * 0.25 * scale,
              decoration: const BoxDecoration(
                color: Colors.white,
                shape: BoxShape.circle,
              ),
            );
          }),
        );
      },
    );
  }
}

/// Speaking wave animation
class _SpeakingWave extends StatefulWidget {
  final double size;

  const _SpeakingWave({required this.size});

  @override
  State<_SpeakingWave> createState() => _SpeakingWaveState();
}

class _SpeakingWaveState extends State<_SpeakingWave> with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 600),
      vsync: this,
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.center,
          children: List.generate(5, (index) {
            final delay = index * 0.15;
            final progress = (_controller.value + delay) % 1.0;
            final height = 0.3 + (progress < 0.5 ? progress : 1 - progress) * 1.4;
            return Container(
              margin: const EdgeInsets.symmetric(horizontal: 1),
              width: widget.size * 0.12,
              height: widget.size * height,
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(2),
              ),
            );
          }),
        );
      },
    );
  }
}
