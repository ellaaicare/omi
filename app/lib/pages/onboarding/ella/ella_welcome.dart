import 'package:flutter/material.dart';

import 'package:firebase_auth/firebase_auth.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaWelcome extends StatefulWidget {
  final VoidCallback onNext;

  const EllaWelcome({super.key, required this.onNext});

  @override
  State<EllaWelcome> createState() => _EllaWelcomeState();
}

class _EllaWelcomeState extends State<EllaWelcome> {
  late final TextEditingController _nameController;

  @override
  void initState() {
    super.initState();
    final displayName = FirebaseAuth.instance.currentUser?.displayName?.split(' ').first ?? '';
    _nameController = TextEditingController(text: displayName);
  }

  @override
  void dispose() {
    _nameController.dispose();
    super.dispose();
  }

  void _handleNext() {
    final name = _nameController.text.trim();
    if (name.isEmpty) return;
    SharedPreferencesUtil().givenName = name;
    widget.onNext();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: GestureDetector(
          onTap: () => FocusScope.of(context).unfocus(),
          behavior: HitTestBehavior.opaque,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.center,
              children: [
                const SizedBox(height: 24),
                Text(
                  context.l10n.ellaOnboardingStep(1, 3),
                  style: const TextStyle(fontSize: 16, color: EllaColors.textTertiary),
                ),
                const Spacer(flex: 2),
                Container(
                  width: 64,
                  height: 64,
                  decoration: const BoxDecoration(
                    color: EllaColors.primary,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(Icons.favorite, color: Colors.white, size: 32),
                ),
                const SizedBox(height: 24),
                Text(
                  context.l10n.ellaWelcomeGreeting,
                  style: const TextStyle(
                    fontSize: 28,
                    fontWeight: FontWeight.bold,
                    color: EllaColors.textPrimary,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  context.l10n.ellaWelcomeDescription,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    fontSize: 20,
                    color: EllaColors.textSecondary,
                    height: 1.4,
                  ),
                ),
                const Spacer(flex: 1),
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
                const SizedBox(height: 12),
                ValueListenableBuilder<TextEditingValue>(
                  valueListenable: _nameController,
                  builder: (context, value, _) {
                    return SizedBox(
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
                          fillColor: EllaColors.bgTertiary,
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(16),
                            borderSide: BorderSide.none,
                          ),
                          contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                        ),
                        textInputAction: TextInputAction.done,
                        onSubmitted: (_) => _handleNext(),
                      ),
                    );
                  },
                ),
                const Spacer(flex: 2),
                ValueListenableBuilder<TextEditingValue>(
                  valueListenable: _nameController,
                  builder: (context, value, _) {
                    final enabled = value.text.trim().isNotEmpty;
                    return SizedBox(
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
                    );
                  },
                ),
                const SizedBox(height: 32),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
