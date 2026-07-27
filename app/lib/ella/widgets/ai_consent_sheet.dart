import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AiConsentSheet extends StatefulWidget {
  static final Uri privacyPolicyUri = Uri.parse('https://ella-ai-care.com/privacy');

  const AiConsentSheet({
    super.key,
    this.onAccept,
    this.onDecline,
    this.onRequestDeletion,
    this.reviewMode = false,
  });

  final Future<bool> Function()? onAccept;
  final Future<bool> Function()? onDecline;
  final Future<void> Function()? onRequestDeletion;
  final bool reviewMode;

  static Future<bool?> show(
    BuildContext context, {
    Future<bool> Function()? onAccept,
    Future<bool> Function()? onDecline,
    Future<void> Function()? onRequestDeletion,
    bool reviewMode = false,
  }) {
    return showModalBottomSheet<bool>(
      context: context,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.paper,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      builder: (_) => FractionallySizedBox(
        heightFactor: reviewMode ? 0.78 : 0.68,
        child: AiConsentSheet(
          onAccept: onAccept,
          onDecline: onDecline,
          onRequestDeletion: onRequestDeletion,
          reviewMode: reviewMode,
        ),
      ),
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
      final accepted = await (widget.onAccept?.call() ?? Future<bool>.value(false));
      if (!mounted) return;
      if (accepted) {
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

  Future<void> _decline() async {
    if (_isSubmitting) return;
    setState(() => _isSubmitting = true);

    final preferences = SharedPreferencesUtil();
    if (widget.reviewMode) {
      preferences.declineAiConsent();
    } else {
      preferences.deferAiConsent();
    }

    try {
      await widget.onDecline?.call();
    } finally {
      if (mounted) Navigator.of(context).pop(false);
    }
  }

  Future<void> _requestDeletion() async {
    if (_isSubmitting || widget.onRequestDeletion == null) return;
    Navigator.of(context).pop(false);
    await widget.onRequestDeletion!.call();
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
                    const SizedBox(height: 12),
                    Text(context.l10n.aiConsentNoSharingBeforeAllow, style: bodyStyle),
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
                      Text(context.l10n.somethingWentWrongTryAgain,
                          style: bodyStyle?.copyWith(color: EllaColors.error)),
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
                      onPressed: _isSubmitting ? null : _decline,
                      child: Text(
                        widget.reviewMode ? context.l10n.aiConsentRevokeAction : context.l10n.notNow,
                        style: const TextStyle(
                          fontSize: 17,
                          color: EllaColors.inkSoft,
                          decoration: TextDecoration.underline,
                          decorationColor: EllaColors.inkSoft,
                        ),
                      ),
                    ),
                  ),
                  if (widget.reviewMode && widget.onRequestDeletion != null)
                    TextButton(
                      onPressed: _isSubmitting ? null : _requestDeletion,
                      child: Text(
                        context.l10n.aiConsentDeleteDataAction,
                        style: const TextStyle(fontSize: 16, color: EllaColors.error),
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
