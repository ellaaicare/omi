import 'package:flutter/services.dart';
import 'dart:math' as math;
import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:omi/ella/widgets/ella_v7_companion_art.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/iris_capture_controls.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Conservative opaque first frame; iOS reports its actual read-only setting.
class EllaV7Accessibility extends ValueNotifier<bool> {
  EllaV7Accessibility() : super(true) {
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'changed') _accept(call.arguments);
    });
    refresh();
  }
  static final shared = EllaV7Accessibility();
  static const _channel = MethodChannel('com.ella.v7/accessibility');
  void _accept(dynamic data) {
    if (data is Map && data['reduceTransparency'] is bool && data['increaseContrast'] is bool) {
      value = data['reduceTransparency'] == true || data['increaseContrast'] == true;
    } else {
      value = true;
    }
  }

  Future<void> refresh() async {
    try {
      _accept(await _channel.invokeMethod<dynamic>('read'));
    } catch (_) {
      value = true;
    }
  }
}

/// Decorative glass never clips or scales its content to fit. Solid surfaces
/// preserve contrast when the user requests simpler or higher-contrast UI.
class EllaV7Glass extends StatelessWidget {
  const EllaV7Glass({super.key, required this.child, this.radius = 24, this.padding = EdgeInsets.zero});
  final Widget child;
  final double radius;
  final EdgeInsetsGeometry padding;
  @override
  Widget build(BuildContext context) => ValueListenableBuilder<bool>(
      valueListenable: EllaV7Accessibility.shared,
      builder: (context, reduceTransparency, _) {
        final p = EllaPalette.of(context), m = MediaQuery.of(context);
        final twilight = p.nightMix != null && p.nightMix! > 0 && p.nightMix! < 1;
        final solid = reduceTransparency || m.highContrast || m.accessibleNavigation || twilight;
        final surface = DecoratedBox(
            decoration: BoxDecoration(
                color: solid
                    ? p.elevatedCard
                    : p.elevatedCard.withValues(alpha: .66 - .04 * (p.nightMix ?? (p.dark ? 1 : 0))),
                borderRadius: BorderRadius.circular(radius),
                border:
                    Border.all(color: p.mix(Colors.white.withValues(alpha: .75), Colors.white.withValues(alpha: .12)))),
            child: Padding(padding: padding, child: child));
        return ClipRRect(
            borderRadius: BorderRadius.circular(radius),
            child: BackdropFilter(enabled: !solid, filter: ImageFilter.blur(sigmaX: 18, sigmaY: 18), child: surface));
      });
}

class EllaV7Lume extends StatefulWidget {
  const EllaV7Lume(
      {super.key,
      this.pose = 'idle',
      this.size = 86,
      this.colorIndex,
      this.styleIndex,
      this.inputActive = false,
      this.animate = true});
  final String pose;
  final double size;
  final int? colorIndex;
  final int? styleIndex;
  final bool inputActive;
  final bool animate;
  @override
  State<EllaV7Lume> createState() => _EllaV7LumeState();
}

class _EllaV7LumeState extends State<EllaV7Lume> with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  late final AnimationController _motion = AnimationController(vsync: this, duration: const Duration(seconds: 4));
  bool _foreground = true;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
  }

  void _sync() {
    final m = MediaQuery.of(context);
    if (!widget.animate ||
        widget.pose == 'needs' ||
        !_foreground ||
        m.disableAnimations ||
        m.accessibleNavigation ||
        !TickerMode.valuesOf(context).enabled) {
      _motion.stop();
    } else if (!_motion.isAnimating) {
      _motion.repeat();
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  @override
  void didUpdateWidget(EllaV7Lume oldWidget) {
    super.didUpdateWidget(oldWidget);
    _sync();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _sync();
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _motion.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ExcludeSemantics(
      child: IgnorePointer(
          child: ListenableBuilder(
              listenable: EllaV7CompanionPreferences.shared,
              builder: (context, _) => AnimatedBuilder(
                  animation: _motion,
                  builder: (context, _) => ColorFiltered(
                      colorFilter: ColorFilter.matrix(
                          ellaV7LumeColorMatrix(widget.colorIndex ?? EllaV7CompanionPreferences.shared.colorIndex)),
                      child: SizedBox.square(
                          dimension: widget.size,
                          child: CustomPaint(
                              painter: EllaV7CompanionPainter(
                                  phase: _motion.isAnimating ? _motion.value : 0,
                                  styleIndex: widget.styleIndex ?? EllaV7CompanionPreferences.shared.styleIndex,
                                  pose: widget.pose,
                                  inputActive: widget.inputActive,
                                  moving: _motion.isAnimating))))))));
}

/// Uses only the dock's current presentation capabilities. A preference that
/// enables Whispers is not evidence of current speech, so it never says Whispering.
String ellaV7CaptureCaption(BuildContext context, IrisCaptureControls controls) {
  if (controls.necklaceDisconnected) return context.l10n.irisNecklaceNotConnected;
  return switch (controls.listening) {
    IrisListeningState.paused => '${context.l10n.irisCapturePaused} · ${context.l10n.irisNotListening}',
    IrisListeningState.active => switch (controls.readCaptureSource?.call()) {
        IrisCaptureSource.phone => '${context.l10n.irisListening} · ${context.l10n.captureStatusPhone}',
        IrisCaptureSource.necklace => '${context.l10n.irisListening} · ${context.l10n.captureStatusNecklace}',
        null => context.l10n.irisListening,
      },
    IrisListeningState.unavailable => context.l10n.irisNotListening,
  };
}

class EllaV7ChatIdentity extends StatelessWidget {
  const EllaV7ChatIdentity({super.key, this.compact = false, this.inputActive = false});
  final bool compact;
  final bool inputActive;
  static double extent(BuildContext context) => 48;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
      listenable: Listenable.merge([IrisCaptureControls.shared, EllaV7CompanionPreferences.shared]),
      builder: (context, _) {
        final c = IrisCaptureControls.shared;
        final name = EllaV7CompanionPreferences.shared.name;
        final pose = c.necklaceDisconnected
            ? 'needs'
            : switch (c.listening) {
                IrisListeningState.active => 'listen',
                IrisListeningState.paused => 'paused',
                _ => 'idle',
              };
        return SizedBox(
            height: 48,
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Semantics(
                  button: true,
                  enabled: true,
                  excludeSemantics: true,
                  onTap: c.openStatus,
                  label:
                      '${name.isEmpty ? context.l10n.ellaV7CompanionDefaultName : name}. ${context.l10n.irisStatusDetails}: ${ellaV7CaptureCaption(context, c)}',
                  child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                          key: const Key('v7-chat-companion-status'),
                          onTap: c.openStatus,
                          borderRadius: BorderRadius.circular(24),
                          child: EllaV7Lume(size: 48, pose: pose, inputActive: inputActive)))),
            ]));
      });
}

class EllaV7Atmosphere extends StatefulWidget {
  const EllaV7Atmosphere({super.key, required this.child});
  final Widget child;
  @override
  State<EllaV7Atmosphere> createState() => _EllaV7AtmosphereState();
}

class _EllaV7AtmosphereState extends State<EllaV7Atmosphere>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  bool _foreground = true;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _sync();
  }

  late final AnimationController _clock = AnimationController(vsync: this, duration: const Duration(seconds: 60));
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  void _sync() {
    final m = MediaQuery.of(context);
    if (!_foreground ||
        m.disableAnimations ||
        m.accessibleNavigation ||
        m.highContrast ||
        !TickerMode.valuesOf(context).enabled) {
      _clock.stop();
    } else if (!_clock.isAnimating) {
      _clock.repeat();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _clock.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RepaintBoundary(
      child: CustomPaint(
          painter: _AtmospherePainter(
              _clock,
              EllaPalette.of(context).nightMix ?? (EllaPalette.of(context).dark ? 1 : 0),
              MediaQuery.highContrastOf(context)),
          child: widget.child));
}

class _AtmospherePainter extends CustomPainter {
  _AtmospherePainter(this.clock, this.night, this.solid) : super(repaint: clock);
  final Animation<double> clock;
  final double night;
  final bool solid;
  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawColor(Color.lerp(const Color(0xFFF3EEF6), const Color(0xFF0B0A1C), night)!, BlendMode.src);
    // Opposing ink switches during twilight: keep its background predictable
    // and opaque while the palette blends; glass/wash return at steady endpoints.
    if (solid || (night > 0 && night < 1)) return;
    const day = [Color(0xFFFFAA8C), Color(0xFFA096FF), Color(0xFFC4F096), Color(0xFFF0B4FF)];
    const nocturne = [Color(0xFF6046BE), Color(0xFF962D5F), Color(0xFF146E78), Color(0xFF6046BE)];
    final colors = List.generate(4, (i) => Color.lerp(day[i], nocturne[i], night)!);
    for (var i = 0; i < colors.length; i++) {
      final phase = (clock.value + i * .23) * math.pi * 2;
      final c = Offset(size.width * (.2 + .6 * (i % 2)) + math.sin(phase) * size.width * .045,
          size.height * (.15 + i * .2) + math.cos(phase) * size.height * .035);
      final radius = size.width * .85;
      canvas.drawCircle(
          c,
          radius,
          Paint()
            ..shader = RadialGradient(
                    colors: [colors[i].withValues(alpha: 0.22 - .06 * night), colors[i].withValues(alpha: 0)])
                .createShader(Rect.fromCircle(center: c, radius: radius)));
    }
  }

  @override
  bool shouldRepaint(_AtmospherePainter old) => old.night != night || old.solid != solid;
}
