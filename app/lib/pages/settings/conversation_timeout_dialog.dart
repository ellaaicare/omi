import 'package:flutter/material.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/l10n_extensions.dart';

class ConversationTimeoutDialog {
  static Future<void> show(BuildContext context) async {
    final currentDuration = SharedPreferencesUtil().conversationSilenceDuration;
    int selectedDuration = currentDuration;

    // Timeout options: 2 mins, 5 mins, 10 mins, 30 mins, 4 hours
    final timeoutOptions = [
      {'label': context.l10n.timeout2Minutes, 'value': 120, 'description': context.l10n.timeout2MinutesDesc},
      {'label': context.l10n.timeout5Minutes, 'value': 300, 'description': context.l10n.timeout5MinutesDesc},
      {'label': context.l10n.timeout10Minutes, 'value': 600, 'description': context.l10n.timeout10MinutesDesc},
      {'label': context.l10n.timeout30Minutes, 'value': 1800, 'description': context.l10n.timeout30MinutesDesc},
      {'label': context.l10n.timeout4Hours, 'value': -1, 'description': context.l10n.timeout4HoursDesc},
    ];

    await showDialog(
      context: context,
      builder: (context) {
        return StatefulBuilder(
          builder: (context, setState) {
            return AlertDialog(
              backgroundColor: Colors.white,
              surfaceTintColor: Colors.transparent,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
              title: Text(
                context.l10n.conversationTimeout,
                style: const TextStyle(
                  color: EllaColors.textPrimary,
                  fontSize: 20,
                  fontWeight: FontWeight.w600,
                ),
              ),
              content: SizedBox(
                width: double.maxFinite,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      context.l10n.conversationTimeoutDesc,
                      style: const TextStyle(
                        color: EllaColors.textSecondary,
                        fontSize: 14,
                      ),
                    ),
                    const SizedBox(height: 16),
                    ...timeoutOptions.map((option) {
                      final isSelected = selectedDuration == option['value'];
                      return Container(
                        margin: const EdgeInsets.only(bottom: 8),
                        child: Material(
                          color: Colors.transparent,
                          child: InkWell(
                            borderRadius: BorderRadius.circular(12),
                            onTap: () {
                              setState(() {
                                selectedDuration = option['value'] as int;
                              });
                            },
                            child: Container(
                              padding: const EdgeInsets.all(16),
                              decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(12),
                                border: Border.all(
                                  color: isSelected ? EllaColors.primary : EllaColors.bgTertiary,
                                  width: isSelected ? 2 : 1,
                                ),
                                color: isSelected ? EllaColors.primarySubtle : Colors.transparent,
                              ),
                              child: Row(
                                children: [
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment: CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                          option['label'] as String,
                                          style: TextStyle(
                                            color: EllaColors.textPrimary,
                                            fontSize: 16,
                                            fontWeight: isSelected ? FontWeight.w600 : FontWeight.w400,
                                          ),
                                        ),
                                        const SizedBox(height: 4),
                                        Text(
                                          option['description'] as String,
                                          style: const TextStyle(
                                            color: EllaColors.textSecondary,
                                            fontSize: 12,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                  if (isSelected)
                                    const Icon(
                                      Icons.check_circle,
                                      color: EllaColors.primary,
                                      size: 20,
                                    ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      );
                    }),
                  ],
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () {
                    Navigator.of(context).pop();
                  },
                  child: Text(
                    context.l10n.cancel,
                    style: const TextStyle(color: EllaColors.textSecondary),
                  ),
                ),
                TextButton(
                  onPressed: () {
                    SharedPreferencesUtil().conversationSilenceDuration = selectedDuration;
                    Navigator.of(context).pop();

                    // Show confirmation
                    String message;
                    if (selectedDuration == -1) {
                      message = context.l10n.conversationEndAfterHours;
                    } else {
                      final minutes = selectedDuration ~/ 60;
                      message = context.l10n.conversationEndAfterMinutes(minutes);
                    }
                    AppSnackbar.showSnackbar(message);
                  },
                  child: Text(
                    context.l10n.save,
                    style: const TextStyle(color: EllaColors.primary, fontWeight: FontWeight.w600),
                  ),
                ),
              ],
            );
          },
        );
      },
    );
  }
}
