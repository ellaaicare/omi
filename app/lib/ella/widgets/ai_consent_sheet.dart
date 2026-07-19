import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AiConsentSheet extends StatelessWidget {
  static final Uri privacyPolicyUri = Uri.parse('https://ella-ai-care.com/privacy');

  const AiConsentSheet({super.key});

  static Future<bool?> show(BuildContext context) {
    return showModalBottomSheet<bool>(
      context: context,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.bgSecondary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(8))),
      builder: (_) => const AiConsentSheet(),
    );
  }

  @override
  Widget build(BuildContext context) {
    final bodyStyle = Theme.of(context).textTheme.bodyLarge?.copyWith(
          color: EllaColors.textSecondary,
          height: 1.5,
        );
    return PopScope(
      canPop: false,
      child: SingleChildScrollView(
        padding: EdgeInsets.fromLTRB(24, 28, 24, 24 + MediaQuery.paddingOf(context).bottom),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              context.l10n.aiConsentTitle,
              style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
            ),
            const SizedBox(height: 16),
            Text.rich(
              TextSpan(
                style: bodyStyle,
                children: [
                  TextSpan(text: context.l10n.aiConsentBodyIntro),
                  TextSpan(text: context.l10n.aiConsentDeepgram, style: const TextStyle(fontWeight: FontWeight.w700)),
                  TextSpan(text: context.l10n.aiConsentBodyMiddle),
                  TextSpan(text: context.l10n.aiConsentAiPartners, style: const TextStyle(fontWeight: FontWeight.w700)),
                  TextSpan(text: context.l10n.aiConsentBodyEnd),
                ],
              ),
            ),
            const SizedBox(height: 14),
            Text.rich(
              TextSpan(
                text: context.l10n.privacyPolicy,
                style: bodyStyle?.copyWith(
                  color: EllaColors.primary,
                  decoration: TextDecoration.underline,
                  decorationColor: EllaColors.primary,
                ),
                recognizer: TapGestureRecognizer()..onTap = () => launchUrl(privacyPolicyUri),
              ),
            ),
            const SizedBox(height: 28),
            SizedBox(
              width: double.infinity,
              height: 54,
              child: FilledButton(
                onPressed: () {
                  SharedPreferencesUtil().acceptAiConsent();
                  Navigator.of(context).pop(true);
                },
                style: FilledButton.styleFrom(
                  backgroundColor: EllaColors.primary,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                ),
                child: Text(context.l10n.allowAndContinue, style: const TextStyle(fontSize: 17)),
              ),
            ),
            const SizedBox(height: 10),
            SizedBox(
              width: double.infinity,
              height: 50,
              child: TextButton(
                onPressed: () {
                  SharedPreferencesUtil().declineAiConsent();
                  Navigator.of(context).pop(false);
                },
                child: Text(context.l10n.notNow, style: const TextStyle(fontSize: 17)),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
