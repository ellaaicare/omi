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
