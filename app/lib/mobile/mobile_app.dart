import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/onboarding/device_selection.dart';
import 'package:omi/pages/onboarding/ella/ella_onboarding.dart';
import 'package:omi/pages/onboarding/wrapper.dart';
import 'package:omi/pages/persona/persona_profile.dart';
import 'package:omi/providers/auth_provider.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/logger.dart';

class MobileApp extends StatefulWidget {
  const MobileApp({super.key});

  @override
  State<MobileApp> createState() => _MobileAppState();
}

class _MobileAppState extends State<MobileApp> {
  // TODO: replace with flavor check
  static const bool _isEllaApp = true;

  /// True while attempting to restore onboarding state from server.
  bool _restoringOnboarding = false;
  bool _attemptedOnboardingRestore = false;

  @override
  Widget build(BuildContext context) {
    const debugAutoCall = bool.fromEnvironment('DEBUG_AUTO_CALL');

    // Debug mode: bypass auth and go straight to home
    if (debugAutoCall && !isHermesProvisioningGateEnabled) {
      return const HomePageWrapper();
    }

    // Ella app: use simplified onboarding that handles auth internally
    if (_isEllaApp) {
      return Consumer<AuthenticationProvider>(
        builder: (context, authProvider, child) {
          if (authProvider.isSignedIn() && SharedPreferencesUtil().onboardingCompleted) {
            return isHermesProvisioningGateEnabled ? const EllaProvisioningGatePage() : const HomePageWrapper();
          }

          // Self-healing: if signed in but onboardingCompleted is false,
          // try restoring from server before showing onboarding.
          // This handles SharedPreferences data loss after iOS app updates.
          // Also pre-set language flag to prevent language dialog from
          // appearing during the restore (issue #633).
          if (authProvider.isSignedIn() && !SharedPreferencesUtil().onboardingCompleted) {
            if (!_restoringOnboarding && !_attemptedOnboardingRestore) {
              _restoringOnboarding = true;
              // Prevent language dialog from firing during restore
              if (!SharedPreferencesUtil().hasSetPrimaryLanguage) {
                SharedPreferencesUtil().hasSetPrimaryLanguage = true;
                SharedPreferencesUtil().userPrimaryLanguage = 'en';
              }
              AuthService.instance.restoreOnboardingState().catchError((e) {
                Logger.debug('MobileApp: failed to restore onboarding state: $e');
              }).whenComplete(() {
                if (!mounted) return;
                setState(() {
                  _restoringOnboarding = false;
                  _attemptedOnboardingRestore = true;
                });
              });
            }
            // Show loading while checking server — avoids flashing onboarding
            if (_restoringOnboarding) {
              return const Scaffold(
                body: Center(child: CircularProgressIndicator()),
              );
            }
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
