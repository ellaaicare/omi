import 'package:flutter/material.dart';

import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/auth_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaWelcome extends StatefulWidget {
  final VoidCallback onNext;
  final VoidCallback? onSignOut;

  const EllaWelcome({super.key, required this.onNext, this.onSignOut});

  @override
  State<EllaWelcome> createState() => _EllaWelcomeState();
}

class _EllaWelcomeState extends State<EllaWelcome> {
  late final TextEditingController _nameController;
  late final TextEditingController _phoneController;
  late final String _email;

  @override
  void initState() {
    super.initState();
    final user = FirebaseAuth.instance.currentUser;
    final displayName = user?.displayName?.split(' ').first ?? '';
    _nameController = TextEditingController(text: displayName);
    _phoneController = TextEditingController();
    _email = user?.email ?? SharedPreferencesUtil().email;
  }

  @override
  void dispose() {
    _nameController.dispose();
    _phoneController.dispose();
    super.dispose();
  }

  void _handleNext() {
    final name = _nameController.text.trim();
    if (name.isEmpty) return;
    SharedPreferencesUtil().givenName = name;
    final phone = _phoneController.text.trim();
    if (phone.isNotEmpty) {
      SharedPreferencesUtil().phoneNumber = phone;
    }
    widget.onNext();
  }

  @override
  Widget build(BuildContext context) {
    final enabled = _nameController.text.trim().isNotEmpty;
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: GestureDetector(
          onTap: () => FocusScope.of(context).unfocus(),
          behavior: HitTestBehavior.opaque,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  const SizedBox(height: 24),
                  Text(
                    context.l10n.ellaOnboardingStep(1, 3),
                    style: const TextStyle(fontSize: 16, color: EllaColors.textTertiary),
                  ),
                  const SizedBox(height: 16),
                  // Hero illustration
                  ClipRRect(
                    borderRadius: BorderRadius.circular(24),
                    child: Image.asset(
                      'assets/images/ella_onboarding_1.png',
                      height: 180,
                      fit: BoxFit.contain,
                    ),
                  ),
                  const SizedBox(height: 20),
                  Text(
                    context.l10n.ellaWelcomeGreeting,
                    style: const TextStyle(
                      fontSize: 28,
                      fontWeight: FontWeight.bold,
                      color: EllaColors.textPrimary,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    context.l10n.ellaWelcomeDescription,
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 18,
                      color: EllaColors.textSecondary,
                      height: 1.4,
                    ),
                  ),
                  const SizedBox(height: 24),
                  // Name field
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      context.l10n.ellaWelcomeNamePrompt,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                        color: EllaColors.textPrimary,
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 56,
                    child: TextField(
                      controller: _nameController,
                      autofocus: false,
                      textCapitalization: TextCapitalization.words,
                      style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
                      decoration: InputDecoration(
                        hintText: context.l10n.ellaWelcomeNamePlaceholder,
                        hintStyle: const TextStyle(fontSize: 20, color: EllaColors.textDisabled),
                        filled: true,
                        fillColor: Colors.white,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                        ),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      ),
                      textInputAction: TextInputAction.next,
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  // Email (from Google sign-in, read-only)
                  if (_email.isNotEmpty) ...[
                    const SizedBox(height: 16),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text(
                        context.l10n.ellaAddCaregiverEmail,
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w400,
                          color: EllaColors.textTertiary,
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Container(
                      height: 56,
                      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      decoration: BoxDecoration(
                        color: EllaColors.bgTertiary,
                        borderRadius: BorderRadius.circular(16),
                      ),
                      alignment: Alignment.centerLeft,
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              _email,
                              style: const TextStyle(fontSize: 18, color: EllaColors.textSecondary),
                            ),
                          ),
                          if (widget.onSignOut != null)
                            GestureDetector(
                              onTap: () async {
                                await signOutAndClearUserData(context);
                                widget.onSignOut?.call();
                              },
                              child: Text(
                                context.l10n.ellaNotYou,
                                style: const TextStyle(
                                  fontSize: 14,
                                  color: EllaColors.primary,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),
                  ],
                  // Phone number field
                  const SizedBox(height: 16),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      context.l10n.ellaAddCaregiverPhone,
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w400,
                        color: EllaColors.textTertiary,
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 56,
                    child: TextField(
                      controller: _phoneController,
                      keyboardType: TextInputType.phone,
                      style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
                      decoration: InputDecoration(
                        hintText: context.l10n.ellaAddCaregiverPhonePlaceholder,
                        hintStyle: const TextStyle(fontSize: 20, color: EllaColors.textDisabled),
                        filled: true,
                        fillColor: Colors.white,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                        ),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      ),
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _handleNext(),
                    ),
                  ),
                  const SizedBox(height: 24),
                  SizedBox(
                    width: double.infinity,
                    height: 64,
                    child: ElevatedButton(
                      onPressed: enabled ? _handleNext : null,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: enabled ? EllaColors.primary : EllaColors.bgTertiary,
                        foregroundColor: enabled ? Colors.white : EllaColors.textDisabled,
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                        elevation: 0,
                      ),
                      child: Text(
                        context.l10n.ellaNext,
                        style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                      ),
                    ),
                  ),
                  const SizedBox(height: 32),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
