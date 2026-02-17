import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';

/// Animated thinking indicator for Ella chat.
/// Shows a pulsing glow with "Ella is thinking..." text and elapsed time.
/// Designed for long response times (8-15 seconds) — never goes static.
class EllaThinkingIndicator extends StatefulWidget {
  const EllaThinkingIndicator({super.key});

  @override
  State<EllaThinkingIndicator> createState() => _EllaThinkingIndicatorState();
}

class _EllaThinkingIndicatorState extends State<EllaThinkingIndicator> with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _pulseAnimation;
  late Animation<double> _dotAnimation;
  Timer? _timer;
  int _elapsedSeconds = 0;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 1800),
      vsync: this,
    )..repeat();

    _pulseAnimation = Tween<double>(begin: 0.4, end: 1.0).animate(
      CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
    );

    _dotAnimation = Tween<double>(begin: 0.0, end: 1.0).animate(_controller);

    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) {
        setState(() {
          _elapsedSeconds++;
        });
      }
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    _timer?.cancel();
    super.dispose();
  }

  String _getThinkingText() {
    if (_elapsedSeconds < 3) return 'Ella is thinking';
    if (_elapsedSeconds < 8) return 'Ella is thinking';
    if (_elapsedSeconds < 15) return 'Still working on it';
    return 'Almost there';
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          // Pulsing teal dot
          AnimatedBuilder(
            animation: _pulseAnimation,
            builder: (context, child) {
              return Container(
                width: 10,
                height: 10,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: EllaColors.primary.withValues(alpha: _pulseAnimation.value),
                  boxShadow: [
                    BoxShadow(
                      color: EllaColors.primary.withValues(alpha: _pulseAnimation.value * 0.4),
                      blurRadius: 8 * _pulseAnimation.value,
                      spreadRadius: 2 * _pulseAnimation.value,
                    ),
                  ],
                ),
              );
            },
          ),
          const SizedBox(width: 10),
          // Thinking text with animated dots
          AnimatedBuilder(
            animation: _dotAnimation,
            builder: (context, child) {
              var dots = '.' * (1 + (_dotAnimation.value * 3).floor() % 3);
              return Text(
                '${_getThinkingText()}$dots',
                style: TextStyle(
                  color: EllaColors.textSecondary,
                  fontSize: 14,
                  fontStyle: FontStyle.italic,
                ),
              );
            },
          ),
          if (_elapsedSeconds >= 5) ...[
            const SizedBox(width: 8),
            Text(
              '${_elapsedSeconds}s',
              style: TextStyle(
                color: EllaColors.textDisabled,
                fontSize: 12,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
