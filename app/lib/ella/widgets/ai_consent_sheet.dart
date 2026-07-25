import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AiConsentSheet extends StatefulWidget {
  static final Uri privacyPolicyUri = Uri.parse('https://ella-ai-care.com/privacy');

  const AiConsentSheet({super.key, this.onAccept});

  final Future<bool> Function()? onAccept;

  static Future<bool?> show(BuildContext context, {Future<bool> Function()? onAccept}) {
    return showModalBottomSheet<bool>(
      context: context,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.paper,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      builder: (_) => AiConsentSheet(onAccept: onAccept),
    );
  }

  @override
  State<AiConsentSheet> createState() => _AiConsentSheetState();
}

class _AiConsentSheetState extends State<AiConsentSheet> {
  bool _isSubmitting = false;
  bool _hasError = false;

  Future<void> _accept() async {
    if (_isSubmitting) return;
    setState(() {
      _isSubmitting = true;
      _hasError = false;
    });

    try {
      final accepted = await (widget.onAccept?.call() ?? Future<bool>.value(true));
      if (!mounted) return;
      if (accepted) {
        if (widget.onAccept == null) SharedPreferencesUtil().acceptAiConsent();
        Navigator.of(context).pop(true);
        return;
      }
    } catch (_) {
      // Keep the consent surface open and capture disabled on acknowledgement failure.
    }

    if (mounted) {
      setState(() {
        _isSubmitting = false;
        _hasError = true;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final bodyStyle = Theme.of(context).textTheme.bodyLarge?.copyWith(color: EllaColors.textSecondary, height: 1.5);
    return PopScope(
      canPop: false,
      child: SingleChildScrollView(
        padding: EdgeInsets.fromLTRB(24, 16, 24, 44 + MediaQuery.paddingOf(context).bottom),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(context.l10n.aiConsentTitle, style: EllaTextStyles.display),
            const SizedBox(height: 16),
            Text.rich(
              TextSpan(
                style: bodyStyle,
                children: [
                  TextSpan(text: context.l10n.aiConsentBodyIntro),
                  TextSpan(
                    text: context.l10n.aiConsentDeepgram,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  TextSpan(text: context.l10n.aiConsentBodyMiddle),
                  TextSpan(
                    text: context.l10n.aiConsentAiPartners,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  TextSpan(text: context.l10n.aiConsentBodyBeforeElevenLabs),
                  TextSpan(
                    text: context.l10n.aiConsentElevenLabs,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  TextSpan(text: context.l10n.aiConsentBodyEnd),
                  TextSpan(text: context.l10n.aiConsentMemoryContext),
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
                recognizer: TapGestureRecognizer()..onTap = () => launchUrl(AiConsentSheet.privacyPolicyUri),
              ),
            ),
            const SizedBox(height: 28),
            if (_hasError) ...[
              Text(context.l10n.somethingWentWrongTryAgain, style: bodyStyle?.copyWith(color: EllaColors.error)),
              const SizedBox(height: 12),
            ],
            SizedBox(
              width: double.infinity,
              height: 56,
              child: FilledButton(
                onPressed: _isSubmitting ? null : _accept,
                style: FilledButton.styleFrom(
                  backgroundColor: EllaColors.tealDeep,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
                ),
                child: _isSubmitting
                    ? const SizedBox(
                        width: 22,
                        height: 22,
                        child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.paper),
                      )
                    : Text(context.l10n.allowAndContinue, style: const TextStyle(fontSize: 17)),
              ),
            ),
            const SizedBox(height: 10),
            SizedBox(
              width: double.infinity,
              height: 50,
              child: TextButton(
                onPressed: _isSubmitting
                    ? null
                    : () {
                        SharedPreferencesUtil().deferAiConsent();
                        Navigator.of(context).pop(false);
                      },
                child: Text(
                  context.l10n.notNow,
                  style: const TextStyle(
                    fontSize: 17,
                    color: EllaColors.inkSoft,
                    decoration: TextDecoration.underline,
                    decorationColor: EllaColors.inkSoft,
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
