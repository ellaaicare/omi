import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/platform/platform_service.dart';
import 'package:omi/ella/ella_theme.dart';

bool shouldShowInternalAssessmentDebugUi({
  required bool isDebugMode,
  required bool isIOS,
  required ServerConversation conversation,
}) {
  return isDebugMode && isIOS && conversation.hasInternalAssessment;
}

class DebugInternalAssessmentSheet extends StatelessWidget {
  final ServerConversation conversation;

  const DebugInternalAssessmentSheet({super.key, required this.conversation});

  @visibleForTesting
  static bool isSupported(ServerConversation conversation, {bool isDebugMode = kDebugMode, bool isIOS = false}) {
    return shouldShowInternalAssessmentDebugUi(
      isDebugMode: isDebugMode,
      isIOS: isIOS,
      conversation: conversation,
    );
  }

  @override
  Widget build(BuildContext context) {
    final payload = conversation.internalAssessmentDebugText;

    return SafeArea(
      child: Container(
        decoration: const BoxDecoration(
          color: EllaColors.bgPrimary,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    context.l10n.developer,
                    style: const TextStyle(
                      color: EllaColors.textPrimary,
                      fontSize: 18,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                IconButton(
                  onPressed: payload == null
                      ? null
                      : () {
                          Clipboard.setData(ClipboardData(text: payload));
                          ScaffoldMessenger.of(
                            context,
                          ).showSnackBar(SnackBar(content: Text(context.l10n.summaryCopiedToClipboard)));
                        },
                  icon: const Icon(Icons.copy_rounded, color: EllaColors.textPrimary),
                ),
              ],
            ),
            const SizedBox(height: 8),
            ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 360),
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: EllaColors.bgSecondary,
                  borderRadius: BorderRadius.circular(18),
                  border: Border.all(color: EllaColors.bgTertiary),
                ),
                child: SingleChildScrollView(
                  child: SelectableText(
                    payload ?? context.l10n.noSummaryForConversation,
                    style: TextStyle(
                      color: EllaColors.textPrimary,
                      fontSize: 13,
                      height: 1.4,
                      fontFamily: PlatformService.isIOS ? 'Menlo' : null,
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
