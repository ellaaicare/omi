import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/pages/home/page.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/pages/onboarding/find_device/page.dart';
import 'package:omi/pages/settings/device_settings.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/widgets/device_widget.dart';

class ConnectDevicePage extends StatefulWidget {
  const ConnectDevicePage({super.key, this.originUid, this.authenticatedUid, this.consentRequester});

  final String? originUid;
  final String Function()? authenticatedUid;
  final Future<bool> Function(BuildContext)? consentRequester;

  @override
  State<ConnectDevicePage> createState() => _ConnectDevicePageState();
}

class _ConnectDevicePageState extends State<ConnectDevicePage> {
  late final String _originUid = widget.originUid ?? _currentUid;

  String get _currentUid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;
  bool get _ownerIsCurrent => _originUid.isNotEmpty && _originUid == _currentUid;

  @override
  void initState() {
    super.initState();
    MixpanelManager().connectDevicePageOpened();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
        appBar: AppBar(
          title: Text(
            context.l10n.connect,
            style: const TextStyle(color: Color(0xFF2D2D2D)),
          ),
          backgroundColor: const Color(0xFFFAF5F0),
          iconTheme: const IconThemeData(color: Color(0xFF2D2D2D)),
          actions: [
            IconButton(
              onPressed: () {
                Navigator.of(context).push(
                  MaterialPageRoute(
                    builder: (context) => const DeviceSettings(),
                  ),
                );
              },
              icon: const Icon(Icons.settings),
            )
          ],
        ),
        backgroundColor: const Color(0xFFFAF5F0),
        body: ListView(
          children: [
            Consumer<OnboardingProvider>(
              builder: (context, onboardingProvider, child) {
                return DeviceAnimationWidget(
                  isConnected: onboardingProvider.isConnected,
                  deviceName: onboardingProvider.deviceName,
                  deviceType: onboardingProvider.deviceType,
                  animatedBackground: onboardingProvider.isConnected,
                );
              },
            ),
            FindDevicesPage(
              isFromOnboarding: false,
              canConnect: () => _ownerIsCurrent,
              consentRequester: widget.consentRequester,
              goNext: () {
                Logger.debug('onConnected from FindDevicesPage');
                routeToPage(context, const HomePageWrapper(), replace: true);
              },
              includeSkip: false,
            )
          ],
        ));
  }
}
