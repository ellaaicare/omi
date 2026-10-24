import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum EllaV7Appearance { dayNight, light, dark }

class EllaV7AppearanceController extends ValueNotifier<EllaV7Appearance> {
  EllaV7AppearanceController()
      : super(EllaV7Appearance.values.firstWhere((v) => v.name == SharedPreferencesUtil().getString('ellaV7Appearance'),
            orElse: () => EllaV7Appearance.dayNight));
  static final shared = EllaV7AppearanceController();
  Future<void> select(EllaV7Appearance next) async {
    final written = await SharedPreferencesUtil().saveString('ellaV7Appearance', next.name);
    if (written) value = next;
  }
}

/// Fixed local-time dawn/dusk avoids location access. A30-minute transition
/// around07:00/19:00 is explicitly a product interpretation, not astronomical sunrise.
double ellaV7NightAmount(DateTime now, EllaV7Appearance mode) {
  if (mode == EllaV7Appearance.light) return 0;
  if (mode == EllaV7Appearance.dark) return 1;
  final minutes = now.hour * 60 + now.minute + now.second / 60;
  if (minutes >= 405 && minutes <= 435) return 1 - (minutes - 405) / 30;
  if (minutes >= 1125 && minutes <= 1155) return (minutes - 1125) / 30;
  return minutes < 405 || minutes > 1155 ? 1 : 0;
}

class EllaV7Tone extends ThemeExtension<EllaV7Tone> {
  const EllaV7Tone(this.night);
  final double night;
  @override
  EllaV7Tone copyWith({double? night}) => EllaV7Tone(night ?? this.night);
  @override
  EllaV7Tone lerp(covariant EllaV7Tone? other, double t) => EllaV7Tone(night + ((other?.night ?? night) - night) * t);
}

class EllaV7ThemeScope extends StatefulWidget {
  const EllaV7ThemeScope({super.key, required this.child});
  final Widget child;
  @override
  State<EllaV7ThemeScope> createState() => _EllaV7ThemeScopeState();
}

class _EllaV7ThemeScopeState extends State<EllaV7ThemeScope> with WidgetsBindingObserver {
  Timer? _clock;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _startClock();
  }

  void _startClock() {
    _clock?.cancel();
    _clock = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _startClock();
      EllaV7Accessibility.shared.refresh();
      setState(() {});
    } else {
      _clock?.cancel();
    }
  }

  @override
  void dispose() {
    _clock?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ValueListenableBuilder<EllaV7Appearance>(
      valueListenable: EllaV7AppearanceController.shared,
      builder: (context, mode, _) {
        final night = ellaV7NightAmount(DateTime.now(), mode);
        final theme =
            ThemeData.lerp(ellaThemeData(), ellaDarkThemeData(), night).copyWith(extensions: [EllaV7Tone(night)]);
        final palette = EllaPalette(night >= .5, nightMix: night);
        return Theme(
            data: theme.copyWith(
              textTheme: theme.textTheme.apply(bodyColor: palette.ink, displayColor: palette.ink),
              iconTheme: theme.iconTheme.copyWith(color: palette.ink),
            ),
            child: widget.child);
      });
}

class EllaV7AppearancePicker extends StatelessWidget {
  const EllaV7AppearancePicker({super.key});
  @override
  Widget build(BuildContext context) => ValueListenableBuilder<EllaV7Appearance>(
      valueListenable: EllaV7AppearanceController.shared,
      builder: (context, selected, _) {
        final palette = EllaPalette.of(context);
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(context.l10n.ellaV7Appearance,
              style: EllaTypography.of(context).body.copyWith(fontWeight: FontWeight.w700)),
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final mode in EllaV7Appearance.values)
              ChoiceChip(
                  selected: mode == selected,
                  onSelected: (_) => EllaV7AppearanceController.shared.select(mode),
                  materialTapTargetSize: MaterialTapTargetSize.padded,
                  showCheckmark: false,
                  backgroundColor: palette.card,
                  selectedColor: palette.ink,
                  side: BorderSide(color: palette.cardEdge),
                  label: Text(
                      switch (mode) {
                        EllaV7Appearance.dayNight => context.l10n.ellaV7DayNight,
                        EllaV7Appearance.light => context.l10n.ellaV7AlwaysLight,
                        EllaV7Appearance.dark => context.l10n.ellaV7AlwaysDark
                      },
                      style: TextStyle(fontSize: 16, color: mode == selected ? palette.paper : palette.ink)))
          ]),
          Text(context.l10n.ellaV7DayNightExplanation, style: EllaTypography.of(context).secondary),
        ]);
      });
}
