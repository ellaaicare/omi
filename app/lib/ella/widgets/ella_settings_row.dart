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
    final effectiveIconColor = iconColor ?? EllaColors.primary;
    final effectiveIconBgColor = iconBgColor ?? effectiveIconColor;

    return Semantics(
      button: true,
      label: '$title${subtitle != null ? '. $subtitle' : ''}',
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: Container(
          constraints: const BoxConstraints(minHeight: 64),
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
          decoration: BoxDecoration(
            color: EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
          ),
          child: Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: effectiveIconBgColor.withOpacity(0.15),
                ),
                child: Icon(icon, size: 20, color: effectiveIconColor),
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w500,
                        color: titleColor ?? EllaColors.textPrimary,
                      ),
                    ),
                    if (subtitle != null) ...[
                      const SizedBox(height: 2),
                      Text(
                        subtitle!,
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w400,
                          color: EllaColors.textTertiary,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
              if (showChevron) const Icon(Icons.chevron_right, size: 24, color: EllaColors.textTertiary),
            ],
          ),
        ),
      ),
    );
  }
}
