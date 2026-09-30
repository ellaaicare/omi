import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Ella 4-tab bottom navigation: Home, Chat, Talk, Settings.
///
/// - Text labels always visible (elder-friendly)
/// - Teal active color, no center record button
/// - At least 80dp high, with additional space for scaled labels and safe area
class BottomNavBar extends StatelessWidget {
  const BottomNavBar({
    super.key,
    required this.onTabTap,
  });

  final void Function(int index, bool isRepeat) onTabTap;

  static TextStyle _labelStyle(BuildContext context, {required bool isSelected}) {
    final textScale = MediaQuery.textScalerOf(context).scale(14) / 14;
    return Theme.of(context).textTheme.bodyMedium!.copyWith(
          color: isSelected ? EllaColors.primary : EllaColors.textTertiary,
          fontSize: textScale >= 1.8 ? 10 : 14,
          fontWeight: isSelected ? FontWeight.w500 : FontWeight.w400,
        );
  }

  /// Shared with page clearances so larger labels never cover page controls.
  static double navigationHeight(BuildContext context) {
    final media = MediaQuery.of(context);
    final tabWidth = (media.size.width - media.padding.horizontal) / 4;
    final labels = [
      context.l10n.bottomNavHome,
      context.l10n.bottomNavChat,
      context.l10n.bottomNavTalk,
      context.l10n.bottomNavSettings,
    ];
    var height = EllaSizes.navBarHeight;
    for (final label in labels) {
      final painter = TextPainter(
        text: TextSpan(text: label, style: _labelStyle(context, isSelected: true)),
        textDirection: Directionality.of(context),
        textScaler: media.textScaler,
        locale: Localizations.localeOf(context),
        textAlign: TextAlign.center,
      )..layout(maxWidth: tabWidth);
      final requiredHeight = EllaSizes.iconMedium + 4 + painter.height + 16;
      if (requiredHeight > height) height = requiredHeight.ceilToDouble();
      painter.dispose();
    }
    return height;
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<HomeProvider>(
      builder: (context, home, child) {
        final height = navigationHeight(context);
        return Align(
          alignment: Alignment.bottomCenter,
          child: Container(
            width: double.infinity,
            decoration: const BoxDecoration(
              color: EllaColors.bgSecondary,
              border: Border(
                top: BorderSide(color: EllaColors.bgTertiary, width: 0.5),
              ),
            ),
            child: SafeArea(
              top: false,
              child: SizedBox(
                height: height,
                child: Row(
                  children: [
                    _NavTab(
                      icon: FontAwesomeIcons.house,
                      label: context.l10n.bottomNavHome,
                      height: height,
                      isSelected: home.selectedIndex == 0,
                      onTap: () {
                        HapticFeedback.mediumImpact();
                        MixpanelManager().bottomNavigationTabClicked('Home');
                        primaryFocus?.unfocus();
                        onTabTap(0, home.selectedIndex == 0);
                      },
                    ),
                    _NavTab(
                      icon: FontAwesomeIcons.solidComment,
                      label: context.l10n.bottomNavChat,
                      height: height,
                      isSelected: home.selectedIndex == 1,
                      onTap: () {
                        HapticFeedback.mediumImpact();
                        MixpanelManager().bottomNavigationTabClicked('Chat');
                        primaryFocus?.unfocus();
                        onTabTap(1, home.selectedIndex == 1);
                      },
                    ),
                    _NavTab(
                      icon: FontAwesomeIcons.waveSquare,
                      label: context.l10n.bottomNavTalk,
                      height: height,
                      isSelected: home.selectedIndex == 2,
                      onTap: () {
                        HapticFeedback.mediumImpact();
                        MixpanelManager().bottomNavigationTabClicked('Talk');
                        primaryFocus?.unfocus();
                        onTabTap(2, home.selectedIndex == 2);
                      },
                    ),
                    _NavTab(
                      icon: FontAwesomeIcons.gear,
                      label: context.l10n.bottomNavSettings,
                      height: height,
                      isSelected: home.selectedIndex == 3,
                      onTap: () {
                        HapticFeedback.mediumImpact();
                        MixpanelManager().bottomNavigationTabClicked('Settings');
                        primaryFocus?.unfocus();
                        onTabTap(3, home.selectedIndex == 3);
                      },
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

class _NavTab extends StatelessWidget {
  const _NavTab({
    required this.icon,
    required this.label,
    required this.height,
    required this.isSelected,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final double height;
  final bool isSelected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = isSelected ? EllaColors.primary : EllaColors.textTertiary;
    return Expanded(
      child: Semantics(
        button: true,
        selected: isSelected,
        child: InkWell(
          onTap: onTap,
          child: SizedBox(
            height: height,
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(icon, color: color, size: EllaSizes.iconMedium),
                const SizedBox(height: 4),
                Text(
                  label,
                  textAlign: TextAlign.center,
                  style: BottomNavBar._labelStyle(context, isSelected: isSelected),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
