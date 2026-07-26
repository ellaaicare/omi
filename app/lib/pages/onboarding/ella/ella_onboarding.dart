import 'dart:async';

import 'package:flutter/material.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/onboarding/auth.dart';
import 'package:omi/pages/onboarding/ella/ella_connect.dart';
import 'package:omi/pages/onboarding/ella/ella_emergency.dart';
import 'package:omi/pages/onboarding/ella/ella_welcome.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class EllaOnboarding extends StatefulWidget {
  const EllaOnboarding({super.key});

  @visibleForTesting
  static bool shouldPresentVoiceConsent({
    required bool hasCurrentConsent,
    required bool hasPriorAccountConsent,
    required bool deferredCurrentConsent,
  }) =>
      !hasCurrentConsent && !deferredCurrentConsent;

  @visibleForTesting
  static bool shouldStartProvisioning({required bool hasCurrentConsent}) => hasCurrentConsent;

  @override
  State<EllaOnboarding> createState() => _EllaOnboardingState();
}

class _EllaOnboardingState extends State<EllaOnboarding> {
  final PageController _pageController = PageController();
  int _currentPage = 0;
  bool _isSignedIn = false;
  bool _isCompletingOnboarding = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (AuthService.instance.isSignedIn()) {
        if (SharedPreferencesUtil().onboardingCompleted) {
          _routeToAuthenticatedHome();
        } else {
          _onSignedIn();
        }
      }
    });
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  Future<void> _onSignedIn() async {
    setState(() => _isSignedIn = true);
  }

  Future<void> _startHermesProvisioning() async {
    final user = FirebaseAuth.instance.currentUser;
    if (user == null || !mounted) return;

    String timezone;
    try {
      timezone = await FlutterTimezone.getLocalTimezone();
    } catch (_) {
      timezone = DateTime.now().timeZoneName;
    }
    if (!mounted) return;

    final preferences = SharedPreferencesUtil();
    await context.read<EllaProvisioningProvider>().start(
          uid: user.uid,
          requestContext: EllaProvisioningRequestContext(
            appVersion: PlatformManager.instance.appVersion,
            locale: Localizations.localeOf(context).toLanguageTag(),
            timezone: timezone,
            consentReceiptId: preferences.hasAccountBoundAiConsent(user.uid) ? preferences.aiConsentReceiptId : '',
          ),
        );
  }

  void _goToPage(int page) {
    _pageController.animateToPage(page, duration: const Duration(milliseconds: 300), curve: Curves.easeInOut);
    setState(() => _currentPage = page);
  }

  Future<void> _completeOnboarding() async {
    if (_isCompletingOnboarding) return;
    setState(() => _isCompletingOnboarding = true);

    final preferences = SharedPreferencesUtil();
    final uid = FirebaseAuth.instance.currentUser?.uid ?? '';
    if (isHermesProvisioningGateEnabled && uid.isNotEmpty) {
      await preferences.prepareEllaProvisioningAccount(uid);
    }
    if (!mounted) return;
    final shouldPresentVoiceConsent = EllaOnboarding.shouldPresentVoiceConsent(
      hasCurrentConsent:
          isHermesProvisioningGateEnabled ? preferences.hasAccountBoundAiConsent(uid) : preferences.aiConsentAccepted,
      hasPriorAccountConsent: uid.isNotEmpty && preferences.hasPriorAccountBoundAiConsent(uid),
      deferredCurrentConsent: preferences.isCurrentAiConsentDeferred,
    );
    if (shouldPresentVoiceConsent && mounted) {
      await AiConsentSheet.show(
        context,
        onAccept: isHermesProvisioningGateEnabled
            ? () async {
                final receiptId = await EllaAiConsentService().acknowledgePrivateCloudSync(uid: uid);
                if (receiptId == null) return false;
                if (mounted) context.read<EllaProvisioningProvider>().setConsentReceiptId(receiptId);
                return true;
              }
            : null,
      );
    }
    if (!mounted) return;

    SharedPreferencesUtil().onboardingCompleted = true;
    if (AuthService.instance.isSignedIn()) {
      updateUserOnboardingState(completed: true);
      final hasCurrentConsent =
          isHermesProvisioningGateEnabled ? preferences.hasAccountBoundAiConsent(uid) : preferences.aiConsentAccepted;
      if (isHermesProvisioningGateEnabled &&
          EllaOnboarding.shouldStartProvisioning(hasCurrentConsent: hasCurrentConsent)) {
        unawaited(_startHermesProvisioning());
      }
    }
    _routeToAuthenticatedHome();
  }

  void _routeToAuthenticatedHome() {
    routeToPage(context, const HomePageWrapper(), replace: true);
  }

  @override
  Widget build(BuildContext context) {
    final publicMode = SharedPreferencesUtil().publicMode;
    if (!_isSignedIn) {
      return Scaffold(
        backgroundColor: EllaColors.bgPrimary,
        body: AuthComponent(
          onSignIn: () {
            if (SharedPreferencesUtil().onboardingCompleted) {
              _routeToAuthenticatedHome();
            } else {
              _onSignedIn();
            }
          },
        ),
      );
    }

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: Stack(
        children: [
          PageView(
            controller: _pageController,
            physics: const NeverScrollableScrollPhysics(),
            onPageChanged: (page) => setState(() => _currentPage = page),
            children: [
              EllaWelcome(
                onNext: () => _goToPage(1),
                onSignOut: () => setState(() {
                  _isSignedIn = false;
                }),
              ),
              EllaConnect(
                onNext: publicMode ? _completeOnboarding : () => _goToPage(2),
                onSkip: publicMode ? _completeOnboarding : () => _goToPage(2),
                onBack: () => _goToPage(0),
              ),
              if (!publicMode)
                EllaEmergency(onComplete: _completeOnboarding, onSkip: _completeOnboarding, onBack: () => _goToPage(1)),
            ],
          ),
          Positioned(
            bottom: MediaQuery.of(context).padding.bottom + 8,
            left: 0,
            right: 0,
            child: EllaProgressDots(currentPage: _currentPage, totalPages: publicMode ? 2 : 3),
          ),
        ],
      ),
    );
  }
}

class EllaProgressDots extends StatelessWidget {
  final int currentPage;
  final int totalPages;

  const EllaProgressDots({super.key, required this.currentPage, required this.totalPages});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(totalPages, (index) {
        final isActive = index == currentPage;
        return Container(
          margin: const EdgeInsets.symmetric(horizontal: 4),
          width: isActive ? 12 : 8,
          height: isActive ? 12 : 8,
          decoration: BoxDecoration(
            color: isActive ? EllaColors.primary : EllaColors.textDisabled,
            shape: BoxShape.circle,
          ),
        );
      }),
    );
  }
}
