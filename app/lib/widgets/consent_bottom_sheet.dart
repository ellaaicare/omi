import 'package:flutter/material.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/auth_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

class ConsentBottomSheet extends StatelessWidget {
  final String authMethod;
  final VoidCallback onContinue;
  const ConsentBottomSheet({super.key, required this.authMethod, required this.onContinue});

  @override
  Widget build(BuildContext context) {
    final legalStyle = TextButton.styleFrom(
      minimumSize: const Size(48, 48),
      foregroundColor: EllaColors.tealDeep,
      textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 16, decoration: TextDecoration.underline),
    );
    return Container(
      decoration: const BoxDecoration(
          color: EllaColors.bgSecondary, borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height - MediaQuery.paddingOf(context).top),
        child: SafeArea(
            top: false,
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                Semantics(
                    header: true,
                    child: Text(context.l10n.dataAndPrivacy,
                        style:
                            const TextStyle(color: EllaColors.textPrimary, fontSize: 24, fontWeight: FontWeight.bold))),
                const SizedBox(height: 16),
                Row(children: [
                  Icon(authMethod == 'apple' ? FontAwesomeIcons.apple : FontAwesomeIcons.google,
                      color: EllaColors.textPrimary, size: 24),
                  const SizedBox(width: 12),
                  Expanded(
                      child: Text(authMethod == 'apple' ? context.l10n.signInWithApple : context.l10n.signInWithGoogle,
                          style: const TextStyle(
                              color: EllaColors.textPrimary, fontSize: 16, fontWeight: FontWeight.w500))),
                ]),
                const SizedBox(height: 20),
                Text(context.l10n.ellaAuthDataDisclosure,
                    style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16, height: 1.4)),
                const SizedBox(height: 16),
                Text(
                    '${context.l10n.yourDataIsProtected}${context.l10n.privacyPolicy}'
                    '${context.l10n.and}${context.l10n.termsOfService}.',
                    style: const TextStyle(color: EllaColors.textSecondary, fontSize: 16, height: 1.4)),
                TextButton(
                    key: const Key('consent-privacy'),
                    style: legalStyle,
                    onPressed: () => context.read<AuthenticationProvider>().openPrivacyPolicy(),
                    child: Text(context.l10n.privacyPolicy)),
                TextButton(
                    key: const Key('consent-terms'),
                    style: legalStyle,
                    onPressed: () => context.read<AuthenticationProvider>().openTermsOfService(),
                    child: Text(context.l10n.termsOfService)),
                const SizedBox(height: 24),
                SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      key: const Key('consent-continue'),
                      onPressed: () {
                        Navigator.of(context).pop();
                        onContinue();
                      },
                      style: ElevatedButton.styleFrom(
                        minimumSize: const Size(48, 52),
                        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
                        backgroundColor: EllaColors.tealDeep,
                        foregroundColor: EllaColors.paper,
                        textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 16, fontWeight: FontWeight.w600),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                        elevation: 0,
                      ),
                      child: Text(
                          authMethod == 'apple' ? context.l10n.continueWithApple : context.l10n.continueWithGoogle,
                          textAlign: TextAlign.center),
                    )),
                const SizedBox(height: 12),
                SizedBox(
                    width: double.infinity,
                    child: TextButton(
                      key: const Key('consent-cancel'),
                      onPressed: () => Navigator.of(context).pop(),
                      style: TextButton.styleFrom(
                        minimumSize: const Size(48, 52),
                        foregroundColor: EllaColors.textSecondary,
                        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
                        textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 16, fontWeight: FontWeight.w500),
                      ),
                      child: Text(context.l10n.cancel, textAlign: TextAlign.center),
                    )),
              ]),
            )),
      ),
    );
  }

  static void show(BuildContext context, {required String authMethod, required VoidCallback onContinue}) {
    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.transparent,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (context) => ConsentBottomSheet(authMethod: authMethod, onContinue: onContinue),
    );
  }
}
