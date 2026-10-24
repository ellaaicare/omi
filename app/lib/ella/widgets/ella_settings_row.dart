import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

class EllaSettingsRow extends StatelessWidget {
  final IconData icon;
  final Color? iconColor;
  final Color? iconBgColor;
  final String title;
  final String? subtitle;
  final VoidCallback? onTap;
  final bool showChevron;
  final Color? titleColor;

  const EllaSettingsRow({
    super.key,
    required this.icon,
    this.iconColor,
    this.iconBgColor,
    required this.title,
    this.subtitle,
    this.onTap,
    this.showChevron = true,
    this.titleColor,
  });

  @override
  Widget build(BuildContext context) {
    final effectiveIconColor = iconColor ?? EllaPalette.of(context).primary;
    final effectiveIconBgColor = iconBgColor ?? effectiveIconColor;
    final largeText = MediaQuery.textScalerOf(context).scale(18) >= 32;
    final titleWidget = Text(title,
        style: TextStyle(
            fontSize: 18, fontWeight: FontWeight.w500, color: titleColor ?? EllaPalette.of(context).textPrimary));
    final subtitleWidget = subtitle == null
        ? null
        : Text(subtitle!,
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaPalette.of(context).textTertiary));
    final iconWidget = Container(
        width: 40,
        height: 40,
        decoration: BoxDecoration(shape: BoxShape.circle, color: effectiveIconBgColor.withValues(alpha: .15)),
        child: Icon(icon, size: 20, color: effectiveIconColor));
    final chevron = Icon(Icons.chevron_right, size: 24, color: EllaPalette.of(context).textTertiary);

    return Semantics(
      button: true,
      label: '$title${subtitle != null ? '. $subtitle' : ''}',
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: Container(
          constraints: const BoxConstraints(minHeight: 64),
          padding: EdgeInsets.symmetric(horizontal: largeText ? 16 : 24, vertical: 16),
          decoration: BoxDecoration(
            color: EllaPalette.of(context).bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
          ),
          child: largeText
              ? Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [iconWidget, const Spacer(), if (showChevron) chevron]),
                  const SizedBox(height: 8),
                  titleWidget,
                  if (subtitleWidget != null) ...[const SizedBox(height: 4), subtitleWidget],
                ])
              : Row(children: [
                  iconWidget,
                  const SizedBox(width: 16),
                  Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    titleWidget,
                    if (subtitleWidget != null) ...[const SizedBox(height: 2), subtitleWidget],
                  ])),
                  if (showChevron) chevron,
                ]),
        ),
      ),
    );
  }
}
