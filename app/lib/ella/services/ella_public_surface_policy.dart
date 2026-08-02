import 'package:omi/backend/preferences.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';

const bool isEllaAccessDemoGalleryConfigured = bool.fromEnvironment('ELLA_ACCESS_DEMO_GALLERY');
const bool isEllaDebugAutoCallConfigured = bool.fromEnvironment('DEBUG_AUTO_CALL');
const bool isEllaGuardianConfigured = bool.fromEnvironment('ELLA_GUARDIAN_ENABLED', defaultValue: false);

bool allowsInheritedOmiSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Safety-sensitive Ella features stay hidden in public builds until their
/// authenticated end-to-end delivery paths have separate release receipts.
bool allowsUnverifiedEllaSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Guardian and related care surfaces are unavailable to public and invitation
/// builds until every route is authenticated and account-isolated end to end.
/// The capability remains off when its build flag is absent or misconfigured.
bool allowsGuardianSurface({
  bool isPublicBuild = SharedPreferencesUtil.isPublicBuild,
  bool isInvitationBuild = isEllaInternalPilotEnabled,
  bool guardianConfigured = isEllaGuardianConfigured,
}) =>
    guardianConfigured && !isPublicBuild && !isInvitationBuild;

/// Notification and alternate Home navigation is intentionally narrower than
/// the app's internal route set. Public builds only accept exact routes for the
/// four visible tabs and the public data/privacy settings page.
String? allowedEllaNavigationRoute(
  String? route, {
  bool isPublicBuild = SharedPreferencesUtil.isPublicBuild,
  bool isInvitationBuild = isEllaInternalPilotEnabled,
  bool guardianConfigured = isEllaGuardianConfigured,
}) {
  if (route == null || route.trim().isEmpty) return null;

  final uri = Uri.tryParse(route.trim());
  if (uri == null || uri.hasScheme || uri.hasAuthority || uri.hasQuery || uri.hasFragment) return null;
  final segments = uri.pathSegments.where((segment) => segment.isNotEmpty).toList(growable: false);
  if (segments.isEmpty) return '/';

  final careAliases = {'guardian', 'guardian-alerts', 'whisper', 'whispers', 'caregiver', 'caregivers', 'emergency'};
  final isCareAlias = segments.map((segment) => segment.toLowerCase()).any(careAliases.contains);
  if (isCareAlias &&
      !allowsGuardianSurface(
        isPublicBuild: isPublicBuild,
        isInvitationBuild: isInvitationBuild,
        guardianConfigured: guardianConfigured,
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
    _ => null,
  };
}
