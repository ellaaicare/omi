import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'dart:async';
import 'dart:math' as math;
import 'package:flutter/material.dart';

/// Poses from Iris's verified avatar('lume', o) and motion-spec.md.
enum LumeState { listening, thinking, speaking, paused }

LumeState lumeStateForActivity(
        {required bool captureActive, required bool requestPending, required bool audioPlaying}) =>
    audioPlaying
        ? LumeState.speaking
        : requestPending
            ? LumeState.thinking
            : captureActive
                ? LumeState.listening
                : LumeState.paused;

class LumePose {
  const LumePose(
      {required this.bob,
      required this.ear,
      required this.earLeft,
      required this.eyes,
      required this.lookX,
      required this.lookY,
      required this.squash,
      required this.mouth,
      required this.tilt,
      required this.glow});
  final double bob, ear, earLeft, eyes, lookX, lookY, squash, mouth, tilt, glow;
  factory LumePose.at(double t, LumeState state, {required bool reducedMotion, required double baseGlow}) {
    double wave(double period, double amplitude, [double phase = 0]) =>
        amplitude * math.sin(2 * math.pi * (t / period + phase));
    final w = reducedMotion ? 0.0 : 1.0;
    if (state == LumeState.thinking) {
      return LumePose(
          bob: w * wave(4.2, 1.2),
          ear: w * wave(4.2, 3) - 3,
          earLeft: w * wave(4.2, 3, .2) - 3,
          eyes: .82,
          lookX: reducedMotion ? 1 : wave(4.2, 1.4),
          lookY: -2.4,
          squash: w * wave(4.2, .015),
          mouth: 0,
          tilt: reducedMotion ? 3 : wave(6, 4),
          glow: baseGlow + (reducedMotion ? .08 : wave(1.6, .16)));
    }
    final sp = state == LumeState.speaking, paused = state == LumeState.paused;
    final blink = !reducedMotion && [1.7, 6.2, 11.3, 16.1].any((b) => t % 18 > b && t % 18 < b + .16);
    return LumePose(
        bob: w * wave(3.2, 1.6),
        ear: w * (wave(2.6, 6) + (sp ? wave(.5, 5) : 0)) - (paused ? 8 : 0),
        earLeft: w * (wave(2.6, 6, .15) + (sp ? wave(.5, 5, .2) : 0)) - (paused ? 8 : 0),
        eyes: paused
            ? .6
            : blink
                ? .1
                : 1,
        lookX: 0,
        lookY: 0,
        squash: w * (wave(3.2, .025) + (sp ? wave(.45, .05).abs() : 0)),
        mouth: sp ? (reducedMotion ? .5 : math.sin(t * 9).abs() * .9) : 0,
        tilt: w * wave(5, 2.5),
        glow: (paused ? baseGlow * .6 : baseGlow) + (sp && !reducedMotion ? wave(.9, .12) : 0));
  }
}

/// Purely decorative. Adjacent title and capture capsule retain spoken status.
/// No demo state cycle, transport, playback, capture control, or asset fetching.
class EllaCompanionAvatar extends StatefulWidget {
  const EllaCompanionAvatar(
      {super.key, this.size = 66, this.state = LumeState.paused, this.stateReader, this.animate = true});
  final double size;
  final LumeState state;
  final LumeState Function()? stateReader;
  final bool animate;
  @override
  State<EllaCompanionAvatar> createState() => _EllaCompanionAvatarState();
}

class _EllaCompanionAvatarState extends State<EllaCompanionAvatar> with WidgetsBindingObserver {
  Timer? _stateWatch;
  LumeState _state = LumeState.paused;
  bool _foreground = true;
  bool _visible = true;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    _refreshState();
    _syncStateWatch();
  }

  void _syncStateWatch() {
    if (widget.stateReader == null || !_foreground || !_visible) {
      _stateWatch?.cancel();
      _stateWatch = null;
      return;
    }
    _stateWatch ??= Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (_foreground && _visible) _refreshState();
    });
  }

  void _refreshState() {
    LumeState state;
    try {
      state = widget.stateReader?.call() ?? widget.state;
    } catch (_) {
      state = LumeState.paused;
    }
    if (_state != state && mounted) setState(() => _state = state);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _visible = TickerMode.valuesOf(context).enabled;
    _syncStateWatch();
  }

  @override
  void didUpdateWidget(EllaCompanionAvatar oldWidget) {
    super.didUpdateWidget(oldWidget);
    _refreshState();
    _syncStateWatch();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    if (_foreground) _refreshState();
    _syncStateWatch();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stateWatch?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => EllaV7Lume(
      size: widget.size,
      animate: widget.animate,
      pose: switch (_state) {
        LumeState.listening => 'listen',
        LumeState.speaking => 'whisper',
        LumeState.paused => 'paused',
        _ => 'idle'
      });
}

class LumePainter extends CustomPainter {
  LumePainter(
      {required this.seconds,
      required this.state,
      required this.reducedMotion,
      required this.avatarSize,
      required this.baseGlow})
      : super(repaint: seconds);
  final ValueNotifier<double> seconds;
  final LumeState state;
  final bool reducedMotion;
  final double avatarSize, baseGlow;
  static final Path _left = Path()
    ..moveTo(21, 25)
    ..cubicTo(12, 22, 8, 14, 10, 7)
    ..cubicTo(18, 9, 23, 15, 24.5, 22)
    ..close();
  static final Path _right = Path()
    ..moveTo(43, 25)
    ..cubicTo(52, 22, 56, 14, 54, 7)
    ..cubicTo(46, 9, 41, 15, 39.5, 22)
    ..close();
  static final Path _body = Path()
    ..moveTo(10, 40)
    ..cubicTo(10, 27, 20, 21, 32, 21)
    ..cubicTo(44, 21, 54, 27, 54, 40)
    ..cubicTo(54, 52, 45, 58, 32, 58)
    ..cubicTo(19, 58, 10, 52, 10, 40)
    ..close();
  static final Path _shine = Path()
    ..moveTo(16, 34)
    ..cubicTo(18, 28, 23, 25, 29, 25);
  static const Color _ink = Color(0xff0b1020), _tint = Color(0xff8c9bff);
  @override
  void paint(Canvas c, Size size) {
    final p = LumePose.at(seconds.value, state, reducedMotion: reducedMotion, baseGlow: baseGlow);
    final sx = 1 + p.squash * .6, sy = 1 - p.squash;
    c.save();
    c.translate((size.width - avatarSize) / 2, (size.height - avatarSize) / 2 + p.bob);
    c.scale(avatarSize / 64);
    c.drawOval(Rect.fromCenter(center: const Offset(32, 60), width: 30 * sx, height: 6),
        Paint()..color = Colors.black.withValues(alpha: .14));
    c.translate(32, 58);
    c.rotate(p.tilt * math.pi / 180);
    c.scale(sx, sy);
    c.translate(-32, -58);
    c.drawOval(
        Rect.fromCenter(center: const Offset(32, 38), width: 44, height: 40.48),
        Paint()
          ..color = _tint.withValues(alpha: p.glow.clamp(0, 1))
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6.5));
    void shell(Path path) {
      c.drawPath(
          path,
          Paint()
            ..shader = const LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [Color(0xf5ffffff), Color(0x998c9bff), Color(0xccc08bff)],
                stops: [0, .45, 1]).createShader(path.getBounds()));
      c.drawPath(
          path,
          Paint()
            ..color = Colors.white.withValues(alpha: .88)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.2);
    }

    c.save();
    c.translate(22, 25);
    c.rotate(-p.earLeft * math.pi / 180);
    c.translate(-22, -25);
    shell(_left);
    c.restore();
    c.save();
    c.translate(42, 25);
    c.rotate(p.ear * math.pi / 180);
    c.translate(-42, -25);
    shell(_right);
    c.restore();
    shell(_body);
    c.drawPath(
        _shine,
        Paint()
          ..color = Colors.white.withValues(alpha: .9)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 3
          ..strokeCap = StrokeCap.round);
    c.drawOval(Rect.fromCenter(center: const Offset(46, 49), width: 6, height: 3.2),
        Paint()..color = Colors.white.withValues(alpha: .35));
    c.save();
    c.translate(p.lookX, p.lookY + 40 * (1 - p.eyes));
    c.scale(1, p.eyes);
    for (final x in [27.0, 37.0]) {
      c.drawOval(Rect.fromCenter(center: Offset(x, 40), width: 5.4, height: 6.2), Paint()..color = _ink);
      c.drawCircle(Offset(x + .9, 38.9), .9, Paint()..color = Colors.white);
    }
    c.restore();
    if (p.mouth > .05) {
      c.drawOval(
          Rect.fromCenter(
              center: Offset(32 + p.lookX * .6, 48), width: 2 * (2.25 + p.mouth * 1.2), height: 2 * (.6 + p.mouth * 2)),
          Paint()..color = _ink.withValues(alpha: .85));
    } else {
      final mouth = Path()
        ..moveTo(29.5, 47.5)
        ..quadraticBezierTo(32, 49.1, 34.5, 47.5);
      c.drawPath(
          mouth,
          Paint()
            ..color = _ink.withValues(alpha: .75)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.8
            ..strokeCap = StrokeCap.round);
    }
    c.restore();
  }

  @override
  bool shouldRepaint(LumePainter old) =>
      old.state != state ||
      old.reducedMotion != reducedMotion ||
      old.avatarSize != avatarSize ||
      old.baseGlow != baseGlow ||
      old.seconds != seconds;
}
