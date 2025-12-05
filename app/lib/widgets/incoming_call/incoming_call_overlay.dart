import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:omi/services/incoming_call/incoming_call_service.dart';

/// Full-screen overlay for incoming calls from Ella
class IncomingCallOverlay extends StatelessWidget {
  const IncomingCallOverlay({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<IncomingCallService>(
      builder: (context, service, child) {
        // Hide overlay when idle or inCall (V2 UI takes over)
        if (service.state == IncomingCallState.idle ||
            service.state == IncomingCallState.inCall) {
          return const SizedBox.shrink();
        }

        return Material(
          color: Colors.black.withOpacity(0.95),
          child: SafeArea(
            child: _IncomingCallContent(service: service),
          ),
        );
      },
    );
  }
}

class _IncomingCallContent extends StatefulWidget {
  final IncomingCallService service;

  const _IncomingCallContent({required this.service});

  @override
  State<_IncomingCallContent> createState() => _IncomingCallContentState();
}

class _IncomingCallContentState extends State<_IncomingCallContent>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    )..repeat(reverse: true);

    _pulseAnimation = Tween<double>(begin: 1.0, end: 1.15).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final service = widget.service;
    final call = service.currentCall;
    final state = service.state;

    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        const Spacer(flex: 2),

        // Ella avatar with pulse animation
        AnimatedBuilder(
          animation: _pulseAnimation,
          builder: (context, child) {
            return Transform.scale(
              scale: state == IncomingCallState.ringing ? _pulseAnimation.value : 1.0,
              child: Container(
                width: 120,
                height: 120,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: LinearGradient(
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                    colors: [
                      Colors.purple.shade400,
                      Colors.blue.shade400,
                    ],
                  ),
                  boxShadow: state == IncomingCallState.ringing
                      ? [
                          BoxShadow(
                            color: Colors.purple.withOpacity(0.5),
                            blurRadius: 30,
                            spreadRadius: 10,
                          ),
                        ]
                      : null,
                ),
                child: const Icon(
                  Icons.person,
                  size: 60,
                  color: Colors.white,
                ),
              ),
            );
          },
        ),

        const SizedBox(height: 32),

        // "Ella is calling"
        Text(
          state == IncomingCallState.answering
              ? 'Connecting...'
              : state == IncomingCallState.declined ||
                      state == IncomingCallState.timeout
                  ? 'Playing voicemail...'
                  : 'Ella is calling',
          style: const TextStyle(
            color: Colors.white,
            fontSize: 28,
            fontWeight: FontWeight.bold,
          ),
        ),

        const SizedBox(height: 8),

        // Reason
        if (call != null)
          Text(
            call.reasonDisplay,
            style: TextStyle(
              color: Colors.white.withOpacity(0.7),
              fontSize: 16,
            ),
          ),

        const SizedBox(height: 24),

        // Voice listening indicator
        if (state == IncomingCallState.ringing) ...[
          _VoiceListeningIndicator(),
          const SizedBox(height: 8),
          Text(
            'Say "Answer" or "Decline"',
            style: TextStyle(
              color: Colors.white.withOpacity(0.6),
              fontSize: 14,
            ),
          ),
        ],

        const SizedBox(height: 16),

        // Timeout countdown
        if (state == IncomingCallState.ringing && service.remainingSeconds > 0)
          Text(
            '${service.remainingSeconds}s',
            style: TextStyle(
              color: Colors.white.withOpacity(0.5),
              fontSize: 14,
            ),
          ),

        const Spacer(flex: 2),

        // Answer/Decline buttons
        if (state == IncomingCallState.ringing) ...[
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              // Decline button
              _CallActionButton(
                icon: Icons.call_end,
                label: 'Decline',
                color: Colors.red,
                onTap: () => service.declineCall(),
              ),

              // Answer button
              _CallActionButton(
                icon: Icons.call,
                label: 'Answer',
                color: Colors.green,
                onTap: () => service.answerCall(),
              ),
            ],
          ),
          const SizedBox(height: 48),
        ],

        // Cancel button for other states
        if (state != IncomingCallState.ringing &&
            state != IncomingCallState.idle)
          TextButton(
            onPressed: () => service.forceCancel(),
            child: Text(
              'Cancel',
              style: TextStyle(
                color: Colors.white.withOpacity(0.6),
                fontSize: 16,
              ),
            ),
          ),

        const SizedBox(height: 32),
      ],
    );
  }
}

/// Animated button for call actions
class _CallActionButton extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _CallActionButton({
    required this.icon,
    required this.label,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 70,
            height: 70,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: color,
              boxShadow: [
                BoxShadow(
                  color: color.withOpacity(0.4),
                  blurRadius: 20,
                  spreadRadius: 5,
                ),
              ],
            ),
            child: Icon(
              icon,
              color: Colors.white,
              size: 32,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            label,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 14,
            ),
          ),
        ],
      ),
    );
  }
}

/// Animated voice listening indicator
class _VoiceListeningIndicator extends StatefulWidget {
  @override
  State<_VoiceListeningIndicator> createState() =>
      _VoiceListeningIndicatorState();
}

class _VoiceListeningIndicatorState extends State<_VoiceListeningIndicator>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(5, (index) {
        return AnimatedBuilder(
          animation: _controller,
          builder: (context, child) {
            final delay = index * 0.1;
            final value = math.sin((_controller.value + delay) * math.pi * 2);
            final height = 10 + (value + 1) * 10;

            return Container(
              width: 4,
              height: height,
              margin: const EdgeInsets.symmetric(horizontal: 2),
              decoration: BoxDecoration(
                color: Colors.purple.withOpacity(0.8),
                borderRadius: BorderRadius.circular(2),
              ),
            );
          },
        );
      }),
    );
  }
}
