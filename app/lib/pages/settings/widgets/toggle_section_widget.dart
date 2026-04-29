import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

class ToggleSectionWidget extends StatefulWidget {
  final bool isSectionEnabled;
  final String sectionTitle;
  final String sectionDescription;
  final List<Widget> options;
  final Function(bool) onSectionEnabledChanged;

  const ToggleSectionWidget(
      {super.key,
      required this.isSectionEnabled,
      required this.sectionTitle,
      required this.sectionDescription,
      required this.options,
      required this.onSectionEnabledChanged});
  @override
  State<ToggleSectionWidget> createState() => _ToggleSectionWidgetState();
}

class _ToggleSectionWidgetState extends State<ToggleSectionWidget> {
  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: EllaColors.bgTertiary, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      widget.sectionTitle,
                      style: const TextStyle(
                        color: EllaColors.textPrimary,
                        fontSize: 15,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      widget.sectionDescription,
                      style: const TextStyle(
                        color: EllaColors.textSecondary,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
              Switch(
                value: widget.isSectionEnabled,
                onChanged: widget.onSectionEnabledChanged,
                activeThumbColor: EllaColors.primary,
                activeTrackColor: EllaColors.primarySubtle,
              ),
            ],
          ),
          if (widget.isSectionEnabled)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisAlignment: MainAxisAlignment.start,
                children: widget.options,
              ),
            ),
        ],
      ),
    );
  }
}
