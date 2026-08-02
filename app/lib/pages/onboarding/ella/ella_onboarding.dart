import 'dart:async';

import 'package:flutter/material.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/onboarding/auth.dart';
import 'package:omi/pages/onboarding/ella/ella_connect.dart';
import 'package:omi/pages/onboarding/ella/ella_emergency.dart';
import 'package:omi/pages/onboarding/ella/ella_welcome.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/utils/platform/platform_manager.dart';

typedef EllaOnboardingAuthBuilder = Widget Function(BuildContext context, VoidCallback onSignIn);

class EllaOnboarding extends StatefulWidget {
  const EllaOnboarding({
    super.key,
    this.pilotLocaleRestricted = isEllaInternalPilotEnabled,
    this.entitlementGateEnabled = isEllaEntitlementGateEnabled,
    this.provisioningGateEnabled = isHermesProvisioningGateEnabled,
    this.authenticatedUidProvider,
    this.isSignedInProvider,
    this.consentServiceFactory,
    this.authBuilder,
  });

  final bool pilotLocaleRestricted;
  final bool entitlementGateEnabled;
  final bool provisioningGateEnabled;
  final String? Function()? authenticatedUidProvider;
  final bool Function()? isSignedInProvider;
  final EllaAiConsentService Function()? consentServiceFactory;
  final EllaOnboardingAuthBuilder? authBuilder;

  @visibleForTesting
  static bool shouldPresentVoiceConsent({
    required bool hasCurrentConsent,
    required bool hasPriorAccountConsent,
    required bool deferredCurrentConsent,
  }) =>
      !hasCurrentConsent && !deferredCurrentConsent;

  @visibleForTesting
  static bool shouldStartProvisioning({required bool hasCurrentConsent}) => hasCurrentConsent;

  @visibleForTesting
  static bool shouldStartProvisioningDirectly({
    required bool provisioningGateEnabled,
    required bool entitlementGateEnabled,
    required bool hasCurrentConsent,
  }) =>
      provisioningGateEnabled && !entitlementGateEnabled && hasCurrentConsent;

  @override
  State<EllaOnboarding> createState() => _EllaOnboardingState();
}

class _EllaOnboardingState extends State<EllaOnboarding> {
  final PageController _pageController = PageController();
  int _currentPage = 0;
  bool _isSignedIn = false;
  bool _isCompletingOnboarding = false;
  bool _signInRefreshInFlight = false;
  String? _pendingPilotSignInUid;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_isAuthenticated) {
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
    final uid = _authenticatedUid;
    if (uid == null || uid.isEmpty) return;
    if (!_isPilotLocaleAllowed) {
      _pendingPilotSignInUid = uid;
      return;
    }
    await _refreshSignedInAuthority(uid);
  }

  Future<void> _resumePendingPilotSignIn() async {
    final uid = _authenticatedUid;
    if (uid == null || uid != _pendingPilotSignInUid || !_isPilotLocaleAllowed) return;
    await _refreshSignedInAuthority(uid);
  }

  Future<void> _refreshSignedInAuthority(String uid) async {
    if (_signInRefreshInFlight || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;
    _signInRefreshInFlight = true;
    final preferences = SharedPreferencesUtil();
    try {
      await const EllaAccountIsolationService().prepareProvisioningAccount(uid, preferences: preferences);
      if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;
      final hasCurrentConsent = await _createConsentService().refreshServerAuthority(uid: uid);
      if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;
      _pendingPilotSignInUid = null;
      if (EllaOnboarding.shouldStartProvisioningDirectly(
        provisioningGateEnabled: widget.provisioningGateEnabled,
        entitlementGateEnabled: widget.entitlementGateEnabled,
        hasCurrentConsent: hasCurrentConsent,
      )) {
        unawaited(_startHermesProvisioning());
      }
    } finally {
      _signInRefreshInFlight = false;
    }
  }

  Future<void> _startHermesProvisioning() async {
    final uid = _authenticatedUid;
    if (uid == null || uid.isEmpty || !mounted || !_isPilotLocaleAllowed) return;

    String timezone;
    try {
      timezone = await FlutterTimezone.getLocalTimezone();
    } catch (_) {
      timezone = DateTime.now().timeZoneName;
    }
    if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;

    final preferences = SharedPreferencesUtil();
    await context.read<EllaProvisioningProvider>().start(
          uid: uid,
          requestContext: EllaProvisioningRequestContext(
            appVersion: PlatformManager.instance.appVersion,
            locale: Localizations.localeOf(context).toLanguageTag(),
            timezone: timezone,
            consentReceiptId: preferences.hasAccountBoundAiConsent(uid) ? preferences.aiConsentReceiptId : '',
          ),
        );
  }

  void _goToPage(int page) {
    _pageController.animateToPage(page, duration: const Duration(milliseconds: 300), curve: Curves.easeInOut);
    setState(() => _currentPage = page);
  }

  Future<void> _completeOnboarding() async {
    if (_isCompletingOnboarding || !_isPilotLocaleAllowed) return;
    setState(() => _isCompletingOnboarding = true);

    final preferences = SharedPreferencesUtil();
    final uid = _authenticatedUid ?? '';
    if (widget.provisioningGateEnabled && uid.isNotEmpty) {
      await const EllaAccountIsolationService().prepareProvisioningAccount(uid, preferences: preferences);
    }
    if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;
    final consentService = _createConsentService();
    if (uid.isNotEmpty && !preferences.aiConsentAccepted) {
      await consentService.refreshServerAuthority(uid: uid);
    }
    if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;
    final shouldPresentVoiceConsent = EllaOnboarding.shouldPresentVoiceConsent(
      hasCurrentConsent: preferences.hasAccountBoundAiConsent(uid),
      hasPriorAccountConsent: uid.isNotEmpty && preferences.hasPriorAccountBoundAiConsent(uid),
      deferredCurrentConsent: preferences.isCurrentAiConsentDeferred,
    );
    if (shouldPresentVoiceConsent && mounted) {
      await AiConsentSheet.show(
        context,
        onAccept: () async {
          final receiptId = await consentService.grantCurrentConsent(uid: uid);
          if (receiptId == null) return false;
          if (mounted) context.read<EllaProvisioningProvider>().setConsentReceiptId(receiptId);
          return true;
        },
        onDecline: () => consentService.declineCurrentConsent(uid: uid),
      );
    }
    if (!mounted || !_isPilotLocaleAllowed || _authenticatedUid != uid) return;

    SharedPreferencesUtil().onboardingCompleted = true;
    if (_isAuthenticated) {
      updateUserOnboardingState(completed: true);
      final hasCurrentConsent = preferences.hasAccountBoundAiConsent(uid);
      if (EllaOnboarding.shouldStartProvisioningDirectly(
        provisioningGateEnabled: widget.provisioningGateEnabled,
        entitlementGateEnabled: widget.entitlementGateEnabled,
        hasCurrentConsent: hasCurrentConsent,
      )) {
        unawaited(_startHermesProvisioning());
      }
    }
    _routeToAuthenticatedHome();
  }

  void _routeToAuthenticatedHome() {
    routeToPage(context, const HomePageWrapper(), replace: true);
  }

  String? get _authenticatedUid => widget.authenticatedUidProvider?.call() ?? FirebaseAuth.instance.currentUser?.uid;

  bool get _isAuthenticated => widget.isSignedInProvider?.call() ?? AuthService.instance.isSignedIn();

  EllaAiConsentService _createConsentService() => widget.consentServiceFactory?.call() ?? EllaAiConsentService();

  bool get _isPilotLocaleAllowed {
    if (!widget.pilotLocaleRestricted) return true;
    final selectedLanguageCode = SharedPreferencesUtil().getString('app_locale');
    if (selectedLanguageCode.isNotEmpty) {
      return isEllaInternalPilotLocaleSupported(selectedLanguageCode);
    }
    final locale = Localizations.maybeLocaleOf(context);
    return locale != null && isEllaInternalPilotLocaleSupported(locale.languageCode);
  }

  void _handleSignIn() {
    if (SharedPreferencesUtil().onboardingCompleted) {
      _routeToAuthenticatedHome();
    } else {
      unawaited(_onSignedIn());
    }
  }

  @override
  Widget build(BuildContext context) {
    final publicMode = SharedPreferencesUtil().publicMode;
    final showGuardianSurfaces = allowsGuardianSurface(isPublicBuild: publicMode);
    if (!_isSignedIn) {
      return Scaffold(
        backgroundColor: EllaColors.bgPrimary,
        body: widget.authBuilder?.call(context, _handleSignIn) ?? AuthComponent(onSignIn: _handleSignIn),
      );
    }

    final onboarding = Scaffold(
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
                onNext: showGuardianSurfaces ? () => _goToPage(2) : _completeOnboarding,
                onSkip: showGuardianSurfaces ? () => _goToPage(2) : _completeOnboarding,
                onBack: () => _goToPage(0),
              ),
              if (showGuardianSurfaces)
                EllaEmergency(onComplete: _completeOnboarding, onSkip: _completeOnboarding, onBack: () => _goToPage(1)),
            ],
          ),
          Positioned(
            bottom: MediaQuery.of(context).padding.bottom + 8,
            left: 0,
            right: 0,
            child: EllaProgressDots(currentPage: _currentPage, totalPages: showGuardianSurfaces ? 3 : 2),
          ),
        ],
      ),
    );
    if (widget.entitlementGateEnabled) {
      return EllaEntitlementGatePage(
        pilotLocaleRestricted: widget.pilotLocaleRestricted,
        onPilotLocaleAllowed: _resumePendingPilotSignIn,
        readyChild: widget.provisioningGateEnabled ? EllaProvisioningGatePage(readyChild: onboarding) : onboarding,
      );
    }
    return onboarding;
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
