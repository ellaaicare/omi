import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

class EllaBreathingDot extends StatefulWidget {
  const EllaBreathingDot({
    super.key,
    this.active = true,
    this.live = false,
    this.size = 10,
    this.activeColor = EllaColors.teal,
    this.inactiveColor = EllaColors.inkSoft,
  });

  final bool active;
  final bool live;
  final double size;
  final Color activeColor;
  final Color inactiveColor;

  @override
  State<EllaBreathingDot> createState() => _EllaBreathingDotState();
}

class _EllaBreathingDotState extends State<EllaBreathingDot> with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final Animation<double> _scale;
  late final Animation<double> _opacity;

  Duration get _duration => Duration(milliseconds: widget.live ? 1300 : 2600);

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(vsync: this, duration: _duration);
    final curved = CurvedAnimation(parent: _controller, curve: Curves.easeInOut);
    _scale = Tween<double>(begin: 1, end: 1.06).animate(curved);
    _opacity = Tween<double>(begin: 0.7, end: 1).animate(curved);
    if (widget.active) _controller.repeat(reverse: true);
  }

  @override
  void didUpdateWidget(EllaBreathingDot oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.live != widget.live) {
      _controller.duration = _duration;
      if (_controller.isAnimating) _controller.repeat(reverse: true);
    }
    if (oldWidget.active != widget.active) {
      widget.active ? _controller.repeat(reverse: true) : _controller.stop();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.disableAnimationsOf(context);
    final dot = Container(
      width: widget.size,
      height: widget.size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: widget.active ? widget.activeColor : widget.inactiveColor,
      ),
    );
    if (!widget.active || reduceMotion) return dot;
    return FadeTransition(
      opacity: _opacity,
      child: ScaleTransition(scale: _scale, child: dot),
    );
  }
}
