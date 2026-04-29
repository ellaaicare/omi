import 'package:flutter/material.dart';

import 'package:url_launcher/url_launcher.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/settings/webview.dart';
import 'package:omi/utils/analytics/intercom.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

class AboutOmiPage extends StatefulWidget {
  const AboutOmiPage({super.key});

  @override
  State<AboutOmiPage> createState() => _AboutOmiPageState();
}

class _AboutOmiPageState extends State<AboutOmiPage> {
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        title: Text(context.l10n.aboutOmi),
        backgroundColor: EllaColors.bgPrimary,
        foregroundColor: EllaColors.textPrimary,
        iconTheme: const IconThemeData(color: EllaColors.textPrimary),
      ),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.privacyPolicy, style: const TextStyle(color: EllaColors.textPrimary)),
              trailing: const Icon(Icons.privacy_tip_outlined, color: EllaColors.textTertiary, size: 20),
              onTap: () {
                MixpanelManager().pageOpened('About Privacy Policy');
                routeToPage(
                  context,
                  PageWebView(url: 'https://www.omi.me/pages/privacy', title: context.l10n.privacyPolicyTitle),
                );
              },
            ),
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.visitWebsite, style: const TextStyle(color: EllaColors.textPrimary)),
              subtitle: const Text('https://omi.me', style: TextStyle(color: EllaColors.textSecondary)),
              trailing: const Icon(Icons.language_outlined, color: EllaColors.textTertiary, size: 20),
              onTap: () {
                MixpanelManager().pageOpened('About Visit Website');
                // routeToPage(context, const PageWebView(url: 'https://www.omi.me/', title: 'omi'));
                launchUrl(Uri.parse('https://www.omi.me/'));
              },
            ),
            ListTile(
              title: Text(context.l10n.helpOrInquiries, style: const TextStyle(color: EllaColors.textPrimary)),
              subtitle: const Text('team@basedhardware.com', style: TextStyle(color: EllaColors.textSecondary)),
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              trailing: const Icon(Icons.help_outline_outlined, color: EllaColors.textTertiary, size: 20),
              onTap: () async {
                await IntercomManager.instance.intercom.displayMessenger();
              },
            ),
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.joinCommunity, style: const TextStyle(color: EllaColors.textPrimary)),
              subtitle: Text(context.l10n.membersAndCounting, style: const TextStyle(color: EllaColors.textSecondary)),
              trailing: const Icon(Icons.discord, color: Colors.purple, size: 20),
              onTap: () {
                MixpanelManager().pageOpened('About Join Discord');
                launchUrl(Uri.parse('http://discord.omi.me'));
              },
            ),
          ],
        ),
      ),
    );
  }
}
