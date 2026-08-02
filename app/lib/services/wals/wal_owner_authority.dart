import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/wal.dart';

class ActiveWalAuthority {
  const ActiveWalAuthority({required this.owner, required this.consent, this.currentCheck});

  final WalOwner owner;
  final AiConsentAuthoritySnapshot consent;
  final bool Function()? currentCheck;

  bool isCurrent({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    if (currentCheck != null) return currentCheck!();
    final prefs = preferences ?? SharedPreferencesUtil();
    final currentUid = authenticatedUid ?? WalOwnerAuthority.authenticatedUid;
    final currentOwner = WalOwnerAuthority.currentOwner(preferences: prefs, authenticatedUid: currentUid);
    return currentOwner != null && owner.matches(currentOwner) && consent.isCurrent(preferences: prefs);
  }
}

class WalOwnerAuthority {
  const WalOwnerAuthority._();

  static WalOwner? currentOwner({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    final prefs = preferences ?? SharedPreferencesUtil();
    final firebaseUid = authenticatedUid ?? WalOwnerAuthority.authenticatedUid;
    if (firebaseUid.isEmpty || prefs.uid != firebaseUid) return null;

    final profileBindingId = prefs.aiConsentProfileBindingId;
    final consentReceiptId = prefs.aiConsentReceiptId;
    final receipt = prefs.getEllaProvisioningReceipt(firebaseUid);
    final bindingRevision = receipt?['binding_revision'];
    if (profileBindingId.isEmpty || consentReceiptId.isEmpty || bindingRevision is! int || bindingRevision <= 0) {
      return null;
    }

    return WalOwner(
      uid: firebaseUid,
      profileBindingId: profileBindingId,
      bindingRevision: bindingRevision,
      consentReceiptId: consentReceiptId,
      authorityGenerationAtCapture: prefs.aiConsentAuthorityGeneration,
    );
  }

  static ActiveWalAuthority? active({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    final prefs = preferences ?? SharedPreferencesUtil();
    final owner = currentOwner(preferences: prefs, authenticatedUid: authenticatedUid);
    if (owner == null) return null;
    final consent = AiConsentAuthoritySnapshot.capture(preferences: prefs, expectedUid: owner.uid);
    if (consent == null) return null;
    return ActiveWalAuthority(owner: owner, consent: consent);
  }

  static String get authenticatedUid {
    try {
      return FirebaseAuth.instance.currentUser?.uid ?? '';
    } catch (_) {
      return '';
    }
  }
}
