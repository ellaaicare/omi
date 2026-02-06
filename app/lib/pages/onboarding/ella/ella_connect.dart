import 'dart:async';

import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaConnect extends StatefulWidget {
  final VoidCallback onNext;
  final VoidCallback onSkip;
  final VoidCallback onBack;

  const EllaConnect({super.key, required this.onNext, required this.onSkip, required this.onBack});

  @override
  State<EllaConnect> createState() => _EllaConnectState();
}

class _EllaConnectState extends State<EllaConnect> with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;
  bool _deviceFound = false;
  bool _showTrouble = false;
  Timer? _troubleTimer;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    )..repeat(reverse: true);
    _pulseAnimation = Tween<double>(begin: 0.8, end: 1.0).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );

    _troubleTimer = Timer(const Duration(seconds: 15), () {
      if (mounted && !_deviceFound) {
        setState(() => _showTrouble = true);
      }
    });

    WidgetsBinding.instance.addPostFrameCallback((_) {
      _startScanning();
    });
  }

  void _startScanning() {
    final provider = context.read<OnboardingProvider>();
    provider.scanDevices(onShowDialog: () {});
  }

  @override
  void dispose() {
    _pulseController.dispose();
    _troubleTimer?.cancel();
    super.dispose();
  }

  void _onDeviceFound(BtDevice device) {
    if (_deviceFound) return;
    setState(() => _deviceFound = true);
    _pulseController.stop();
    _troubleTimer?.cancel();

    final provider = context.read<OnboardingProvider>();
    provider.handleTap(
      device: device,
      isFromOnboarding: false,
      goNext: null,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              const SizedBox(height: 24),
              Row(
                children: [
                  GestureDetector(
                    onTap: widget.onBack,
                    child: Container(
                      width: 48,
                      height: 48,
                      decoration: const BoxDecoration(
                        color: EllaColors.bgTertiary,
                        shape: BoxShape.circle,
                      ),
                      child: const Icon(Icons.arrow_back, color: EllaColors.textPrimary, size: 24),
                    ),
                  ),
                  Expanded(
                    child: Text(
                      context.l10n.ellaOnboardingStep(2, 3),
                      textAlign: TextAlign.center,
                      style: const TextStyle(fontSize: 16, color: EllaColors.textTertiary),
                    ),
                  ),
                  const SizedBox(width: 48),
                ],
              ),
              const SizedBox(height: 16),
              // Hero illustration
              ClipRRect(
                borderRadius: BorderRadius.circular(24),
                child: Image.asset(
                  'assets/images/ella_onboarding_2.png',
                  height: 220,
                  fit: BoxFit.contain,
                ),
              ),
              const SizedBox(height: 24),
              Text(
                context.l10n.ellaConnectTitle,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 28,
                  fontWeight: FontWeight.bold,
                  color: EllaColors.textPrimary,
                ),
              ),
              const SizedBox(height: 32),
              if (!_deviceFound)
                AnimatedBuilder(
                  animation: _pulseAnimation,
                  builder: (context, child) {
                    return Transform.scale(
                      scale: _pulseAnimation.value,
                      child: Container(
                        width: 80,
                        height: 80,
                        decoration: BoxDecoration(
                          color: EllaColors.primary.withValues(alpha: 0.3),
                          shape: BoxShape.circle,
                        ),
                        child: Center(
                          child: Container(
                            width: 56,
                            height: 56,
                            decoration: const BoxDecoration(
                              color: EllaColors.primary,
                              shape: BoxShape.circle,
                            ),
                            child: const Icon(Icons.bluetooth_searching, color: Colors.white, size: 28),
                          ),
                        ),
                      ),
                    );
                  },
                ),
              if (_deviceFound)
                Container(
                  width: 80,
                  height: 80,
                  decoration: const BoxDecoration(
                    color: EllaColors.success,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(Icons.check, color: Colors.white, size: 40),
                ),
              const SizedBox(height: 24),
              Text(
                context.l10n.ellaConnectInstructions,
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 20, color: EllaColors.textSecondary, height: 1.4),
              ),
              const SizedBox(height: 16),
              Consumer<OnboardingProvider>(
                builder: (context, provider, _) {
                  if (_deviceFound) {
                    return Column(
                      children: [
                        Text(
                          context.l10n.ellaConnectFound,
                          style: const TextStyle(
                            fontSize: 22,
                            fontWeight: FontWeight.w600,
                            color: EllaColors.success,
                          ),
                        ),
                        const SizedBox(height: 16),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.all(20),
                          decoration: BoxDecoration(
                            color: EllaColors.bgSecondary,
                            borderRadius: BorderRadius.circular(16),
                          ),
                          child: Row(
                            children: [
                              const Icon(Icons.devices, color: EllaColors.primary, size: 28),
                              const SizedBox(width: 16),
                              Expanded(
                                child: Text(
                                  provider.deviceName.isNotEmpty ? provider.deviceName : 'Omi Device',
                                  style: const TextStyle(
                                    fontSize: 18,
                                    fontWeight: FontWeight.w500,
                                    color: EllaColors.textPrimary,
                                  ),
                                ),
                              ),
                              Text(
                                context.l10n.ellaConnectConnected,
                                style: const TextStyle(
                                    fontSize: 16, color: EllaColors.success, fontWeight: FontWeight.w600),
                              ),
                            ],
                          ),
                        ),
                      ],
                    );
                  }

                  // Check if a device was found in the list
                  if (provider.deviceList.isNotEmpty && !_deviceFound) {
                    WidgetsBinding.instance.addPostFrameCallback((_) {
                      _onDeviceFound(provider.deviceList.first);
                    });
                  }

                  return Text(
                    context.l10n.ellaConnectScanning,
                    style: const TextStyle(fontSize: 18, color: EllaColors.textTertiary),
                  );
                },
              ),
              if (_showTrouble && !_deviceFound) ...[
                const SizedBox(height: 24),
                Text(
                  context.l10n.ellaConnectTrouble,
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 16, color: EllaColors.warning),
                ),
              ],
              const Spacer(),
              if (_deviceFound)
                SizedBox(
                  width: double.infinity,
                  height: 64,
                  child: ElevatedButton(
                    onPressed: widget.onNext,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: EllaColors.primary,
                      foregroundColor: Colors.white,
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                      elevation: 0,
                    ),
                    child: Text(
                      context.l10n.ellaNext,
                      style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                    ),
                  ),
                ),
              if (!_deviceFound)
                TextButton(
                  onPressed: widget.onSkip,
                  style: TextButton.styleFrom(minimumSize: const Size(double.infinity, 48)),
                  child: Text(
                    context.l10n.ellaConnectSkip,
                    style: const TextStyle(
                      fontSize: 18,
                      color: EllaColors.textTertiary,
                      decoration: TextDecoration.underline,
                      decorationColor: EllaColors.textTertiary,
                    ),
                  ),
                ),
              const SizedBox(height: 32),
            ],
          ),
        ),
      ),
    );
  }
}
