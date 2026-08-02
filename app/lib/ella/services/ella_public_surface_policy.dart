import 'package:omi/backend/preferences.dart';

bool allowsInheritedOmiSurface({bool isPublicBuild = SharedPreferencesUtil.isPublicBuild}) => !isPublicBuild;
