import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

class EllaPermissionToggle extends StatelessWidget {
  final String title;
  final String description;
  final bool isOn;
  final ValueChanged<bool>? onChanged;
  final bool locked;
  final BorderRadius? borderRadius;

  const EllaPermissionToggle({
    super.key,
    required this.title,
    required this.description,
    required this.isOn,
    this.onChanged,
    this.locked = false,
    this.borderRadius,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      toggled: isOn,
      label: '$title. $description',
      hint: locked ? 'This setting cannot be changed' : 'Double tap to toggle',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        decoration: BoxDecoration(
          color: EllaColors.bgSecondary,
          borderRadius: borderRadius ?? BorderRadius.circular(EllaSizes.radiusLarge),
        ),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      if (locked) ...[
                        const Icon(Icons.lock, size: 16, color: EllaColors.textDisabled),
                        const SizedBox(width: 8),
                      ],
                      Flexible(
                        child: Text(
                          title,
                          style: const TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.w500,
                            color: EllaColors.textPrimary,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    description,
                    style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w400,
                      color: EllaColors.textTertiary,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 16),
            CupertinoSwitch(
              value: isOn,
              onChanged: locked ? null : onChanged,
              activeTrackColor: EllaColors.primary,
            ),
          ],
        ),
      ),
    );
  }
}
