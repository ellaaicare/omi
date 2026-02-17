import 'dart:async';

import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/home_provider.dart';

enum EmergencyButtonState { idle, alerting, cooldown }

class EllaEmergencyButton extends StatefulWidget {
  const EllaEmergencyButton({super.key});

  @override
  State<EllaEmergencyButton> createState() => _EllaEmergencyButtonState();
}

class _EllaEmergencyButtonState extends State<EllaEmergencyButton> with SingleTickerProviderStateMixin {
  EmergencyButtonState _buttonState = EmergencyButtonState.idle;
  late AnimationController _pulseController;
  late Animation<double> _scaleAnimation;
  late Animation<double> _shadowOpacityAnimation;
  late Animation<double> _shadowBlurAnimation;
  Timer? _cooldownTimer;
  bool _isPressed = false;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      duration: const Duration(milliseconds: 2000),
      vsync: this,
    )..repeat(reverse: true);

    _scaleAnimation = Tween<double>(begin: 1.0, end: 1.005).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    _shadowOpacityAnimation = Tween<double>(begin: 0.15, end: 0.25).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    _shadowBlurAnimation = Tween<double>(begin: 8.0, end: 12.0).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _pulseController.dispose();
    _cooldownTimer?.cancel();
    super.dispose();
  }

  Future<void> _onEmergencyTap() async {
    if (_buttonState != EmergencyButtonState.idle) return;

    HapticFeedback.mediumImpact();

    // Navigate to voice chat tab (index 2)
    if (mounted) {
      context.read<HomeProvider>().setIndex(2);
    }
  }

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.of(context).disableAnimations;
    final isIdle = _buttonState == EmergencyButtonState.idle;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: EllaSizes.spacingM, vertical: EllaSizes.spacingS),
      child: Semantics(
        button: true,
        label: 'Call Ella. Tap to notify your emergency contacts.',
        hint: 'Double tap to activate',
        child: AnimatedBuilder(
          animation: _pulseController,
          builder: (context, child) {
            final scale = (isIdle && !reduceMotion) ? _scaleAnimation.value : (_isPressed ? 0.97 : 1.0);
            final shadowOpacity = (isIdle && !reduceMotion) ? _shadowOpacityAnimation.value : 0.0;
            final shadowBlur = (isIdle && !reduceMotion) ? _shadowBlurAnimation.value : 0.0;

            return Transform.scale(
              scale: scale,
              child: GestureDetector(
                onTapDown: (_) {
                  if (isIdle) setState(() => _isPressed = true);
                },
                onTapUp: (_) {
                  if (_isPressed) setState(() => _isPressed = false);
                  _onEmergencyTap();
                },
                onTapCancel: () {
                  if (_isPressed) setState(() => _isPressed = false);
                },
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  curve: Curves.easeOut,
                  height: EllaSizes.emergencyButtonHeight,
                  width: double.infinity,
                  padding: const EdgeInsets.symmetric(horizontal: EllaSizes.spacingL),
                  decoration: BoxDecoration(
                    color:
                        isIdle ? (_isPressed ? EllaColors.primaryDark : EllaColors.primary) : EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                    boxShadow: isIdle
                        ? [
                            BoxShadow(
                              color: EllaColors.primary.withOpacity(_isPressed ? 0.4 : shadowOpacity),
                              blurRadius: _isPressed ? 6 : shadowBlur,
                              offset: Offset(0, _isPressed ? 2 : 4),
                            ),
                          ]
                        : [],
                  ),
                  child: AbsorbPointer(
                    absorbing: !isIdle,
                    child: Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: isIdle
                          ? [
                              const Icon(Icons.phone_rounded, size: EllaSizes.iconLarge, color: Colors.white),
                              const SizedBox(width: 12),
                              const Text(
                                'Call Ella',
                                style: TextStyle(
                                  fontSize: 20,
                                  fontWeight: FontWeight.w600,
                                  color: Colors.white,
                                ),
                              ),
                            ]
                          : [
                              const SizedBox(
                                width: 20,
                                height: 20,
                                child: CupertinoActivityIndicator(color: EllaColors.textDisabled),
                              ),
                              const SizedBox(width: 12),
                              const Text(
                                'Contacting help...',
                                style: TextStyle(
                                  fontSize: 20,
                                  color: EllaColors.textDisabled,
                                ),
                              ),
                            ],
                    ),
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}
