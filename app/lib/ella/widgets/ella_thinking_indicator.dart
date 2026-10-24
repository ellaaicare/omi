import 'dart:async';

import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Animated thinking indicator for Ella chat.
/// Shows pending state and local elapsed time without inferring server progress.
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
    );

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

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (MediaQuery.disableAnimationsOf(context) || MediaQuery.accessibleNavigationOf(context)) {
      _controller.stop();
    } else if (!_controller.isAnimating) {
      _controller.repeat();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
      child: Wrap(
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: 10,
        runSpacing: 4,
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
          // Thinking text with animated dots
          AnimatedBuilder(
            animation: _dotAnimation,
            builder: (context, child) {
              var dots = '.' * (1 + (_dotAnimation.value * 3).floor() % 3);
              return Text(
                '${context.l10n.ellaChatWaitingResponse}$dots',
                style: TextStyle(
                  color: EllaPalette.of(context).textSecondary,
                  fontSize: 14,
                  fontStyle: FontStyle.italic,
                ),
              );
            },
          ),
          if (_elapsedSeconds >= 5) ...[
            Text(
              context.l10n.ellaChatElapsedSeconds(_elapsedSeconds),
              style: TextStyle(
                color: EllaPalette.of(context).textDisabled,
                fontSize: 12,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
