import 'package:omi/backend/preferences.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';

const bool isEllaAccessDemoGalleryConfigured = bool.fromEnvironment('ELLA_ACCESS_DEMO_GALLERY');
const bool isEllaDebugAutoCallConfigured = bool.fromEnvironment('DEBUG_AUTO_CALL');
const bool isEllaGuardianConfigured = bool.fromEnvironment('ELLA_GUARDIAN_ENABLED', defaultValue: false);

bool allowsInheritedOmiSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Safety-sensitive Ella features stay hidden in public builds until their
/// authenticated end-to-end delivery paths have separate release receipts.
bool allowsUnverifiedEllaSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

bool get hasAuthenticatedGuardianIdentity {
  final persistedUid = SharedPreferencesUtil().uid.trim();
  final authenticatedUid = WalOwnerAuthority.authenticatedUid.trim();
  return persistedUid.isNotEmpty && authenticatedUid == persistedUid;
}

/// Authenticated Whispers are available to invitation users while remaining
/// fail-closed when the build capability or exact Firebase identity is absent.
bool allowsGuardianSurface({
  bool isPublicBuild = SharedPreferencesUtil.isPublicBuild,
  bool isInvitationBuild = SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled,
  bool guardianConfigured = isEllaGuardianConfigured,
  bool? guardianAuthenticated,
}) {
  final hasExactIdentity = guardianAuthenticated ?? hasAuthenticatedGuardianIdentity;
  return guardianConfigured && hasExactIdentity && (!isPublicBuild || isInvitationBuild);
}

/// Caregiver, emergency, and broad Guardian policy controls remain internal.
/// The invitation capability is intentionally limited to authenticated audio
/// Whispers until those separate delivery contracts are proven.
bool allowsGuardianCareSurface({
  bool isPublicBuild = SharedPreferencesUtil.isPublicBuild,
  bool isInvitationBuild = SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled,
  bool guardianConfigured = isEllaGuardianConfigured,
  bool? guardianAuthenticated,
}) =>
    allowsGuardianSurface(
      isPublicBuild: isPublicBuild,
      isInvitationBuild: isInvitationBuild,
      guardianConfigured: guardianConfigured,
      guardianAuthenticated: guardianAuthenticated,
    ) &&
    !isPublicBuild &&
    !isInvitationBuild;

/// Notification and alternate Home navigation is intentionally narrower than
/// the app's internal route set. Public builds only accept exact routes for the
/// four visible tabs and the public data/privacy settings page.
String? allowedEllaNavigationRoute(
  String? route, {
  bool isPublicBuild = SharedPreferencesUtil.isPublicBuild,
  bool isInvitationBuild = SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled,
  bool guardianConfigured = isEllaGuardianConfigured,
  bool? guardianAuthenticated,
}) {
  if (route == null || route.trim().isEmpty) return null;

  final uri = Uri.tryParse(route.trim());
  if (uri == null || uri.hasScheme || uri.hasAuthority || uri.hasQuery || uri.hasFragment) return null;
  final segments = uri.pathSegments.where((segment) => segment.isNotEmpty).toList(growable: false);
  if (segments.isEmpty) return '/';

  final whisperAliases = {'guardian-alerts', 'whisper', 'whispers'};
  final careAliases = {'guardian', 'caregiver', 'caregivers', 'emergency'};
  final normalizedSegments = segments.map((segment) => segment.toLowerCase());
  final isWhisperAlias = normalizedSegments.any(whisperAliases.contains);
  final isCareAlias = normalizedSegments.any(careAliases.contains);
  if (isWhisperAlias &&
      !allowsGuardianSurface(
        isPublicBuild: isPublicBuild,
        isInvitationBuild: isInvitationBuild,
        guardianConfigured: guardianConfigured,
        guardianAuthenticated: guardianAuthenticated,
      )) {
    return null;
  }
  if (isCareAlias &&
      !allowsGuardianCareSurface(
        isPublicBuild: isPublicBuild,
        isInvitationBuild: isInvitationBuild,
        guardianConfigured: guardianConfigured,
        guardianAuthenticated: guardianAuthenticated,
      )) {
    return null;
  }
  if (!isPublicBuild) return route;

  return switch (segments) {
    ['home'] => '/home',
    ['today'] => '/today',
    ['chat'] => '/chat',
    ['voice'] => '/voice',
    ['settings'] => '/settings',
    ['settings', 'data-privacy'] => '/settings/data-privacy',
    ['guardian-alerts'] when isWhisperAlias => '/guardian-alerts',
    ['whispers'] when isWhisperAlias => '/whispers',
    _ => null,
  };
}
