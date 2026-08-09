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
/// - 80dp height including safe area
class BottomNavBar extends StatelessWidget {
  const BottomNavBar({
    super.key,
    required this.onTabTap,
  });

  final void Function(int index, bool isRepeat) onTabTap;

  @override
  Widget build(BuildContext context) {
    return Consumer<HomeProvider>(
      builder: (context, home, child) {
        final textScale = MediaQuery.textScalerOf(context).scale(14) / 14;
        const navigationHeight = EllaSizes.navBarHeight;
        final labelFontSize = textScale >= 1.8 ? 10.0 : 14.0;
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
                height: navigationHeight,
                child: Row(
                  children: [
                    _NavTab(
                      icon: FontAwesomeIcons.house,
                      label: context.l10n.bottomNavHome,
                      height: navigationHeight,
                      labelFontSize: labelFontSize,
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
                      height: navigationHeight,
                      labelFontSize: labelFontSize,
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
                      height: navigationHeight,
                      labelFontSize: labelFontSize,
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
                      height: navigationHeight,
                      labelFontSize: labelFontSize,
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
    required this.labelFontSize,
    required this.isSelected,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final double height;
  final double labelFontSize;
  final bool isSelected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = isSelected ? EllaColors.primary : EllaColors.textTertiary;
    return Expanded(
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
                maxLines: 2,
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: color,
                  fontSize: labelFontSize,
                  fontWeight: isSelected ? FontWeight.w500 : FontWeight.w400,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
