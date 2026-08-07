import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_legal_links.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AiConsentSheet extends StatefulWidget {
  static final Uri privacyPolicyUri = EllaLegalLinks.privacy;

  const AiConsentSheet({super.key, this.onAccept, this.onDecline, this.onRequestDeletion, this.reviewMode = false});

  final Future<AiConsentGrantOutcome> Function()? onAccept;
  final Future<bool> Function()? onDecline;
  final Future<void> Function()? onRequestDeletion;
  final bool reviewMode;

  static Future<bool?> show(
    BuildContext context, {
    Future<AiConsentGrantOutcome> Function()? onAccept,
    Future<bool> Function()? onDecline,
    Future<void> Function()? onRequestDeletion,
    bool reviewMode = false,
    bool pilotLocaleRestricted = isEllaInternalPilotEnabled,
  }) {
    if (pilotLocaleRestricted && !isEllaInternalPilotLocaleSupported(Localizations.localeOf(context).languageCode)) {
      return Future<bool?>.value();
    }
    return showModalBottomSheet<bool>(
      context: context,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.paper,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      builder: (_) => FractionallySizedBox(
        heightFactor: 0.92,
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
  static const _genericFailure = AiConsentGrantOutcome.failed(AiConsentGrantFailureKind.rejected);

  bool _isSubmitting = false;
  AiConsentGrantFailureKind? _failureKind;
  String _supportCode = '';

  Future<void> _accept() async {
    if (_isSubmitting) return;
    setState(() {
      _isSubmitting = true;
      _failureKind = null;
      _supportCode = '';
    });

    var outcome = _genericFailure;
    try {
      outcome = await (widget.onAccept?.call() ?? Future<AiConsentGrantOutcome>.value(_genericFailure));
      if (!mounted) return;
      if (outcome.accepted) {
        Navigator.of(context).pop(true);
        return;
      }
    } catch (_) {
      // Keep the consent surface open and capture disabled on acknowledgement failure.
      outcome = _genericFailure;
    }

    if (mounted) {
      setState(() {
        _isSubmitting = false;
        _failureKind = outcome.failureKind ?? AiConsentGrantFailureKind.rejected;
        _supportCode = outcome.supportCode;
      });
    }
  }

  String _failureMessage(BuildContext context) => switch (_failureKind) {
        AiConsentGrantFailureKind.serverUnavailable => context.l10n.aiConsentGrantUnavailableBody,
        AiConsentGrantFailureKind.policyMismatch => context.l10n.aiConsentGrantPolicyMismatchBody,
        AiConsentGrantFailureKind.network => context.l10n.aiConsentGrantNetworkBody,
        _ => context.l10n.somethingWentWrongTryAgain,
      };

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

  Widget _processorDisclosure({required IconData icon, required String title, required String body}) {
    return Semantics(
      container: true,
      label: '$title. $body',
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: EllaColors.bgSecondary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
          border: Border.all(color: EllaColors.cardEdge),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, color: EllaColors.primary, size: 22),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 4),
                  Text(
                    body,
                    style: Theme.of(
                      context,
                    ).textTheme.bodyMedium?.copyWith(color: EllaColors.textSecondary, height: 1.4),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
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
                    Text(context.l10n.aiConsentManagedCloudIntro, style: bodyStyle),
                    const SizedBox(height: 12),
                    _processorDisclosure(
                      icon: Icons.cloud_outlined,
                      title: context.l10n.aiConsentHermesCloudTitle,
                      body: context.l10n.aiConsentHermesCloudBody,
                    ),
                    const SizedBox(height: 10),
                    _processorDisclosure(
                      icon: Icons.auto_awesome_outlined,
                      title: context.l10n.aiConsentOpenAiManagedTitle,
                      body: context.l10n.aiConsentOpenAiManagedBody,
                    ),
                    const SizedBox(height: 10),
                    _processorDisclosure(
                      icon: Icons.psychology_alt_outlined,
                      title: context.l10n.aiConsentHermesProfileMemoryTitle,
                      body: context.l10n.aiConsentHermesProfileMemoryBody,
                    ),
                    const SizedBox(height: 10),
                    _processorDisclosure(
                      icon: Icons.message_outlined,
                      title: context.l10n.aiConsentPhotonTitle,
                      body: context.l10n.aiConsentPhotonBody,
                    ),
                    const SizedBox(height: 12),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(14),
                      decoration: BoxDecoration(
                        color: EllaColors.primarySubtle,
                        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                      ),
                      child: Text(context.l10n.aiConsentManagedCloudScope, style: bodyStyle),
                    ),
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
                  ],
                ),
              ),
            ),
            if (_failureKind != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 0, 24, 10),
                child: Semantics(
                  liveRegion: true,
                  child: Column(
                    children: [
                      Text(
                        _failureMessage(context),
                        style: EllaTextStyles.secondary.copyWith(color: EllaColors.error),
                        textAlign: TextAlign.center,
                      ),
                      if (_supportCode.isNotEmpty) ...[
                        const SizedBox(height: 4),
                        Text(_supportCode, style: EllaTextStyles.caption, textAlign: TextAlign.center),
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
