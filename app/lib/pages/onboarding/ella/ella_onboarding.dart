import 'package:flutter/material.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/onboarding/auth.dart';
import 'package:omi/pages/onboarding/ella/ella_connect.dart';
import 'package:omi/pages/onboarding/ella/ella_emergency.dart';
import 'package:omi/pages/onboarding/ella/ella_welcome.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/other/temp.dart';

class EllaOnboarding extends StatefulWidget {
  const EllaOnboarding({super.key});

  @override
  State<EllaOnboarding> createState() => _EllaOnboardingState();
}

class _EllaOnboardingState extends State<EllaOnboarding> {
  final PageController _pageController = PageController();
  int _currentPage = 0;
  bool _isSignedIn = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (AuthService.instance.isSignedIn()) {
        if (SharedPreferencesUtil().onboardingCompleted) {
          routeToPage(context, const HomePageWrapper(), replace: true);
        } else {
          setState(() => _isSignedIn = true);
        }
      }
    });
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  void _goToPage(int page) {
    _pageController.animateToPage(
      page,
      duration: const Duration(milliseconds: 300),
      curve: Curves.easeInOut,
    );
    setState(() => _currentPage = page);
  }

  void _completeOnboarding() {
    SharedPreferencesUtil().onboardingCompleted = true;
    updateUserOnboardingState(completed: true);
    routeToPage(context, const HomePageWrapper(), replace: true);
  }

  @override
  Widget build(BuildContext context) {
    if (!_isSignedIn) {
      return Scaffold(
        backgroundColor: EllaColors.bgPrimary,
        body: AuthComponent(
          onSignIn: () {
            if (SharedPreferencesUtil().onboardingCompleted) {
              routeToPage(context, const HomePageWrapper(), replace: true);
            } else {
              setState(() => _isSignedIn = true);
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
              EllaWelcome(onNext: () => _goToPage(1)),
              EllaConnect(
                onNext: () => _goToPage(2),
                onSkip: () => _goToPage(2),
                onBack: () => _goToPage(0),
              ),
              EllaEmergency(
                onComplete: _completeOnboarding,
                onSkip: _completeOnboarding,
                onBack: () => _goToPage(1),
              ),
            ],
          ),
          Positioned(
            bottom: MediaQuery.of(context).padding.bottom + 8,
            left: 0,
            right: 0,
            child: EllaProgressDots(currentPage: _currentPage, totalPages: 3),
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
