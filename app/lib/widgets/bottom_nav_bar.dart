import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Existing page indices preserve routing, drafts and capture lifecycles.
class IrisDestination {
  const IrisDestination(this.index, this.label, this.icon);
  final int index;
  final String label;
  final IconData icon;
}

List<IrisDestination> irisDestinations(BuildContext context) => [
      IrisDestination(1, context.l10n.bottomNavChat, Icons.chat_bubble_outline_rounded),
      IrisDestination(0, context.l10n.irisJournal, Icons.article_outlined),
      IrisDestination(3, context.l10n.irisMore, Icons.more_horiz_rounded),
    ];

class BottomNavBar extends StatelessWidget {
  const BottomNavBar({super.key, required this.onTabTap});
  final void Function(int index, bool isRepeat) onTabTap;

  static bool compact(BuildContext context) => MediaQuery.textScalerOf(context).scale(1) >= 2;

  static double navigationHeight(BuildContext context) {
    if (EllaRailScope.active(context) || EllaBottomNavigationPlacementScope.active(context)) return 0;
    // At accessibility text sizes, a narrow three-column label fragments into
    // individual letters. Keep the destinations in the semantics tree instead.
    if (compact(context)) return 64;
    final safe = MediaQuery.paddingOf(context);
    final width = math.min(350, MediaQuery.sizeOf(context).width - safe.left - safe.right - 24) / 3 - 24;
    double labelHeight = 0;
    for (final destination in irisDestinations(context)) {
      final painter = TextPainter(
          text: TextSpan(
              text: destination.label,
              style:
                  TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 16, height: 1.2, fontWeight: FontWeight.w700)),
          textDirection: Directionality.of(context),
          textScaler: MediaQuery.textScalerOf(context))
        ..layout(maxWidth: math.max(1, width));
      labelHeight = math.max(labelHeight, painter.height);
      painter.dispose();
    }
    return math.max(MediaQuery.textScalerOf(context).scale(1) >= 1.2 ? 76 : 72, labelHeight + 50);
  }

  @override
  Widget build(BuildContext context) {
    final selected = context.watch<HomeProvider>().selectedIndex;
    final palette = EllaPalette.of(context);
    final compactNavigation = compact(context);
    return Align(
        alignment: Alignment.bottomCenter,
        child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 350),
                child: EllaV7Glass(
                    radius: 33,
                    child: Material(
                      color: Colors.transparent,
                      child: SafeArea(
                          top: false,
                          child: SizedBox(
                            height: navigationHeight(context),
                            child: Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 12),
                                child: Row(children: [
                                  for (final destination in irisDestinations(context))
                                    Expanded(
                                        child: Semantics(
                                      label: destination.label,
                                      excludeSemantics: true,
                                      onTap: () {
                                        primaryFocus?.unfocus();
                                        onTabTap(destination.index, selected == destination.index);
                                      },
                                      selected:
                                          selected == destination.index || (selected == 2 && destination.index == 1),
                                      button: true,
                                      child: InkWell(
                                        key: Key('iris-navigation-${destination.index}'),
                                        onTap: () {
                                          primaryFocus?.unfocus();
                                          onTabTap(destination.index, selected == destination.index);
                                        },
                                        child: Padding(
                                            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
                                            child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                                              Container(
                                                  key: Key('iris-navigation-pill-${destination.index}'),
                                                  width: 56,
                                                  height: 30,
                                                  decoration: BoxDecoration(
                                                      color: selected == destination.index ||
                                                              (selected == 2 && destination.index == 1)
                                                          ? palette.ink
                                                          : Colors.transparent,
                                                      borderRadius: BorderRadius.circular(15)),
                                                  child: Icon(destination.icon,
                                                      size: 24,
                                                      color: selected == destination.index ||
                                                              (selected == 2 && destination.index == 1)
                                                          ? palette.paper
                                                          : palette.ink)),
                                              if (!compactNavigation) ...[
                                                const SizedBox(height: 4),
                                                Text(destination.label,
                                                    textAlign: TextAlign.center,
                                                    softWrap: true,
                                                    style: TextStyle(
                                                        fontFamily: EllaTextStyles.uiFont,
                                                        fontSize: 16,
                                                        height: 1.2,
                                                        fontWeight: selected == destination.index
                                                            ? FontWeight.w700
                                                            : FontWeight.w500,
                                                        color: palette.ink)),
                                              ],
                                            ])),
                                      ),
                                    )),
                                ])),
                          )),
                    )))));
  }
}

class EllaNavigationRail extends StatelessWidget {
  const EllaNavigationRail({super.key, required this.onTabTap});
  final void Function(int index, bool isRepeat) onTabTap;
  static bool fits(BuildContext context) =>
      MediaQuery.sizeOf(context).width >= 900 && MediaQuery.textScalerOf(context).scale(1) < 2;
  @override
  Widget build(BuildContext context) {
    final palette = EllaPalette.of(context);
    final selected = context.watch<HomeProvider>().selectedIndex;
    return SafeArea(
        right: false,
        child: SizedBox(
          key: const Key('iris-navigation-rail'),
          width: 130,
          child: ListView(children: [
            for (final destination in irisDestinations(context))
              ListTile(
                selected: selected == destination.index,
                textColor: palette.ink,
                selectedColor: palette.ink,
                leading: Icon(destination.icon),
                title: Text(destination.label),
                onTap: () {
                  primaryFocus?.unfocus();
                  onTabTap(destination.index, selected == destination.index);
                },
              )
          ]),
        ));
  }
}

class EllaRailScope extends InheritedWidget {
  const EllaRailScope({super.key, this.enabled = true, required super.child});
  final bool enabled;
  static bool active(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<EllaRailScope>()?.enabled ?? false;
  @override
  bool updateShouldNotify(EllaRailScope oldWidget) => enabled != oldWidget.enabled;
}

/// The shell owns one live dock; child memory pages must not mount another.
class EllaCaptureDockPlacementScope extends InheritedWidget {
  const EllaCaptureDockPlacementScope({super.key, required super.child});
  static bool active(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<EllaCaptureDockPlacementScope>() != null;
  @override
  bool updateShouldNotify(EllaCaptureDockPlacementScope oldWidget) => false;
}

/// Pages receive a viewport already ending above the shell navigation.
class EllaBottomNavigationPlacementScope extends InheritedWidget {
  const EllaBottomNavigationPlacementScope({super.key, required super.child});
  static bool active(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<EllaBottomNavigationPlacementScope>() != null;
  @override
  bool updateShouldNotify(EllaBottomNavigationPlacementScope oldWidget) => false;
}
