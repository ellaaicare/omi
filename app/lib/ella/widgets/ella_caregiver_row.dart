import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaCaregiverRow extends StatelessWidget {
  final Caregiver caregiver;
  final VoidCallback? onTap;

  const EllaCaregiverRow({
    super.key,
    required this.caregiver,
    this.onTap,
  });

  Color get _statusColor => caregiver.isActive
      ? EllaColors.success
      : caregiver.isExpired
          ? EllaColors.error
          : EllaColors.warning;

  String _statusLabel(BuildContext context) {
    return caregiver.isActive
        ? context.l10n.ellaCaregiverStatusActive
        : caregiver.isExpired
            ? context.l10n.ellaCaregiverStatusExpired
            : context.l10n.ellaCaregiverStatusInvited;
  }

  @override
  Widget build(BuildContext context) {
    final statusLabel = _statusLabel(context);

    return Semantics(
      button: true,
      label: '${caregiver.name}, ${caregiver.displayRelationship}, $statusLabel',
      hint: 'Double tap to view details',
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
          ),
          child: Row(
            children: [
              Container(
                width: 48,
                height: 48,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: EllaColors.primary.withValues(alpha: 0.15),
                ),
                child: Center(
                  child: Text(
                    caregiver.initial,
                    style: const TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.w600,
                      color: EllaColors.primary,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      caregiver.name,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                        color: EllaColors.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      caregiver.displayRelationship,
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w400,
                        color: EllaColors.textTertiary,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Row(
                      children: [
                        Container(
                          width: 8,
                          height: 8,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: _statusColor,
                          ),
                        ),
                        const SizedBox(width: 6),
                        Text(
                          statusLabel,
                          style: TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w400,
                            color: _statusColor,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right, size: 24, color: EllaColors.textTertiary),
            ],
          ),
        ),
      ),
    );
  }
}
