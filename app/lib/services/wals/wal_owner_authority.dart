import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/wal.dart';

abstract interface class ExactAccountAuthorityVerifier {
  String get uid;
  bool isExactCurrent();
}

class ExactAccountAuthorityChangedException extends StateError {
  ExactAccountAuthorityChangedException(super.message);
}

abstract interface class AccountCommitAuthority implements ExactAccountAuthorityVerifier {
  bool isCurrent();
}

class ActiveWalAuthority implements AccountCommitAuthority {
  const ActiveWalAuthority({required this.owner, required this.consent, this.currentCheck});

  final WalOwner owner;
  final AiConsentAuthoritySnapshot consent;
  final bool Function()? currentCheck;

  @override
  String get uid => owner.uid;

  @override
  bool isCurrent({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    if (currentCheck != null) return currentCheck!();
    final prefs = preferences ?? SharedPreferencesUtil();
    final currentUid = authenticatedUid ?? WalOwnerAuthority.authenticatedUid;
    final currentOwner = WalOwnerAuthority.currentOwner(preferences: prefs, authenticatedUid: currentUid);
    return currentOwner != null && owner.matches(currentOwner) && consent.isCurrent(preferences: prefs);
  }

  @override
  bool isExactCurrent() => isCurrent();
}

class AccountGenerationAuthority implements AccountCommitAuthority {
  const AccountGenerationAuthority({
    required this.preferences,
    required this.uid,
    required this.generation,
  });

  final SharedPreferencesUtil preferences;
  @override
  final String uid;
  final int generation;

  @override
  bool isCurrent() =>
      !SharedPreferencesUtil.isPublicBuild &&
      preferences.uid == uid &&
      preferences.aiConsentAuthorityGeneration == generation;

  @override
  bool isExactCurrent() => isCurrent();
}

class AccountOperationEntryAuthority implements AccountCommitAuthority {
  const AccountOperationEntryAuthority({
    required this.preferences,
    required this.uid,
    required this.authenticatedUid,
    required this.verifiedPersonaId,
    required this.profileBindingId,
    required this.consentReceiptId,
    required this.consentReceiptUid,
    required this.consentAccepted,
    required this.consentContractVersion,
    required this.consentProcessorSetHash,
    required this.consentScopeVersion,
    required this.consentScopeHash,
    required this.generation,
    required this.provisioningState,
    required this.bindingState,
    required this.bindingRevision,
    required this.policyRevision,
  });

  final SharedPreferencesUtil preferences;
  @override
  final String uid;
  final String authenticatedUid;
  final String? verifiedPersonaId;
  final String profileBindingId;
  final String consentReceiptId;
  final String consentReceiptUid;
  final bool consentAccepted;
  final String consentContractVersion;
  final String consentProcessorSetHash;
  final String consentScopeVersion;
  final String consentScopeHash;
  final int generation;
  final String provisioningState;
  final String bindingState;
  final int bindingRevision;
  final String policyRevision;

  @override
  bool isCurrent() {
    final receipt = preferences.getEllaProvisioningReceipt(uid);
    return uid.isNotEmpty &&
        authenticatedUid.isNotEmpty &&
        WalOwnerAuthority.authenticatedUid == authenticatedUid &&
        preferences.uid == uid &&
        preferences.verifiedPersonaId?.trim() == verifiedPersonaId &&
        preferences.aiConsentProfileBindingId == profileBindingId &&
        preferences.aiConsentReceiptId == consentReceiptId &&
        preferences.aiConsentReceiptUid == consentReceiptUid &&
        preferences.aiConsentAccepted == consentAccepted &&
        preferences.aiConsentContractVersion == consentContractVersion &&
        preferences.aiConsentProcessorSetHash == consentProcessorSetHash &&
        preferences.aiConsentScopeVersion == consentScopeVersion &&
        preferences.aiConsentScopeHash == consentScopeHash &&
        preferences.aiConsentAuthorityGeneration == generation &&
        (receipt?['state']?.toString() ?? '') == provisioningState &&
        (receipt?['binding_state']?.toString() ?? '') == bindingState &&
        (receipt?['binding_revision'] as int? ?? 0) == bindingRevision &&
        (receipt?['effective_policy_revision']?.toString() ?? '') == policyRevision;
  }

  @override
  bool isExactCurrent() => isCurrent();
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

  static AccountCommitAuthority? operationEntry({SharedPreferencesUtil? preferences, String? authenticatedUid}) {
    final prefs = preferences ?? SharedPreferencesUtil();
    final currentAuthenticatedUid = authenticatedUid ?? WalOwnerAuthority.authenticatedUid;
    final operational = active(preferences: prefs, authenticatedUid: currentAuthenticatedUid);
    if (operational != null) return operational;
    if (currentAuthenticatedUid.isEmpty || prefs.uid != currentAuthenticatedUid) {
      if (SharedPreferencesUtil.isPublicBuild) return null;
      return AccountGenerationAuthority(
        preferences: prefs,
        uid: prefs.uid,
        generation: prefs.aiConsentAuthorityGeneration,
      );
    }
    final receipt = prefs.getEllaProvisioningReceipt(currentAuthenticatedUid);
    return AccountOperationEntryAuthority(
      preferences: prefs,
      uid: currentAuthenticatedUid,
      authenticatedUid: currentAuthenticatedUid,
      verifiedPersonaId: prefs.verifiedPersonaId?.trim(),
      profileBindingId: prefs.aiConsentProfileBindingId,
      consentReceiptId: prefs.aiConsentReceiptId,
      consentReceiptUid: prefs.aiConsentReceiptUid,
      consentAccepted: prefs.aiConsentAccepted,
      consentContractVersion: prefs.aiConsentContractVersion,
      consentProcessorSetHash: prefs.aiConsentProcessorSetHash,
      consentScopeVersion: prefs.aiConsentScopeVersion,
      consentScopeHash: prefs.aiConsentScopeHash,
      generation: prefs.aiConsentAuthorityGeneration,
      provisioningState: receipt?['state']?.toString() ?? '',
      bindingState: receipt?['binding_state']?.toString() ?? '',
      bindingRevision: receipt?['binding_revision'] as int? ?? 0,
      policyRevision: receipt?['effective_policy_revision']?.toString() ?? '',
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
