import 'package:omi/backend/preferences.dart';

const bool isEllaAccessDemoGalleryConfigured = bool.fromEnvironment('ELLA_ACCESS_DEMO_GALLERY');
const bool isEllaDebugAutoCallConfigured = bool.fromEnvironment('DEBUG_AUTO_CALL');

bool allowsInheritedOmiSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Safety-sensitive Ella features stay hidden in public builds until their
/// authenticated end-to-end delivery paths have separate release receipts.
bool allowsUnverifiedEllaSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Notification and alternate Home navigation is intentionally narrower than
/// the app's internal route set. Public builds only accept exact routes for the
/// four visible tabs and the public data/privacy settings page.
String? allowedEllaNavigationRoute(String? route, {bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) {
  if (route == null || route.trim().isEmpty) return null;
  if (!isPublicBuild) return route;

  final uri = Uri.tryParse(route.trim());
  if (uri == null || uri.hasScheme || uri.hasAuthority || uri.hasQuery || uri.hasFragment) return null;
  final segments = uri.pathSegments.where((segment) => segment.isNotEmpty).toList(growable: false);
  if (segments.isEmpty) return '/';

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
