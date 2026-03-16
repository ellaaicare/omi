import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:package_info_plus/package_info_plus.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/core/app_shell.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/pages/ella_care_team_page.dart';
import 'package:omi/ella/pages/ella_emergency_contact_page.dart';
import 'package:omi/ella/pages/ella_profile_page.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/widgets/ella_settings_row.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/settings/settings_drawer.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/utils/auth_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

class EllaSettingsPage extends StatefulWidget {
  const EllaSettingsPage({super.key});

  @override
  State<EllaSettingsPage> createState() => _EllaSettingsPageState();
}

class _EllaSettingsPageState extends State<EllaSettingsPage> with RouteAware {
  List<Caregiver> _caregivers = [];
  String? _emergencyContactId;
  String _appVersion = '';

  @override
  void initState() {
    super.initState();
    _loadData();
    _loadAppVersion();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
  }

  Future<void> _loadData() async {
    try {
      final caregivers = await caregiver_api.getCaregivers();
      final emergencyId = await caregiver_api.getEmergencyContactId();
      if (mounted) {
        setState(() {
          _caregivers = caregivers;
          _emergencyContactId = emergencyId;
        });
      }
    } catch (e) {
      Logger.debug('Failed to load caregivers: $e');
    }
  }

  Future<void> _loadAppVersion() async {
    try {
      final info = await PackageInfo.fromPlatform();
      if (mounted) {
        setState(() {
          _appVersion = 'Ella v${info.version} (${info.buildNumber})';
        });
      }
    } catch (_) {}
  }

  String _emergencyContactSubtitle() {
    if (_caregivers.isEmpty || _emergencyContactId == null) {
      return context.l10n.ellaEmergencyContactNotSetUp;
    }
    try {
      final contact = _caregivers.firstWhere((c) => c.id == _emergencyContactId);
      return '${contact.name} (${contact.displayRelationship})';
    } catch (_) {
      return context.l10n.ellaEmergencyContactNotSetUp;
    }
  }

  String _caregiverCountSubtitle() {
    if (_caregivers.isEmpty) return context.l10n.ellaFamilyCaregiversNotSetUp;
    return context.l10n.ellaFamilyCaregiversSubtitle(_caregivers.length);
  }

  Future<void> _signOut() async {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: EllaColors.bgSecondary,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
        title: Text(
          context.l10n.ellaSettingsSignOut,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
        content: const Text(
          'Are you sure you want to sign out?',
          style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: Text(context.l10n.ellaCancel, style: const TextStyle(fontSize: 18, color: EllaColors.textTertiary)),
          ),
          TextButton(
            onPressed: () async {
              Navigator.of(ctx).pop();
              await signOutAndClearUserData(context);
              if (mounted) {
                Navigator.of(context).pushAndRemoveUntil(
                  MaterialPageRoute(builder: (context) => const AppShell()),
                  (route) => false,
                );
              }
            },
            child:
                Text(context.l10n.ellaSettingsSignOut, style: const TextStyle(fontSize: 18, color: EllaColors.error)),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final userName = SharedPreferencesUtil().givenName.isNotEmpty
        ? SharedPreferencesUtil().givenName
        : SharedPreferencesUtil().fullName;
    final deviceName = context.watch<DeviceProvider>().connectedDevice?.name ?? 'Not connected';

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        title: const Text(
          'Settings',
          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
        centerTitle: false,
        automaticallyImplyLeading: false,
      ),
      body: RefreshIndicator(
        onRefresh: _loadData,
        color: EllaColors.primary,
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          children: [
            const SizedBox(height: 8),
            // Profile row — navigates to full profile page
            EllaSettingsRow(
              icon: Icons.person,
              title: context.l10n.ellaSettingsProfile,
              subtitle: _profileSubtitle(userName),
              onTap: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const EllaProfilePage()),
                );
                setState(() {}); // refresh after profile edits
              },
            ),

            // Ella Key (for cross-device linking)
            if (SharedPreferencesUtil().ellaKey.isNotEmpty) ...[
              const SizedBox(height: 8),
              EllaSettingsRow(
                icon: Icons.key,
                title: 'Ella Key',
                subtitle: SharedPreferencesUtil().ellaKey,
                onTap: () {
                  Clipboard.setData(ClipboardData(text: SharedPreferencesUtil().ellaKey));
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text('Ella Key copied to clipboard'),
                      duration: Duration(seconds: 2),
                    ),
                  );
                },
              ),
            ],

            // CARE TEAM section
            _buildSectionHeader(context.l10n.ellaCareTeamSection),
            EllaSettingsRow(
              icon: Icons.people,
              title: context.l10n.ellaFamilyCaregivers,
              subtitle: _caregiverCountSubtitle(),
              onTap: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const EllaCareTeamPage()),
                );
                _loadData();
              },
            ),
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.emergency,
              iconColor: EllaColors.error,
              iconBgColor: EllaColors.error,
              title: context.l10n.ellaEmergencyContact,
              subtitle: _emergencyContactSubtitle(),
              onTap: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const EllaEmergencyContactPage()),
                );
                _loadData();
              },
            ),

            // DEVICE section
            _buildSectionHeader(context.l10n.ellaDeviceSection),
            EllaSettingsRow(
              icon: Icons.bluetooth_connected,
              title: context.l10n.ellaSettingsDevice,
              subtitle: deviceName,
              onTap: () {
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const ConnectDevicePage()),
                );
              },
            ),

            // DEVELOPER section
            _buildSectionHeader('DEVELOPER'),
            EllaSettingsRow(
              icon: Icons.developer_mode,
              title: 'Developer Settings',
              subtitle: 'ASR, transcription, language, debug',
              onTap: () => SettingsDrawer.show(context),
            ),
            const SizedBox(height: 8),

            // ABOUT section
            _buildSectionHeader(context.l10n.ellaAboutSection),
            EllaSettingsRow(
              icon: Icons.privacy_tip,
              title: context.l10n.ellaSettingsPrivacy,
              onTap: () => launchUrl(Uri.parse('https://ella-ai-care.com/legal/privacy')),
            ),
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.description,
              title: context.l10n.ellaSettingsTerms,
              onTap: () => launchUrl(Uri.parse('https://ella-ai-care.com/legal/terms')),
            ),
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.logout,
              iconColor: EllaColors.error,
              iconBgColor: EllaColors.error,
              title: context.l10n.ellaSettingsSignOut,
              titleColor: EllaColors.error,
              showChevron: false,
              onTap: _signOut,
            ),

            const SizedBox(height: 24),
            if (_appVersion.isNotEmpty)
              Center(
                child: Text(
                  _appVersion,
                  style: const TextStyle(fontSize: 14, color: EllaColors.textDisabled),
                ),
              ),
            const SizedBox(height: 32),
          ],
        ),
      ),
    );
  }

  String _profileSubtitle(String name) {
    final email = SharedPreferencesUtil().email;
    if (name.isNotEmpty && email.isNotEmpty) return '$name \u00B7 $email';
    if (name.isNotEmpty) return name;
    if (email.isNotEmpty) return email;
    return '';
  }

  Widget _buildSectionHeader(String text) {
    return Padding(
      padding: const EdgeInsets.only(left: 24, top: 24, bottom: 8),
      child: Text(
        text,
        style: const TextStyle(
          fontSize: 14,
          fontWeight: FontWeight.w600,
          color: EllaColors.primary,
          letterSpacing: 1.2,
        ),
      ),
    );
  }
}
