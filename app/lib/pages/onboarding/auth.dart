import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/auth_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/widgets/consent_bottom_sheet.dart';

class AuthComponent extends StatefulWidget {
  final VoidCallback onSignIn;
  const AuthComponent({super.key, required this.onSignIn});
  @override
  State<AuthComponent> createState() => _AuthComponentState();
}

class _AuthComponentState extends State<AuthComponent> {
  @override
  Widget build(BuildContext context) {
    return Consumer<AuthenticationProvider>(builder: (context, provider, child) {
      final buttonStyle = ElevatedButton.styleFrom(
        minimumSize: const Size(double.infinity, 56),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
        backgroundColor: EllaColors.bgSecondary,
        foregroundColor: EllaColors.textPrimary,
        disabledForegroundColor: EllaColors.textSecondary,
        disabledBackgroundColor: EllaColors.bgTertiary,
        textStyle: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, fontFamily: 'Manrope'),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
      );
      final legalStyle = TextButton.styleFrom(
        minimumSize: const Size(48, 48),
        foregroundColor: EllaColors.tealDeep,
        textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 16, decoration: TextDecoration.underline),
      );
      return SafeArea(child: LayoutBuilder(builder: (context, constraints) {
        return SingleChildScrollView(
            child: ConstrainedBox(
          constraints: BoxConstraints(minHeight: constraints.maxHeight),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
            child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
              Semantics(
                  header: true,
                  child: const Text('ella',
                      style: TextStyle(
                          fontSize: 40,
                          fontWeight: FontWeight.w600,
                          color: EllaColors.textPrimary,
                          fontFamily: 'Manrope'))),
              const SizedBox(height: 40),
              if (provider.loading) ...[
                const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(color: EllaColors.tealDeep)),
                const SizedBox(height: 12),
                Text(context.l10n.pleaseWait, textAlign: TextAlign.center),
                const SizedBox(height: 24),
              ],
              if (Platform.isIOS || Platform.isMacOS) ...[
                ElevatedButton(
                  onPressed: provider.loading
                      ? null
                      : () {
                          HapticFeedback.mediumImpact();
                          ConsentBottomSheet.show(context, authMethod: 'apple', onContinue: () async {
                            final user = FirebaseAuth.instance.currentUser;
                            if (user != null && user.isAnonymous && SharedPreferencesUtil().hasPersonaCreated) {
                              await provider.linkWithApple();
                              if (mounted) {
                                SharedPreferencesUtil().hasOmiDevice = true;
                                SharedPreferencesUtil().verifiedPersonaId = null;
                                widget.onSignIn();
                              }
                            } else {
                              provider.onAppleSignIn(widget.onSignIn);
                            }
                          });
                        },
                  style: buttonStyle,
                  child: Row(children: [
                    const Icon(FontAwesomeIcons.apple, size: 24),
                    const SizedBox(width: 12),
                    Expanded(child: Text(context.l10n.signInWithApple, textAlign: TextAlign.center)),
                  ]),
                ),
                const SizedBox(height: 16),
              ],
              ElevatedButton(
                onPressed: provider.loading
                    ? null
                    : () {
                        HapticFeedback.mediumImpact();
                        ConsentBottomSheet.show(context, authMethod: 'google', onContinue: () async {
                          final user = FirebaseAuth.instance.currentUser;
                          if (user != null && user.isAnonymous && SharedPreferencesUtil().hasPersonaCreated) {
                            await provider.linkWithGoogle();
                            if (mounted) {
                              SharedPreferencesUtil().hasOmiDevice = true;
                              SharedPreferencesUtil().verifiedPersonaId = null;
                              widget.onSignIn();
                            }
                          } else {
                            provider.onGoogleSignIn(widget.onSignIn);
                          }
                        });
                      },
                style: buttonStyle,
                child: Row(children: [
                  const Icon(FontAwesomeIcons.google, size: 24),
                  const SizedBox(width: 12),
                  Expanded(child: Text(context.l10n.signInWithGoogle, textAlign: TextAlign.center)),
                ]),
              ),
              const SizedBox(height: 24),
              Text(
                  '${context.l10n.byContinuingAgree}${context.l10n.privacyPolicy}'
                  '${context.l10n.and}${context.l10n.termsOfUse}.',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary)),
              TextButton(
                  key: const Key('auth-privacy'),
                  style: legalStyle,
                  onPressed: provider.openPrivacyPolicy,
                  child: Text(context.l10n.privacyPolicy, textAlign: TextAlign.center)),
              TextButton(
                  key: const Key('auth-terms'),
                  style: legalStyle,
                  onPressed: provider.openTermsOfService,
                  child: Text(context.l10n.termsOfUse, textAlign: TextAlign.center)),
            ]),
          ),
        ));
      }));
    });
  }
}
