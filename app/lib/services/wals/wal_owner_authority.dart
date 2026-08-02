import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/wal.dart';

abstract interface class AccountCommitAuthority {
  bool isCurrent();
}

class ActiveWalAuthority implements AccountCommitAuthority {
  const ActiveWalAuthority({required this.owner, required this.consent, this.currentCheck});

  final WalOwner owner;
  final AiConsentAuthoritySnapshot consent;
  final bool Function()? currentCheck;

  @override
  bool isCurrent({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    if (currentCheck != null) return currentCheck!();
    final prefs = preferences ?? SharedPreferencesUtil();
    final currentUid = authenticatedUid ?? WalOwnerAuthority.authenticatedUid;
    final currentOwner = WalOwnerAuthority.currentOwner(preferences: prefs, authenticatedUid: currentUid);
    return currentOwner != null && owner.matches(currentOwner) && consent.isCurrent(preferences: prefs);
  }
}

class AccountGenerationAuthority implements AccountCommitAuthority {
  const AccountGenerationAuthority({
    required this.preferences,
    required this.uid,
    required this.generation,
  });

  final SharedPreferencesUtil preferences;
  final String uid;
  final int generation;

  @override
  bool isCurrent() =>
      !SharedPreferencesUtil.isPublicBuild &&
      preferences.uid == uid &&
      preferences.aiConsentAuthorityGeneration == generation;
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
    if (!prefs.hasCurrentEllaProvisioningAuthority(uid: firebaseUid, bindingRevision: bindingRevision)) return null;

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

  static AccountCommitAuthority? activeAccount({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    final prefs = preferences ?? SharedPreferencesUtil();
    final operational = active(preferences: prefs, authenticatedUid: authenticatedUid);
    if (operational != null) return operational;
    if (SharedPreferencesUtil.isPublicBuild) return null;
    return AccountGenerationAuthority(
      preferences: prefs,
      uid: prefs.uid,
      generation: prefs.aiConsentAuthorityGeneration,
    );
  }

  static String get authenticatedUid {
    try {
      return FirebaseAuth.instance.currentUser?.uid ?? '';
    } catch (_) {
      return '';
    }
  }
}
