import 'dart:math' as math;
import 'package:flutter/material.dart';

/// Decorative vector character. The canvas outside the silhouette stays clear.
class EllaV7CompanionPainter extends CustomPainter {
  const EllaV7CompanionPainter(
      {required this.phase,
      required this.styleIndex,
      required this.pose,
      required this.inputActive,
      required this.moving});
  final double phase;
  final int styleIndex;
  final String pose;
  final bool inputActive, moving;
  @override
  void paint(Canvas canvas, Size size) {
    canvas.save();
    canvas.scale(size.width / 100, size.height / 100);
    final wave = moving ? math.sin(phase * math.pi * 2) : 0.0;
    canvas.translate(0, wave * 1.2);
    final shell = Path();
    switch (styleIndex) {
      case 1:
        shell.moveTo(50, 9);
        shell.cubicTo(83, 25, 91, 56, 72, 80);
        shell.cubicTo(60, 95, 31, 91, 20, 72);
        shell.cubicTo(6, 48, 24, 23, 50, 9);
      case 2:
        for (var i = 0; i < 10; i++) {
          final a = -math.pi / 2 + i * math.pi / 5;
          final r = i.isEven ? 39.0 : 29.0;
          final point = Offset(50 + math.cos(a) * r, 51 + math.sin(a) * r);
          if (i == 0) {
            shell.moveTo(point.dx, point.dy);
          } else {
            shell.lineTo(point.dx, point.dy);
          }
        }
      default:
        shell.moveTo(27, 34);
        shell.cubicTo(16, 13, 26, 1, 35, 14);
        shell.lineTo(41, 30);
        shell.quadraticBezierTo(50, 27, 59, 30);
        shell.lineTo(65, 14);
        shell.cubicTo(74, 1, 84, 13, 73, 34);
        shell.cubicTo(96, 51, 85, 88, 50, 88);
        shell.cubicTo(15, 88, 4, 51, 27, 34);
    }
    shell.close();
    canvas.drawPath(
        shell,
        Paint()
          ..shader = const RadialGradient(
              center: Alignment(-.4, -.6),
              radius: 1.3,
              colors: [Color(0xffeee9ff), Color(0xffad9aff), Color(0xff6855c6)]).createShader(shell.getBounds()));
    canvas.drawPath(
        shell,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.3
          ..color = const Color(0xff6956a9));
    canvas.drawOval(const Rect.fromLTWH(28, 33, 20, 9), Paint()..color = Colors.white.withValues(alpha: .42));
    final blink = moving && phase > .79 && phase < .83;
    final gaze = inputActive ? 2.0 : (moving ? wave * 1.8 : 0.0);
    final eyes = !inputActive && (pose == 'paused' || pose == 'needs') ? .65 : 1.0;
    for (final x in [38.0, 62.0]) {
      final center = Offset(x + gaze, inputActive ? 55 : 52);
      canvas.drawOval(Rect.fromCenter(center: center, width: 6.5, height: blink ? 1.5 : 9 * eyes),
          Paint()..color = const Color(0xff25213f));
      if (!blink) canvas.drawCircle(center + const Offset(1, -2), 1.3, Paint()..color = Colors.white);
    }
    final mouth = Path()
      ..moveTo(44, 65)
      ..quadraticBezierTo(50, inputActive ? 70 : 68, 56, 65);
    if (pose == 'whisper') {
      canvas.drawOval(Rect.fromCenter(center: const Offset(50, 66), width: 8, height: moving ? 5 + wave.abs() * 4 : 6),
          Paint()..color = const Color(0xff25213f));
    } else {
      canvas.drawPath(
          mouth,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeCap = StrokeCap.round
            ..strokeWidth = 2
            ..color = const Color(0xff25213f));
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(EllaV7CompanionPainter old) =>
      phase != old.phase ||
      styleIndex != old.styleIndex ||
      pose != old.pose ||
      inputActive != old.inputActive ||
      moving != old.moving;
}
