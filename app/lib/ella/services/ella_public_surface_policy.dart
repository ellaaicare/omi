import 'package:omi/backend/preferences.dart';

bool allowsInheritedOmiSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;

/// Safety-sensitive Ella features stay hidden in public builds until their
/// authenticated end-to-end delivery paths have separate release receipts.
bool allowsUnverifiedEllaSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;
