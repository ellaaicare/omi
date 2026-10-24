import 'package:flutter/material.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';

typedef AiConsentPrompt = Future<bool> Function();
typedef AiConsentProtectedAction = Future<void> Function();

class AiConsentActionGate {
  Future<bool>? _activeRequest;

  Future<bool> ensure({
    required bool Function() hasConsent,
    required AiConsentPrompt requestConsent,
  }) async {
    if (hasConsent()) return true;

    final activeRequest = _activeRequest;
    if (activeRequest != null) {
      return await activeRequest && hasConsent();
    }

    late final Future<bool> trackedRequest;
    trackedRequest = requestConsent().whenComplete(() {
      if (identical(_activeRequest, trackedRequest)) {
        _activeRequest = null;
      }
    });
    _activeRequest = trackedRequest;

    return await trackedRequest && hasConsent();
  }

  Future<bool> run({
    required bool Function() hasConsent,
    required AiConsentPrompt requestConsent,
    required AiConsentProtectedAction action,
  }) async {
    if (!await ensure(hasConsent: hasConsent, requestConsent: requestConsent)) return false;
    await action();
    return true;
  }
}

class AiConsentCoordinator {
  static final AiConsentActionGate _gate = AiConsentActionGate();

  /// An explicit review always presents the choices, including for a cached
  /// grant. Reviewing alone never grants consent or starts protected work.
  static Future<bool> review(
    BuildContext context, {
    bool Function()? canReview,
    EllaAiConsentService? service,
    String Function()? authenticatedUid,
  }) async {
    if (isEllaInternalPilotEnabled &&
        !isEllaInternalPilotLocaleSupported(Localizations.localeOf(context).languageCode)) {
      return false;
    }
    final readUid = authenticatedUid ?? () => FirebaseAuth.instance.currentUser?.uid ?? '';
    final uid = readUid();
    final preferences = SharedPreferencesUtil();
    var accountRetired = false;
    void observeAccount() {
      if (readUid() != uid || preferences.uid != uid) accountRetired = true;
    }

    bool accountCurrent() =>
        !accountRetired && context.mounted && uid.isNotEmpty && readUid() == uid && preferences.uid == uid;
    bool callerCurrent() => accountCurrent() && (canReview?.call() ?? true);
    if (!callerCurrent()) return false;
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(observeAccount);
    try {
      final consentService = service ?? EllaAiConsentService();
      await consentService.refreshServerAuthority(uid: uid);
      if (!callerCurrent()) return false;

      // Refresh may legitimately replace receipt metadata. Fence user decisions
      // against the authority that exists after that refresh.
      final decisionGeneration = preferences.aiConsentAuthorityGeneration;
      bool canDecide() => callerCurrent() && preferences.aiConsentAuthorityGeneration == decisionGeneration;
      final accepted = await AiConsentSheet.show(
        context,
        reviewMode: true,
        canPersistDecision: canDecide,
        onAccept: () async {
          if (!canDecide()) {
            return const AiConsentGrantOutcome.failed(AiConsentGrantFailureKind.rejected);
          }
          final outcome = await consentService.grantCurrentConsentWithOutcome(uid: uid);
          if (!callerCurrent()) {
            return const AiConsentGrantOutcome.failed(AiConsentGrantFailureKind.authorityChanged);
          }
          final receiptId = outcome.receiptId;
          if (callerCurrent() && receiptId != null && preferences.aiConsentReceiptId == receiptId) {
            try {
              context.read<EllaProvisioningProvider>().setConsentReceiptId(receiptId);
            } catch (_) {
              // The authenticated receipt is persisted outside this provider tree.
            }
          }
          return outcome;
        },
        // The sheet applies the explicit local revoke before this callback, so
        // its generation/epoch may have retired. canPersistDecision already
        // admitted this exact choice; the account fence and service checks
        // still apply to its server revoke. Caller fencing remains on results.
        onDecline: () async => accountCurrent() && await consentService.revokeCurrentConsent(uid: uid),
      );
      return callerCurrent() && accepted == true && preferences.hasAccountBoundAiConsent(uid);
    } finally {
      SharedPreferencesUtil.aiConsentAuthorityChanges.removeListener(observeAccount);
    }
  }

  static Future<bool> ensure(BuildContext context) async {
    if (isEllaInternalPilotEnabled &&
        !isEllaInternalPilotLocaleSupported(Localizations.localeOf(context).languageCode)) {
      return false;
    }
    final preferences = SharedPreferencesUtil();
    if (preferences.aiConsentAccepted) return true;
    final uid = FirebaseAuth.instance.currentUser?.uid ?? '';
    if (uid.isEmpty) return false;
    if (await EllaAiConsentService().refreshServerAuthority(uid: uid)) return true;

    return _gate.ensure(
      hasConsent: () => preferences.aiConsentAccepted,
      requestConsent: () => _request(context),
    );
  }

  static Future<bool> _request(BuildContext context) async {
    final uid = FirebaseAuth.instance.currentUser?.uid ?? '';
    if (uid.isEmpty) return false;
    final service = EllaAiConsentService();

    final accepted = await AiConsentSheet.show(
      context,
      onAccept: () async {
        final outcome = await service.grantCurrentConsentWithOutcome(uid: uid);
        final receiptId = outcome.receiptId;
        if (receiptId != null && context.mounted) {
          try {
            context.read<EllaProvisioningProvider>().setConsentReceiptId(receiptId);
          } catch (_) {
            // The authenticated receipt is already persisted when this
            // action is rendered outside the provisioning provider tree.
          }
        }
        return outcome;
      },
      onDecline: () => service.declineCurrentConsent(uid: uid),
    );
    return accepted == true && SharedPreferencesUtil().aiConsentAccepted;
  }
}
