import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/utils/auth_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class EllaProvisioningGatePage extends StatefulWidget {
  const EllaProvisioningGatePage({
    super.key,
    required this.readyChild,
    this.startOnMount = true,
    this.onSignOutOverride,
  });

  final Widget readyChild;
  final bool startOnMount;
  final VoidCallback? onSignOutOverride;

  @override
  State<EllaProvisioningGatePage> createState() => _EllaProvisioningGatePageState();
}

class _EllaProvisioningGatePageState extends State<EllaProvisioningGatePage> with WidgetsBindingObserver {
  bool _consentDeferred = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (widget.startOnMount) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _start());
    }
  }

  Future<void> _start({bool forceConsentPrompt = false}) async {
    final user = FirebaseAuth.instance.currentUser;
    if (user == null || !mounted) return;

    final preferences = SharedPreferencesUtil();
    preferences.uid = user.uid;
    await const EllaAccountIsolationService().prepareProvisioningAccount(user.uid, preferences: preferences);
    if (!mounted) return;

    final consentService = EllaAiConsentService();
    var hasConsent = await consentService.refreshServerAuthority(uid: user.uid);
    if (!mounted) return;
    if (!hasConsent) {
      if (preferences.isCurrentAiConsentDeferred && !forceConsentPrompt) {
        setState(() => _consentDeferred = true);
        return;
      }
      final accepted = await AiConsentSheet.show(
        context,
        onAccept: () async => (await consentService.grantCurrentConsent(uid: user.uid)) != null,
        onDecline: () => consentService.declineCurrentConsent(uid: user.uid),
      );
      if (!mounted) return;
      hasConsent = accepted == true && preferences.aiConsentAccepted;
      if (!hasConsent) {
        setState(() => _consentDeferred = true);
        return;
      }
    }
    setState(() => _consentDeferred = false);

    String timezone;
    try {
      timezone = await FlutterTimezone.getLocalTimezone();
    } catch (_) {
      timezone = DateTime.now().timeZoneName;
    }
    if (!mounted) return;

    await context.read<EllaProvisioningProvider>().start(
          uid: user.uid,
          requestContext: EllaProvisioningRequestContext(
            appVersion: PlatformManager.instance.appVersion,
            locale: Localizations.localeOf(context).toLanguageTag(),
            timezone: timezone,
            consentReceiptId: preferences.hasAccountBoundAiConsent(user.uid) ? preferences.aiConsentReceiptId : '',
          ),
        );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (!mounted) return;
    context.read<EllaProvisioningProvider>().setForeground(state == AppLifecycleState.resumed);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  Future<void> _signOut() async {
    if (widget.onSignOutOverride != null) {
      widget.onSignOutOverride!();
      return;
    }
    context.read<EllaProvisioningProvider>().reset();
    await signOutAndClearUserData(context);
  }

  @override
  Widget build(BuildContext context) {
    if (_consentDeferred) {
      return Scaffold(
        backgroundColor: EllaColors.paper,
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(EllaSizes.screenPadding),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 480),
                child: Container(
                  padding: const EdgeInsets.all(28),
                  decoration: BoxDecoration(
                    color: EllaColors.card,
                    borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.lock_outline_rounded, color: EllaColors.tealDeep, size: 44),
                      const SizedBox(height: 20),
                      Text(
                        context.l10n.aiConsentOffGateTitle,
                        style: EllaTextStyles.display,
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 12),
                      Text(
                        context.l10n.aiConsentOffGateBody,
                        style: EllaTextStyles.secondary,
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 24),
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton(
                          onPressed: () => _start(forceConsentPrompt: true),
                          child: Text(context.l10n.aiConsentReviewAction),
                        ),
                      ),
                      TextButton(onPressed: _signOut, child: Text(context.l10n.signOut)),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    }

    return Consumer<EllaProvisioningProvider>(
      builder: (context, provider, _) {
        if (provider.isOperational) {
          return widget.readyChild;
        }

        final isWorking = provider.state == EllaProvisioningState.idle ||
            provider.state == EllaProvisioningState.checking ||
            provider.state == EllaProvisioningState.queued ||
            provider.state == EllaProvisioningState.provisioning;
        final isBlocked = provider.state == EllaProvisioningState.blocked;
        final canRetry =
            provider.state == EllaProvisioningState.degraded || (isBlocked && (provider.receipt?.retryable ?? false));
        final timedOut = provider.errorCode == 'provisioning_timeout';
        final updateRequired = provider.errorCode == 'upgrade_required';

        final title = switch (provider.state) {
          EllaProvisioningState.idle || EllaProvisioningState.checking => context.l10n.settingUp,
          EllaProvisioningState.queued || EllaProvisioningState.provisioning => context.l10n.settingUp,
          EllaProvisioningState.degraded when timedOut => context.l10n.ellaProvisioningTimeoutTitle,
          EllaProvisioningState.degraded => context.l10n.ellaProvisioningConnectionTitle,
          EllaProvisioningState.blocked when updateRequired => context.l10n.ellaClientUpdateRequiredTitle,
          EllaProvisioningState.blocked => context.l10n.ellaProvisioningPausedTitle,
          EllaProvisioningState.ready => context.l10n.settingUp,
        };
        final body = switch (provider.state) {
          EllaProvisioningState.idle || EllaProvisioningState.checking => context.l10n.pleaseWait,
          EllaProvisioningState.queued || EllaProvisioningState.provisioning => context.l10n.pleaseWait,
          EllaProvisioningState.degraded when timedOut => context.l10n.ellaProvisioningTimeoutBody,
          EllaProvisioningState.degraded => context.l10n.ellaProvisioningConnectionBody,
          EllaProvisioningState.blocked when updateRequired => context.l10n.ellaClientUpdateRequiredBody,
          EllaProvisioningState.blocked => context.l10n.ellaProvisioningPausedBody,
          EllaProvisioningState.ready => context.l10n.pleaseWait,
        };

        return Scaffold(
          backgroundColor: EllaColors.paper,
          body: SafeArea(
            child: Center(
              child: SingleChildScrollView(
                padding: const EdgeInsets.all(EllaSizes.screenPadding),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 480),
                  child: Container(
                    padding: const EdgeInsets.all(28),
                    decoration: BoxDecoration(
                      color: EllaColors.card,
                      borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Container(
                          width: 72,
                          height: 72,
                          decoration: const BoxDecoration(color: EllaColors.cardDeep, shape: BoxShape.circle),
                          child: isWorking
                              ? const Padding(
                                  padding: EdgeInsets.all(22),
                                  child: CircularProgressIndicator(color: EllaColors.tealDeep, strokeWidth: 3),
                                )
                              : Icon(
                                  isBlocked ? Icons.lock_outline_rounded : Icons.cloud_off_outlined,
                                  color: isBlocked ? EllaColors.tealDeep : EllaColors.warning,
                                  size: 32,
                                ),
                        ),
                        const SizedBox(height: 24),
                        Text(title, style: EllaTextStyles.display, textAlign: TextAlign.center),
                        const SizedBox(height: 12),
                        Text(body, style: EllaTextStyles.secondary, textAlign: TextAlign.center),
                        if (provider.supportCode.isNotEmpty) ...[
                          const SizedBox(height: 20),
                          Semantics(
                            button: true,
                            label: context.l10n.copy,
                            child: InkWell(
                              borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                              onTap: () async {
                                await Clipboard.setData(ClipboardData(text: provider.supportCode));
                                if (!context.mounted) return;
                                ScaffoldMessenger.of(
                                  context,
                                ).showSnackBar(SnackBar(content: Text(context.l10n.copied)));
                              },
                              child: Container(
                                width: double.infinity,
                                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
                                decoration: BoxDecoration(
                                  color: EllaColors.paper,
                                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                                ),
                                child: Text(
                                  provider.supportCode,
                                  style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w600),
                                  textAlign: TextAlign.center,
                                ),
                              ),
                            ),
                          ),
                        ],
                        if (canRetry) ...[
                          const SizedBox(height: 24),
                          SizedBox(
                            width: double.infinity,
                            child: FilledButton(
                              onPressed: provider.retry,
                              style: FilledButton.styleFrom(
                                backgroundColor: EllaColors.tealDeep,
                                foregroundColor: EllaColors.paper,
                                minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                              ),
                              child: Text(context.l10n.retry),
                            ),
                          ),
                        ],
                        const SizedBox(height: 12),
                        TextButton(
                          onPressed: _signOut,
                          child: Text(
                            context.l10n.ellaSettingsSignOut,
                            style: const TextStyle(color: EllaColors.inkSoft),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}
