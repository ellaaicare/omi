import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

/// The state the voice orb can be in.
enum VoiceOrbState { idle, listening, processing, speaking }

/// Animated teal orb for Ella Voice Chat.
///
/// Adapted from OpenClaw iOS TalkOrbOverlay:
/// - Two concentric pulsing rings (staggered timing)
/// - Radial gradient core that responds to mic level
/// - Scale animation based on audio input
/// - Centered with status text below
class EllaVoiceOrb extends StatefulWidget {
  const EllaVoiceOrb({
    super.key,
    required this.state,
    required this.onTap,
    this.audioLevel = 0.0,
  });

  final VoiceOrbState state;
  final VoidCallback onTap;
  final double audioLevel;

  @override
  State<EllaVoiceOrb> createState() => _EllaVoiceOrbState();
}

class _EllaVoiceOrbState extends State<EllaVoiceOrb> with TickerProviderStateMixin {
  late AnimationController _pulseController;
  late AnimationController _ring1Controller;
  late AnimationController _ring2Controller;

  @override
  void initState() {
    super.initState();

    // Idle breathing pulse
    _pulseController = AnimationController(
      duration: const Duration(milliseconds: 2000),
      vsync: this,
    )..repeat(reverse: true);

    // Outer ring 1 — expands and fades
    _ring1Controller = AnimationController(
      duration: const Duration(milliseconds: 1300),
      vsync: this,
    )..repeat();

    // Outer ring 2 — slower, staggered
    _ring2Controller = AnimationController(
      duration: const Duration(milliseconds: 1900),
      vsync: this,
    )..repeat();
  }

  @override
  void dispose() {
    _pulseController.dispose();
    _ring1Controller.dispose();
    _ring2Controller.dispose();
    super.dispose();
  }

  IconData _iconForState() {
    switch (widget.state) {
      case VoiceOrbState.idle:
        return Icons.mic;
      case VoiceOrbState.listening:
        return Icons.graphic_eq;
      case VoiceOrbState.processing:
        return Icons.more_horiz;
      case VoiceOrbState.speaking:
        return Icons.volume_up;
    }
  }

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.of(context).disableAnimations;
    final mic = widget.audioLevel.clamp(0.0, 1.0);
    final isActive = widget.state == VoiceOrbState.listening || widget.state == VoiceOrbState.speaking;
    final orbColor = EllaColors.primary;

    return GestureDetector(
      onTap: widget.onTap,
      behavior: HitTestBehavior.opaque,
      child: SizedBox(
        width: 280,
        height: 280,
        child: AnimatedBuilder(
          animation: Listenable.merge([_pulseController, _ring1Controller, _ring2Controller]),
          builder: (context, child) {
            final pulseScale = reduceMotion ? 1.0 : (1.0 + 0.015 * _pulseController.value);

            // Orb scale: idle pulse or mic-responsive
            final orbScale = widget.state == VoiceOrbState.listening
                ? 1.0 + 0.12 * mic
                : widget.state == VoiceOrbState.idle
                    ? pulseScale
                    : 1.0;

            return Stack(
              alignment: Alignment.center,
              children: [
                // Ring 1 — expands outward and fades
                if (isActive && !reduceMotion)
                  Opacity(
                    opacity: (1.0 - _ring1Controller.value).clamp(0.0, 1.0),
                    child: Container(
                      width: 220 * (0.96 + 0.19 * _ring1Controller.value),
                      height: 220 * (0.96 + 0.19 * _ring1Controller.value),
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(color: orbColor.withOpacity(0.26), width: 2),
                      ),
                    ),
                  ),

                // Ring 2 — slower, wider expansion
                if (isActive && !reduceMotion)
                  Opacity(
                    opacity: (0.9 - 0.9 * _ring2Controller.value).clamp(0.0, 1.0),
                    child: Container(
                      width: 220 * (1.02 + 0.43 * _ring2Controller.value),
                      height: 220 * (1.02 + 0.43 * _ring2Controller.value),
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(color: orbColor.withOpacity(0.18), width: 2),
                      ),
                    ),
                  ),

                // Main orb — radial gradient, scales with mic
                Transform.scale(
                  scale: orbScale,
                  child: Container(
                    width: 190,
                    height: 190,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      gradient: RadialGradient(
                        colors: [
                          orbColor.withOpacity(0.75 + 0.20 * mic),
                          orbColor.withOpacity(0.40),
                          Colors.black.withOpacity(0.55),
                        ],
                        center: Alignment.center,
                        radius: 0.6,
                      ),
                      border: Border.all(color: orbColor.withOpacity(0.35), width: 1),
                      boxShadow: [
                        BoxShadow(
                          color: orbColor.withOpacity(0.32),
                          blurRadius: 26,
                        ),
                        BoxShadow(
                          color: Colors.black.withOpacity(0.50),
                          blurRadius: 22,
                          offset: const Offset(0, 10),
                        ),
                      ],
                    ),
                    child: Center(
                      child: Icon(
                        _iconForState(),
                        color: Colors.white.withOpacity(0.9),
                        size: 48,
                      ),
                    ),
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}
