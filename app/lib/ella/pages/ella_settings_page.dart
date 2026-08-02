import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/core/app_shell.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/pages/conversation_lifecycle_settings_page.dart';
import 'package:omi/ella/pages/ella_care_team_page.dart';
import 'package:omi/ella/pages/alert_channels_page.dart';
import 'package:omi/ella/pages/ella_emergency_contact_page.dart';
import 'package:omi/ella/pages/ella_profile_page.dart';
import 'package:omi/ella/pages/ella_workspace_page.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/pages/guardian_mode_page.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_legal_links.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/widgets/ella_settings_row.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/settings/delete_account.dart';
import 'package:omi/pages/settings/settings_drawer.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/developer_mode_provider.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/utils/auth_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

class EllaSettingsPage extends StatefulWidget {
  const EllaSettingsPage({super.key, this.runtimeSideEffectsEnabled = true, this.authenticatedUidOverride});

  final bool runtimeSideEffectsEnabled;
  final String? authenticatedUidOverride;

  @override
  State<EllaSettingsPage> createState() => _EllaSettingsPageState();
}

class _EllaSettingsPageState extends State<EllaSettingsPage> with RouteAware {
  List<Caregiver> _caregivers = [];
  String? _emergencyContactId;
  String _appVersion = '';
  GuardianModeInfo? _guardianMode;
  int _versionTapCount = 0;
  bool _developerUnlocked = false;

  @override
  void initState() {
    super.initState();
    if (widget.runtimeSideEffectsEnabled) {
      _loadData();
      _loadAppVersion();
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
  }

  Future<void> _loadData() async {
    try {
      if (!allowsUnverifiedEllaSurface()) return;
      final results = await Future.wait([
        caregiver_api.getCaregivers(),
        caregiver_api.getEmergencyContactId(),
        guardian_api.getGuardianMode(),
      ]);
      final caregivers = results[0] as List<Caregiver>;
      final emergencyId = results[1] as String?;
      final guardianMode = results[2] as GuardianModeInfo?;
      if (mounted) {
        setState(() {
          _caregivers = caregivers;
          _emergencyContactId = emergencyId;
          if (guardianMode != null) _guardianMode = guardianMode;
        });
      }
    } catch (e) {
      Logger.debug('Failed to load settings data: $e');
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

  void _unlockDeveloperSettings() {
    if (!allowsInheritedOmiSurface()) return;
    setState(() {
      _versionTapCount += 1;
      if (_versionTapCount >= 7) _developerUnlocked = true;
    });
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

  String _conversationMaxDurationSubtitle(UserProvider userProvider) {
    final enabled = userProvider.conversationMaxDurationEnabled;
    final seconds = userProvider.conversationMaxDurationSeconds;
    if (enabled == false) return context.l10n.ellaMaxConversationLengthDisabled;
    if (enabled == true && seconds != null) return context.l10n.minLabel(seconds ~/ 60);
    return context.l10n.ellaMaxConversationLengthBackendDefault;
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
                Navigator.of(
                  context,
                ).pushAndRemoveUntil(MaterialPageRoute(builder: (context) => const AppShell()), (route) => false);
              }
            },
            child: Text(
              context.l10n.ellaSettingsSignOut,
              style: const TextStyle(fontSize: 18, color: EllaColors.error),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _openListeningConsent() async {
    final uid = FirebaseAuth.instance.currentUser?.uid ?? '';
    final consentService = EllaAiConsentService();
    await consentService.refreshServerAuthority(uid: uid);
    if (!mounted) return;
    var revokeSynced = true;
    final accepted = await AiConsentSheet.show(
      context,
      reviewMode: true,
      onAccept: () async {
        final receiptId = await consentService.grantCurrentConsent(uid: uid);
        if (receiptId == null) return false;
        if (mounted) context.read<EllaProvisioningProvider>().setConsentReceiptId(receiptId);
        return true;
      },
      onDecline: () async {
        revokeSynced = await consentService.revokeCurrentConsent(uid: uid);
        return revokeSynced;
      },
      onRequestDeletion: () async {
        await Navigator.of(context).push(MaterialPageRoute(builder: (_) => const DeleteAccount()));
      },
    );
    if (!mounted) return;
    final captureProvider = context.read<CaptureProvider>();
    if (accepted == true) {
      await captureProvider.streamDeviceRecording(device: context.read<DeviceProvider>().connectedDevice);
    } else {
      await captureProvider.stopStreamDeviceRecording();
      await captureProvider.stopStreamRecording();
    }
    if (!mounted) return;
    setState(() {});
    if (!revokeSynced) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.aiConsentRevokeSyncFailed)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final publicMode = SharedPreferencesUtil.isPublicBuild ? true : context.watch<DeveloperModeProvider>().publicMode;
    final showUnverifiedSurfaces = allowsUnverifiedEllaSurface();
    final userName = SharedPreferencesUtil().givenName.isNotEmpty
        ? SharedPreferencesUtil().givenName
        : SharedPreferencesUtil().fullName;
    final deviceProvider = context.watch<DeviceProvider>();
    final deviceName =
        deviceProvider.presentationConnectedDevice?.name ?? deviceProvider.connectedDevice?.name ?? 'Not connected';
    final userProvider = context.watch<UserProvider>();
    final authenticatedUid = widget.authenticatedUidOverride ?? FirebaseAuth.instance.currentUser?.uid ?? '';

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
                await Navigator.push(context, MaterialPageRoute(builder: (context) => const EllaProfilePage()));
                setState(() {}); // refresh after profile edits
              },
            ),
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.lock_person_outlined,
              title: context.l10n.ellaWorkspaceTitle,
              subtitle: context.l10n.ellaWorkspaceSettingsSubtitle,
              onTap: () {
                Navigator.push(context, MaterialPageRoute(builder: (context) => const EllaWorkspacePage()));
              },
            ),
            const SizedBox(height: 8),

            // Internal policy picker, only available after the developer unlock.
            if (_developerUnlocked)
              EllaSettingsRow(
                icon: Icons.shield,
                iconColor: _guardianMode?.color ?? EllaColors.primary,
                iconBgColor: _guardianMode?.color ?? EllaColors.primary,
                title: 'Whisper settings',
                subtitle: _guardianMode != null ? _guardianMode!.displayName : 'Loading…',
                onTap: () async {
                  final updated = await Navigator.push<GuardianModeState>(
                    context,
                    MaterialPageRoute(builder: (context) => const GuardianModePage(showDemo: true)),
                  );
                  if (updated != null && mounted) _loadData();
                },
              ),

            // Ella Key (for cross-device linking)
            if (_developerUnlocked && SharedPreferencesUtil().ellaKey.isNotEmpty) ...[
              const SizedBox(height: 8),
              EllaSettingsRow(
                icon: Icons.key,
                title: 'Ella Key',
                subtitle: SharedPreferencesUtil().ellaKey,
                onTap: () {
                  Clipboard.setData(ClipboardData(text: SharedPreferencesUtil().ellaKey));
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('Ella Key copied to clipboard'), duration: Duration(seconds: 2)),
                  );
                },
              ),
            ],

            if (showUnverifiedSurfaces) ...[
              // CARE TEAM section
              _buildSectionHeader(context.l10n.ellaCareTeamSection),
              EllaSettingsRow(
                icon: Icons.people,
                title: context.l10n.ellaFamilyCaregivers,
                subtitle: _caregiverCountSubtitle(),
                onTap: () async {
                  await Navigator.push(context, MaterialPageRoute(builder: (context) => const EllaCareTeamPage()));
                  _loadData();
                },
              ),
            ],
            if (showUnverifiedSurfaces && !publicMode) ...[
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
              const SizedBox(height: 8),
              EllaSettingsRow(
                icon: Icons.notifications_active,
                title: context.l10n.ellaAlertChannels,
                subtitle: context.l10n.ellaAlertChannelsSubtitle,
                onTap: () {
                  Navigator.push(context, MaterialPageRoute(builder: (context) => const AlertChannelsPage()));
                },
              ),
              const SizedBox(height: 8),
            ],

            // CAPTURE section
            _buildSectionHeader(context.l10n.ellaCaptureSection),
            EllaSettingsRow(
              icon: Icons.hearing,
              title: context.l10n.listeningAndConsent,
              subtitle: SharedPreferencesUtil().hasAccountBoundAiConsent(authenticatedUid)
                  ? context.l10n.aiConsentAllowedStatus
                  : context.l10n.aiConsentNotAllowedStatus,
              onTap: _openListeningConsent,
            ),
            if (showUnverifiedSurfaces) ...[
              const SizedBox(height: 8),
              EllaSettingsRow(
                icon: Icons.record_voice_over_rounded,
                title: 'What Ella has said',
                subtitle: 'Helpful things Ella has said and why',
                onTap: () {
                  Navigator.push(context, MaterialPageRoute(builder: (context) => const GuardianAlertHistoryPage()));
                },
              ),
            ],
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.schedule,
              title: context.l10n.ellaMaxConversationLengthTitle,
              subtitle: _conversationMaxDurationSubtitle(userProvider),
              onTap: () {
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const ConversationLifecycleSettingsPage()),
                );
              },
            ),

            // DEVICE section
            _buildSectionHeader(context.l10n.ellaDeviceSection),
            EllaSettingsRow(
              icon: Icons.bluetooth_connected,
              title: context.l10n.ellaSettingsDevice,
              subtitle: deviceName,
              onTap: () {
                Navigator.push(context, MaterialPageRoute(builder: (context) => const ConnectDevicePage()));
              },
            ),

            if (_developerUnlocked) ...[
              _buildSectionHeader(context.l10n.ellaMoreSection),
              EllaSettingsRow(
                icon: Icons.developer_mode,
                title: context.l10n.ellaAdvancedSettings,
                subtitle: context.l10n.ellaAdvancedSettingsSubtitle,
                onTap: () => SettingsDrawer.show(context, ellaMode: true),
              ),
              const SizedBox(height: 8),
            ],

            // ABOUT section
            _buildSectionHeader(context.l10n.ellaAboutSection),
            EllaSettingsRow(
              icon: Icons.privacy_tip,
              title: context.l10n.ellaSettingsPrivacy,
              onTap: () => launchUrl(EllaLegalLinks.privacy),
            ),
            const SizedBox(height: 8),
            EllaSettingsRow(
              icon: Icons.description,
              title: context.l10n.ellaSettingsTerms,
              onTap: () => launchUrl(EllaLegalLinks.terms),
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
                child: GestureDetector(
                  onTap: allowsInheritedOmiSurface() ? _unlockDeveloperSettings : null,
                  child: Text(_appVersion, style: const TextStyle(fontSize: 14, color: EllaColors.textDisabled)),
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
