import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/guardian_mode_service.dart';
import 'package:omi/utils/l10n_extensions.dart';

class GuardianModeButton extends StatefulWidget {
  const GuardianModeButton({super.key});

  @override
  State<GuardianModeButton> createState() => _GuardianModeButtonState();
}

class _GuardianModeButtonState extends State<GuardianModeButton> with SingleTickerProviderStateMixin {
  final _guardianService = GuardianModeService();
  late AnimationController _pulseController;
  StreamSubscription<GuardianModeState>? _stateSubscription;
  GuardianModeState? _lastKnownState;

  @override
  void initState() {
    super.initState();

    // Don't start animation here - let state control it
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    );

    // Store subscription for disposal
    _stateSubscription = _guardianService.stateStream.listen((state) {
      if (mounted && state != _lastKnownState) {
        _lastKnownState = state;
        setState(() {});
        if (state == GuardianModeState.active) {
          if (!_pulseController.isAnimating) {
            _pulseController.repeat(reverse: true);
          }
        } else {
          _pulseController.stop();
          _pulseController.reset();
        }
      }
    });

    // Initialize animation based on current state
    if (_guardianService.currentState == GuardianModeState.active) {
      _pulseController.repeat(reverse: true);
    }
  }

  @override
  void dispose() {
    _stateSubscription?.cancel();
    _pulseController.dispose();
    super.dispose();
  }

  Future<void> _onTap() async {
    HapticFeedback.mediumImpact();

    try {
      final currentState = _guardianService.currentState;
      if (currentState == GuardianModeState.idle || currentState == GuardianModeState.error) {
        await _guardianService.start();
      } else {
        await _guardianService.stop();
      }
    } catch (e) {
      print('GuardianModeButton: Operation failed: $e');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Guardian Mode error: ${e.toString()}'),
            backgroundColor: Colors.red,
          ),
        );
      }
    }
  }

  Color _getButtonColor() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return Colors.grey.shade700;
      case GuardianModeState.active:
        return EllaColors.primary;
      case GuardianModeState.error:
        return Colors.red.shade600;
    }
  }

  IconData _getIcon() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return Icons.shield_outlined;
      case GuardianModeState.active:
        return Icons.shield;
      case GuardianModeState.error:
        return Icons.warning;
    }
  }

  String _getStatusText() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return 'Guardian OFF';
      case GuardianModeState.active:
        return 'Guardian Mode ON';
      case GuardianModeState.error:
        return 'Guardian Mode Error';
    }
  }

  @override
  Widget build(BuildContext context) {
    final isActive = _guardianService.currentState == GuardianModeState.active;

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        // Button
        Tooltip(
          message: isActive ? context.l10n.ellaGuardianOnTooltip : context.l10n.ellaGuardianOffTooltip,
          child: GestureDetector(
            onTap: _onTap,
            child: AnimatedBuilder(
              animation: _pulseController,
              builder: (context, child) {
                final pulseValue = isActive ? _pulseController.value : 0.0;
                final glowRadius = 20.0 + (pulseValue * 10.0);

                return Container(
                  width: 80,
                  height: 80,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: _getButtonColor(),
                    boxShadow: isActive
                        ? [
                            BoxShadow(
                              color: EllaColors.primary.withValues(alpha: 0.6),
                              blurRadius: glowRadius,
                              spreadRadius: glowRadius / 4,
                            ),
                          ]
                        : null,
                  ),
                  child: Icon(
                    _getIcon(),
                    color: Colors.white,
                    size: 40,
                  ),
                );
              },
            ),
          ),
        ),
        const SizedBox(height: 12),
        // Status text
        Text(
          _getStatusText(),
          style: const TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.w600,
            color: EllaColors.textSecondary,
          ),
        ),
      ],
    );
  }
}
