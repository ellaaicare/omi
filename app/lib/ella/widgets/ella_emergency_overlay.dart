import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';

import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/emergency.dart';
import 'package:omi/ella/services/emergency_api.dart';
import 'package:omi/ella/services/emergency_audio_player.dart';
import 'package:omi/utils/logger.dart';

class EllaEmergencyOverlay extends StatefulWidget {
  final String? emergencyId;
  final List<NotifiedContact> contacts;
  final int cancelWindowSeconds;
  final String? audioConfirmationUrl;
  final bool apiSuccess;

  const EllaEmergencyOverlay({
    super.key,
    required this.emergencyId,
    required this.contacts,
    required this.cancelWindowSeconds,
    required this.audioConfirmationUrl,
    required this.apiSuccess,
  });

  @override
  State<EllaEmergencyOverlay> createState() => _EllaEmergencyOverlayState();
}

class _EllaEmergencyOverlayState extends State<EllaEmergencyOverlay> with SingleTickerProviderStateMixin {
  Timer? _cancelTimer;
  Timer? _retryTimer;
  late int _remainingSeconds;
  bool _isCancelWindowOpen = true;
  bool _isNetworkError = false;
  late AnimationController _checkScaleController;
  late Animation<double> _checkScaleAnimation;

  @override
  void initState() {
    super.initState();
    _remainingSeconds = widget.cancelWindowSeconds;
    _isNetworkError = !widget.apiSuccess;

    _checkScaleController = AnimationController(
      duration: const Duration(milliseconds: 400),
      vsync: this,
    );
    _checkScaleAnimation = Tween<double>(begin: 0.0, end: 1.0).animate(
      CurvedAnimation(parent: _checkScaleController, curve: Curves.elasticOut),
    );

    if (widget.apiSuccess) {
      _checkScaleController.forward();
      _startCancelCountdown();
      _announceOverlay();
    } else {
      _startAutoRetry();
    }
  }

  void _announceOverlay() {
    SemanticsService.announce(
      'Emergency alert sent. Help is on the way. Your contacts have been notified. '
      'You have $_remainingSeconds seconds to cancel.',
      TextDirection.ltr,
    );
  }

  void _startCancelCountdown() {
    _cancelTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      setState(() {
        _remainingSeconds--;

        if (_remainingSeconds <= 3 && _remainingSeconds > 0) {
          HapticFeedback.selectionClick();
        }

        if (_remainingSeconds <= 0) {
          _isCancelWindowOpen = false;
          HapticFeedback.mediumImpact();
          timer.cancel();
          SemanticsService.announce(
            'Cancel window closed. Emergency is confirmed. Your contacts have been notified.',
            TextDirection.ltr,
          );
        }
      });
    });
  }

  void _startAutoRetry() {
    _retryTimer = Timer.periodic(const Duration(seconds: 10), (timer) async {
      await _retryEmergency();
    });
  }

  Future<void> _retryEmergency() async {
    final uid = getCurrentUid();
    try {
      final response = await postEmergency(uid: uid);
      if (!mounted) return;
      setState(() {
        _isNetworkError = false;
        _remainingSeconds = response.cancelWindowSeconds;
        _isCancelWindowOpen = true;
      });
      _retryTimer?.cancel();
      _checkScaleController.forward();
      _startCancelCountdown();
      _announceOverlay();
      EmergencyAudioPlayer.playConfirmation(audioUrl: response.audioConfirmationUrl);
    } catch (e) {
      Logger.debug('Emergency retry failed: $e');
    }
  }

  Future<void> _cancelEmergency() async {
    if (!_isCancelWindowOpen || widget.emergencyId == null) return;

    _cancelTimer?.cancel();
    HapticFeedback.mediumImpact();

    try {
      await postEmergencyCancel(widget.emergencyId!);
      HapticFeedback.lightImpact();
      await EmergencyAudioPlayer.stop();

      if (!mounted) return;
      SemanticsService.announce('Emergency cancelled. Returning to home screen.', TextDirection.ltr);
      Navigator.of(context).pop();
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Emergency cancelled.')),
      );
    } on EmergencyApiException catch (e) {
      if (e.statusCode == 409) {
        HapticFeedback.heavyImpact();
        if (mounted) {
          setState(() => _isCancelWindowOpen = false);
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Cancel window has expired. Help is on the way.')),
          );
        }
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text("Couldn't cancel. Check your connection and try again.")),
          );
        }
      }
    }
  }

  @override
  void dispose() {
    _cancelTimer?.cancel();
    _retryTimer?.cancel();
    _checkScaleController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: _isNetworkError ? _buildNetworkErrorLayout() : _buildConfirmedLayout(),
      ),
    );
  }

  Widget _buildConfirmedLayout() {
    return Column(
      children: [
        const SizedBox(height: 48),
        _buildStatusIcon(),
        const SizedBox(height: 24),
        const Text(
          'Help is on the way',
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: Colors.white),
        ),
        const SizedBox(height: 8),
        const Text(
          'Your contacts have been notified.',
          style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
        ),
        const SizedBox(height: 32),
        _buildContactList(),
        const Spacer(),
        _buildCancelSection(),
        const SizedBox(height: 16),
        _buildDoneButton(),
        const SizedBox(height: 32),
      ],
    );
  }

  Widget _buildNetworkErrorLayout() {
    final emergencyPhone = SharedPreferencesUtil().getString('emergencyContactPhone');
    final emergencyName = SharedPreferencesUtil().getString('emergencyContactName');

    return Column(
      children: [
        const SizedBox(height: 48),
        // Pulsing teal circle (still trying)
        Container(
          width: 80,
          height: 80,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: EllaColors.primary.withOpacity(0.15),
          ),
          child: const Center(
            child: SizedBox(
                width: 36, height: 36, child: CircularProgressIndicator(color: EllaColors.primary, strokeWidth: 3)),
          ),
        ),
        const SizedBox(height: 24),
        const Text(
          "We're trying to reach\nyour contacts...",
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: Colors.white),
        ),
        const SizedBox(height: 8),
        const Text(
          'Make sure your phone is connected\nto the internet.',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
        ),
        const SizedBox(height: 32),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: SizedBox(
            width: double.infinity,
            height: 64,
            child: ElevatedButton(
              onPressed: _retryEmergency,
              style: ElevatedButton.styleFrom(
                backgroundColor: EllaColors.primary,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
              ),
              child: const Text('Try Again',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600, color: Colors.white)),
            ),
          ),
        ),
        const SizedBox(height: 12),
        const Text(
          'Retrying automatically...',
          style: TextStyle(fontSize: 16, color: EllaColors.textTertiary),
        ),
        const Spacer(),
        if (emergencyPhone.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Semantics(
              button: true,
              label: 'Call ${emergencyName.isNotEmpty ? emergencyName : "emergency contact"} directly on the phone',
              hint: 'Double tap to open phone dialer',
              child: SizedBox(
                width: double.infinity,
                height: 56,
                child: ElevatedButton(
                  onPressed: () => launchUrl(Uri(scheme: 'tel', path: emergencyPhone)),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: EllaColors.bgTertiary,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
                  ),
                  child: Text(
                    'Call ${emergencyName.isNotEmpty ? emergencyName : "contact"} directly',
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: Colors.white),
                  ),
                ),
              ),
            ),
          ),
        const SizedBox(height: 32),
      ],
    );
  }

  Widget _buildStatusIcon() {
    return ScaleTransition(
      scale: _checkScaleAnimation,
      child: Container(
        width: 80,
        height: 80,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: EllaColors.primary.withOpacity(0.15),
        ),
        child: const Center(
          child: Icon(Icons.check, size: 36, color: EllaColors.primary),
        ),
      ),
    );
  }

  Widget _buildContactList() {
    if (widget.contacts.isEmpty) return const SizedBox.shrink();

    return Column(
      children: widget.contacts.map((contact) => _ContactStatusRow(contact: contact)).toList(),
    );
  }

  Widget _buildCancelSection() {
    return AnimatedOpacity(
      opacity: _isCancelWindowOpen ? 1.0 : 0.0,
      duration: const Duration(milliseconds: 500),
      child: _isCancelWindowOpen
          ? Column(
              children: [
                Semantics(
                  liveRegion: true,
                  label: '$_remainingSeconds seconds to cancel',
                  child: SizedBox(
                    width: 100,
                    height: 100,
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        SizedBox(
                          width: 100,
                          height: 100,
                          child: CircularProgressIndicator(
                            value: _remainingSeconds / widget.cancelWindowSeconds,
                            strokeWidth: 4,
                            backgroundColor: EllaColors.bgTertiary,
                            valueColor: const AlwaysStoppedAnimation<Color>(EllaColors.textTertiary),
                          ),
                        ),
                        Text(
                          '${_remainingSeconds}s',
                          style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: Colors.white),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 16),
                Semantics(
                  button: true,
                  label: 'Cancel emergency. $_remainingSeconds seconds remaining.',
                  hint: 'Double tap to cancel the emergency alert',
                  child: TextButton(
                    onPressed: _cancelEmergency,
                    style: TextButton.styleFrom(minimumSize: const Size(double.infinity, 56)),
                    child: const Text(
                      "Cancel -- I'm OK",
                      style: TextStyle(
                        fontSize: 20,
                        fontWeight: FontWeight.w600,
                        color: EllaColors.textTertiary,
                        decoration: TextDecoration.underline,
                        decorationColor: EllaColors.textTertiary,
                      ),
                    ),
                  ),
                ),
              ],
            )
          : const SizedBox.shrink(),
    );
  }

  Widget _buildDoneButton() {
    if (_isCancelWindowOpen) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Semantics(
        button: true,
        label: 'Return to home screen',
        child: SizedBox(
          width: double.infinity,
          height: 56,
          child: ElevatedButton(
            onPressed: () {
              HapticFeedback.lightImpact();
              Navigator.of(context).pop();
            },
            style: ElevatedButton.styleFrom(
              backgroundColor: EllaColors.bgTertiary,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
            ),
            child: const Text(
              'Return to Home',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600, color: Colors.white),
            ),
          ),
        ),
      ),
    );
  }
}

class _ContactStatusRow extends StatelessWidget {
  final NotifiedContact contact;

  const _ContactStatusRow({required this.contact});

  @override
  Widget build(BuildContext context) {
    final (icon, color, label) = _statusVisuals(contact.status, contact.method);

    return Semantics(
      label: '${contact.name}, notified via ${contact.method}, status: ${contact.status}',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: color.withOpacity(0.15),
              ),
              child: Center(child: Icon(icon, size: 20, color: color)),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    contact.name,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: Colors.white),
                  ),
                  Text(label, style: TextStyle(fontSize: 16, color: color)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  (IconData, Color, String) _statusVisuals(String status, String method) {
    switch (status) {
      case 'sent':
        return (Icons.check_circle, EllaColors.success, 'SMS sent');
      case 'queued':
        return (Icons.schedule, EllaColors.warning, 'Calling...');
      case 'delivered':
        return (Icons.check_circle, EllaColors.success, 'Delivered');
      case 'failed':
        return (Icons.error_outline, EllaColors.error, 'Retry...');
      default:
        return (Icons.schedule, EllaColors.textTertiary, status);
    }
  }
}
