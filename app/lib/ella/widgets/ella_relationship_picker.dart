import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaRelationshipPicker extends StatelessWidget {
  final String? selected;
  final ValueChanged<String> onSelected;

  const EllaRelationshipPicker({
    super.key,
    this.selected,
    required this.onSelected,
  });

  static Future<String?> show(BuildContext context, {String? current}) async {
    return showModalBottomSheet<String>(
      context: context,
      backgroundColor: EllaColors.bgSecondary,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(EllaSizes.radiusLarge)),
      ),
      builder: (ctx) => EllaRelationshipPicker(
        selected: current,
        onSelected: (value) => Navigator.of(ctx).pop(value),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final options = [
      ('daughter', context.l10n.ellaRelationshipDaughter),
      ('son', context.l10n.ellaRelationshipSon),
      ('spouse', context.l10n.ellaRelationshipSpouse),
      ('sibling', context.l10n.ellaRelationshipSibling),
      ('friend', context.l10n.ellaRelationshipFriend),
      ('doctor', context.l10n.ellaRelationshipDoctor),
      ('other', context.l10n.ellaRelationshipOther),
    ];

    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const SizedBox(height: 12),
          Container(
            width: 40,
            height: 4,
            decoration: BoxDecoration(
              color: EllaColors.textDisabled,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(height: 16),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(
                context.l10n.ellaAddCaregiverRelationship,
                style: const TextStyle(
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                  color: EllaColors.textPrimary,
                ),
              ),
            ),
          ),
          const SizedBox(height: 16),
          ...options.map((option) {
            final isSelected = selected == option.$1;
            return Column(
              children: [
                Semantics(
                  button: true,
                  label: '${option.$2}${isSelected ? ', selected' : ''}',
                  child: InkWell(
                    onTap: () => onSelected(option.$1),
                    child: Container(
                      height: 56,
                      padding: const EdgeInsets.symmetric(horizontal: 24),
                      decoration: BoxDecoration(
                        border: isSelected ? const Border(left: BorderSide(color: EllaColors.primary, width: 4)) : null,
                      ),
                      child: Align(
                        alignment: Alignment.centerLeft,
                        child: Text(
                          option.$2,
                          style: TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w400,
                            color: isSelected ? EllaColors.primary : EllaColors.textPrimary,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                if (option != options.last)
                  const Divider(height: 0.5, thickness: 0.5, color: EllaColors.bgTertiary, indent: 24, endIndent: 24),
              ],
            );
          }),
          const SizedBox(height: 16),
        ],
      ),
    );
  }
}
