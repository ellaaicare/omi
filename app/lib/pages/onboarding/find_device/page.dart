import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/widgets/dialog.dart';
import 'found_devices.dart';

class FindDevicesPage extends StatefulWidget {
  final bool isFromOnboarding;
  final VoidCallback goNext;
  final VoidCallback? onSkip;
  final bool includeSkip;
  final bool Function()? canConnect;
  final Future<bool> Function(BuildContext)? consentRequester;

  const FindDevicesPage(
      {super.key,
      required this.goNext,
      this.includeSkip = true,
      this.isFromOnboarding = false,
      this.onSkip,
      this.canConnect,
      this.consentRequester});

  @override
  State<FindDevicesPage> createState() => _FindDevicesPageState();
}

class _FindDevicesPageState extends State<FindDevicesPage> {
  OnboardingProvider? _provider;
  bool _scanError = false;

  @override
  void initState() {
    super.initState();
    _provider = Provider.of<OnboardingProvider>(context, listen: false);

    SchedulerBinding.instance.addPostFrameCallback((_) {
      if (widget.isFromOnboarding) {
        context.read<HomeProvider>().setupHasSpeakerProfile();
      }
      _scanDevices();
    });
  }

  @override
  dispose() {
    _provider = null;

    super.dispose();
  }

  Future<void> _scanDevices() async {
    if (!mounted || (widget.canConnect != null && !widget.canConnect!())) return;
    setState(() => _scanError = false);
    try {
      await _provider?.scanDevices(
        onShowDialog: () {
          if (mounted) {
            showDialog(
              context: context,
              builder: (c) => getDialog(
                context,
                () {
                  Navigator.of(context).pop();
                },
                () {},
                context.l10n.enableBluetooth,
                context.l10n.bluetoothNeeded,
                singleButton: true,
              ),
            );
          }
        },
      );
    } catch (_) {
      if (mounted) setState(() => _scanError = true);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<OnboardingProvider>(
      builder: (context, provider, child) {
        return Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            FoundDevices(
              goNext: widget.goNext,
              isFromOnboarding: widget.isFromOnboarding,
              canConnect: widget.canConnect,
              consentRequester: widget.consentRequester,
            ),
            if (provider.deviceList.isEmpty && _scanError) Text(context.l10n.upstreamCaptureSearchFailed),
            if (provider.deviceList.isEmpty && (_scanError || provider.enableInstructions))
              TextButton.icon(
                onPressed: widget.canConnect != null && !widget.canConnect!() ? null : _scanDevices,
                icon: const Icon(Icons.refresh_rounded),
                label: Text(context.l10n.tryAgain),
              ),
            if (provider.deviceList.isEmpty && provider.enableInstructions) const SizedBox(height: 48),
            if (provider.deviceList.isEmpty && provider.enableInstructions)
              ElevatedButton(
                onPressed: () => launchUrl(Uri.parse('mailto:team@basedhardware.com')),
                child: Container(
                  width: double.infinity,
                  height: 45,
                  alignment: Alignment.center,
                  child: Text(
                    context.l10n.contactSupport,
                    style: const TextStyle(
                      fontWeight: FontWeight.w400,
                      fontSize: 16,
                      color: Color(0xFF2D2D2D),
                      decoration: TextDecoration.underline,
                    ),
                  ),
                ),
              ),
            if (widget.includeSkip)
              ElevatedButton(
                onPressed: () {
                  if (widget.isFromOnboarding) {
                    widget.onSkip!();
                  } else {
                    widget.goNext();
                  }
                  MixpanelManager().useWithoutDeviceOnboardingFindDevices();
                },
                child: Container(
                  width: double.infinity,
                  height: 45,
                  alignment: Alignment.center,
                  child: Text(
                    context.l10n.connectLater,
                    style: const TextStyle(
                      fontWeight: FontWeight.w400,
                      fontSize: 16,
                      color: Color(0xFF2D2D2D),
                    ),
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}
