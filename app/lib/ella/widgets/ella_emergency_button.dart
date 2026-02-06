import 'dart:async';

import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:geolocator/geolocator.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/emergency.dart';
import 'package:omi/ella/services/emergency_api.dart';
import 'package:omi/ella/services/emergency_audio_player.dart';
import 'package:omi/ella/widgets/ella_emergency_overlay.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/utils/logger.dart';
import 'package:provider/provider.dart';

enum EmergencyButtonState { idle, alerting, cooldown }

class EllaEmergencyButton extends StatefulWidget {
  const EllaEmergencyButton({super.key});

  @override
  State<EllaEmergencyButton> createState() => _EllaEmergencyButtonState();
}

class _EllaEmergencyButtonState extends State<EllaEmergencyButton> with SingleTickerProviderStateMixin {
  EmergencyButtonState _buttonState = EmergencyButtonState.idle;
  late AnimationController _pulseController;
  late Animation<double> _scaleAnimation;
  late Animation<double> _shadowOpacityAnimation;
  late Animation<double> _shadowBlurAnimation;
  Timer? _cooldownTimer;
  bool _isPressed = false;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      duration: const Duration(milliseconds: 2000),
      vsync: this,
    )..repeat(reverse: true);

    _scaleAnimation = Tween<double>(begin: 1.0, end: 1.02).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    _shadowOpacityAnimation = Tween<double>(begin: 0.3, end: 0.5).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    _shadowBlurAnimation = Tween<double>(begin: 12.0, end: 18.0).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _pulseController.dispose();
    _cooldownTimer?.cancel();
    super.dispose();
  }

  void _showNoContactDialog() {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: EllaColors.bgSecondary,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
        title: const Text(
          'No emergency contact set up',
          style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: EllaColors.textPrimary),
        ),
        content: const Text(
          'Add a contact in Settings so Ella knows who to call.',
          style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel', style: TextStyle(fontSize: 18, color: EllaColors.textTertiary)),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              context.read<HomeProvider>().setIndex(2);
            },
            child: const Text('Go to Settings', style: TextStyle(fontSize: 18, color: EllaColors.primary)),
          ),
        ],
      ),
    );
  }

  Future<void> _onEmergencyTap() async {
    if (_buttonState != EmergencyButtonState.idle) return;

    // Check if emergency contact is configured
    final emergencyPhone = SharedPreferencesUtil().emergencyContactPhone;
    if (emergencyPhone.isEmpty) {
      HapticFeedback.heavyImpact();
      _showNoContactDialog();
      return;
    }

    HapticFeedback.heavyImpact();

    setState(() {
      _buttonState = EmergencyButtonState.alerting;
      _pulseController.stop();
    });

    // Start 5-second cooldown (independent of overlay)
    _cooldownTimer = Timer(const Duration(seconds: 5), () {
      if (mounted) {
        setState(() {
          _buttonState = EmergencyButtonState.idle;
          _pulseController.repeat(reverse: true);
        });
      }
    });

    // Get GPS location (non-blocking, 3s timeout)
    Position? position;
    try {
      position = await Geolocator.getCurrentPosition(
        desiredAccuracy: LocationAccuracy.medium,
        timeLimit: const Duration(seconds: 3),
      );
    } catch (_) {
      Logger.debug('Emergency: location unavailable, proceeding without GPS');
    }

    // Fire the API call
    final uid = getCurrentUid();
    EmergencyResponse? response;
    bool apiSuccess = false;

    try {
      response = await postEmergency(
        uid: uid,
        latitude: position?.latitude,
        longitude: position?.longitude,
        accuracyMeters: position?.accuracy,
      );
      apiSuccess = true;
    } on EmergencyApiException catch (e) {
      if (e.statusCode == 429) {
        // Rate limited -- show snackbar, don't open overlay
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Please wait a moment before trying again.')),
          );
        }
        return;
      }
      Logger.debug('Emergency API error: $e');
    }

    if (!mounted) return;

    HapticFeedback.heavyImpact();

    // Push the overlay
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => EllaEmergencyOverlay(
          emergencyId: response?.emergencyId,
          contacts: response?.contactsNotified ?? [],
          cancelWindowSeconds: response?.cancelWindowSeconds ?? 10,
          audioConfirmationUrl: response?.audioConfirmationUrl,
          apiSuccess: apiSuccess,
        ),
      ),
    );

    // Play audio confirmation
    EmergencyAudioPlayer.playConfirmation(audioUrl: response?.audioConfirmationUrl);
  }

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.of(context).disableAnimations;
    final isIdle = _buttonState == EmergencyButtonState.idle;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: EllaSizes.spacingM, vertical: EllaSizes.spacingS),
      child: Semantics(
        button: true,
        label: 'Emergency. Tap to get help and notify your emergency contacts.',
        hint: 'Double tap to activate',
        child: AnimatedBuilder(
          animation: _pulseController,
          builder: (context, child) {
            final scale = (isIdle && !reduceMotion) ? _scaleAnimation.value : (_isPressed ? 0.97 : 1.0);
            final shadowOpacity = (isIdle && !reduceMotion) ? _shadowOpacityAnimation.value : 0.0;
            final shadowBlur = (isIdle && !reduceMotion) ? _shadowBlurAnimation.value : 0.0;

            return Transform.scale(
              scale: scale,
              child: GestureDetector(
                onTapDown: (_) {
                  if (isIdle) setState(() => _isPressed = true);
                },
                onTapUp: (_) {
                  if (_isPressed) setState(() => _isPressed = false);
                  _onEmergencyTap();
                },
                onTapCancel: () {
                  if (_isPressed) setState(() => _isPressed = false);
                },
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  curve: Curves.easeOut,
                  height: EllaSizes.emergencyButtonHeight,
                  width: double.infinity,
                  padding: const EdgeInsets.symmetric(horizontal: EllaSizes.spacingL),
                  decoration: BoxDecoration(
                    color:
                        isIdle ? (_isPressed ? EllaColors.emergencyDark : EllaColors.emergency) : EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                    boxShadow: isIdle
                        ? [
                            BoxShadow(
                              color: EllaColors.emergency.withOpacity(_isPressed ? 0.5 : shadowOpacity),
                              blurRadius: _isPressed ? 6 : shadowBlur,
                              offset: Offset(0, _isPressed ? 2 : 4),
                            ),
                          ]
                        : [],
                  ),
                  child: AbsorbPointer(
                    absorbing: !isIdle,
                    child: Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: isIdle
                          ? [
                              const Icon(Icons.warning_rounded, size: EllaSizes.iconLarge, color: Colors.white),
                              const SizedBox(width: 12),
                              const Text(
                                'Emergency - Get Help',
                                style: TextStyle(
                                  fontSize: 20,
                                  fontWeight: FontWeight.w600,
                                  color: Colors.white,
                                ),
                              ),
                            ]
                          : [
                              const SizedBox(
                                width: 20,
                                height: 20,
                                child: CupertinoActivityIndicator(color: EllaColors.textDisabled),
                              ),
                              const SizedBox(width: 12),
                              const Text(
                                'Alerting...',
                                style: TextStyle(
                                  fontSize: 20,
                                  color: EllaColors.textDisabled,
                                ),
                              ),
                            ],
                    ),
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}
