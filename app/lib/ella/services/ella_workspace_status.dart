import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';

enum EllaRouteVerification { verified, notVerified }

class EllaWorkspaceStatus {
  const EllaWorkspaceStatus({
    required this.email,
    required this.workspaceVerified,
    required this.workspaceFingerprint,
    required this.bindingRevision,
    required this.lastVerifiedAt,
    required this.chat,
    required this.voice,
    required this.whispers,
    required this.quarantinedAudioCount,
  });

  final String email;
  final bool workspaceVerified;
  final String workspaceFingerprint;
  final int bindingRevision;
  final DateTime? lastVerifiedAt;
  final EllaRouteVerification chat;
  final EllaRouteVerification voice;
  final EllaRouteVerification whispers;
  final int quarantinedAudioCount;

  factory EllaWorkspaceStatus.current({SharedPreferencesUtil? preferences, String? uid, String? email}) {
    final prefs = preferences ?? SharedPreferencesUtil();
    final firebaseUser = uid == null || email == null ? _firebaseUserOrNull() : null;
    final currentUid = uid ?? firebaseUser?.uid ?? '';
    final currentEmail = email ?? firebaseUser?.email ?? prefs.email;
    final rawReceipt = currentUid.isEmpty ? null : prefs.getEllaProvisioningReceipt(currentUid);
    final receipt = rawReceipt == null ? null : EllaProvisioningReceipt.fromJson(rawReceipt);
    final exactSelfHostedProvider =
        receipt?.runtimeProvider == 'self_hosted_hermes' || receipt?.runtimeProvider == 'hermes_self_hosted';
    final verified = currentUid.isNotEmpty &&
        prefs.uid == currentUid &&
        receipt?.isOperational == true &&
        exactSelfHostedProvider &&
        receipt?.runtimeStatus.toLowerCase() == 'ready';
    final profileBindingId = prefs.aiConsentProfileBindingId;
    final fingerprint = verified && profileBindingId.isNotEmpty
        ? _fingerprint(
            currentUid,
            profileBindingId,
            receipt!.bindingRevision,
            receipt.effectivePolicyRevision,
          )
        : '';

    // A generic provisioning receipt does not prove individual route use.
    // These remain unverified until the backend defines signed per-route receipts.
    return EllaWorkspaceStatus(
      email: currentEmail,
      workspaceVerified: verified,
      workspaceFingerprint: fingerprint,
      bindingRevision: verified ? receipt!.bindingRevision : 0,
      lastVerifiedAt: verified ? prefs.getEllaProvisioningVerifiedAt(currentUid) : null,
      chat: EllaRouteVerification.notVerified,
      voice: EllaRouteVerification.notVerified,
      whispers: EllaRouteVerification.notVerified,
      quarantinedAudioCount: prefs.getInt('ellaWalQuarantineCount'),
    );
  }

  static User? _firebaseUserOrNull() {
    try {
      return FirebaseAuth.instance.currentUser;
    } catch (_) {
      return null;
    }
  }

  static String _fingerprint(String uid, String profileBindingId, int bindingRevision, String policyRevision) {
    final digest = sha256.convert(utf8.encode('$uid\n$profileBindingId\n$bindingRevision\n$policyRevision')).toString();
    return '${digest.substring(0, 4).toUpperCase()}-${digest.substring(4, 8).toUpperCase()}-${digest.substring(8, 12).toUpperCase()}';
  }
}
