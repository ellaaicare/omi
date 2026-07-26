import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_legal_links.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AiConsentSheet extends StatefulWidget {
  static final Uri privacyPolicyUri = EllaLegalLinks.privacy;

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
      builder: (_) => FractionallySizedBox(heightFactor: 0.68, child: AiConsentSheet(onAccept: onAccept)),
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
      child: Padding(
        padding: EdgeInsets.only(bottom: 12 + MediaQuery.paddingOf(context).bottom),
        child: Column(
          children: [
            Expanded(
              child: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(24, 16, 24, 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(context.l10n.aiConsentTitle, style: EllaTextStyles.display),
                    const SizedBox(height: 16),
                    Text(context.l10n.aiConsentCompactSummary, style: bodyStyle),
                    const SizedBox(height: 14),
                    Text.rich(
                      TextSpan(
                        text: context.l10n.aiConsentProcessorDetailsLink,
                        style: bodyStyle?.copyWith(
                          color: EllaColors.primary,
                          decoration: TextDecoration.underline,
                          decorationColor: EllaColors.primary,
                        ),
                        recognizer: TapGestureRecognizer()..onTap = () => launchUrl(AiConsentSheet.privacyPolicyUri),
                      ),
                    ),
                    if (_hasError) ...[
                      const SizedBox(height: 12),
                      Text(
                        context.l10n.somethingWentWrongTryAgain,
                        style: bodyStyle?.copyWith(color: EllaColors.error),
                      ),
                    ],
                  ],
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Column(
                children: [
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
                  const SizedBox(height: 4),
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
          ],
        ),
      ),
    );
  }
}
