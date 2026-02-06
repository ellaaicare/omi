import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/onboarding/device_selection.dart';
import 'package:omi/pages/onboarding/ella/ella_onboarding.dart';
import 'package:omi/pages/onboarding/wrapper.dart';
import 'package:omi/pages/persona/persona_profile.dart';
import 'package:omi/providers/auth_provider.dart';

class MobileApp extends StatelessWidget {
  const MobileApp({super.key});

  // TODO: replace with flavor check
  static const bool _isEllaApp = true;

  @override
  Widget build(BuildContext context) {
    // Ella app: use simplified onboarding that handles auth internally
    if (_isEllaApp) {
      return Consumer<AuthenticationProvider>(
        builder: (context, authProvider, child) {
          if (authProvider.isSignedIn() && SharedPreferencesUtil().onboardingCompleted) {
            return const HomePageWrapper();
          }
          return const EllaOnboarding();
        },
      );
    }

    return Consumer<AuthenticationProvider>(
      builder: (context, authProvider, child) {
        if (authProvider.isSignedIn()) {
          if (SharedPreferencesUtil().onboardingCompleted) {
            return const HomePageWrapper();
          } else {
            return const OnboardingWrapper();
          }
        } else if (SharedPreferencesUtil().hasOmiDevice == false &&
            SharedPreferencesUtil().hasPersonaCreated &&
            SharedPreferencesUtil().verifiedPersonaId != null) {
          return const PersonaProfilePage();
        } else {
          return const DeviceSelectionPage();
        }
      },
    );
  }
}
