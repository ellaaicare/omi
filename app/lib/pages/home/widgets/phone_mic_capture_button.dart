import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/platform/platform_service.dart';

class PhoneMicCaptureButton extends StatefulWidget {
  const PhoneMicCaptureButton({super.key});

  @override
  State<PhoneMicCaptureButton> createState() => _PhoneMicCaptureButtonState();
}

class _PhoneMicCaptureButtonState extends State<PhoneMicCaptureButton> {
  bool _busy = false;

  Future<void> _togglePhoneMic(CaptureProvider captureProvider) async {
    if (_busy || !captureProvider.canUsePhoneMicCapture) {
      return;
    }

    setState(() => _busy = true);
    HapticFeedback.mediumImpact();

    try {
      if (captureProvider.isPhoneMicRecording) {
        await captureProvider.stopStreamRecording();
        MixpanelManager().phoneMicRecordingStopped();
      } else {
        await captureProvider.streamRecording();
        if (captureProvider.recordingState == RecordingState.record) {
          MixpanelManager().phoneMicRecordingStarted();
        }
      }
    } catch (e) {
      if (!mounted) return;
      AppSnackbar.showSnackbarError(context.l10n.captureRecordingError(e.toString()));
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!PlatformService.isMobile) {
      return const SizedBox.shrink();
    }

    return Selector<CaptureProvider, (RecordingState, bool, bool)>(
      selector: (_, provider) =>
          (provider.recordingState, provider.isPhoneMicRecording, provider.canUsePhoneMicCapture),
      builder: (context, data, child) {
        final (recordingState, isPhoneMicRecording, canUsePhoneMicCapture) = data;
        final isInitialising = recordingState == RecordingState.initialising;
        final enabled = !_busy && canUsePhoneMicCapture && !isInitialising;
        final active = isPhoneMicRecording && recordingState == RecordingState.record;
        final tooltip = active
            ? context.l10n.stopRecording
            : '${context.l10n.startRecording}: ${context.l10n.phone} ${context.l10n.mic}';

        return Tooltip(
          message: tooltip,
          child: Semantics(
            button: true,
            enabled: enabled,
            label: tooltip,
            child: GestureDetector(
              onTap: enabled ? () => _togglePhoneMic(context.read<CaptureProvider>()) : null,
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 180),
                width: 36,
                height: 36,
                decoration: BoxDecoration(
                  color: enabled
                      ? active
                          ? EllaColors.error
                          : EllaColors.primary
                      : EllaColors.bgTertiary,
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: enabled ? Colors.transparent : EllaColors.textDisabled.withValues(alpha: 0.35),
                  ),
                ),
                child: Center(
                  child: _busy || isInitialising
                      ? const SizedBox(
                          width: 15,
                          height: 15,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white,
                          ),
                        )
                      : FaIcon(
                          active
                              ? FontAwesomeIcons.stop
                              : enabled
                                  ? FontAwesomeIcons.microphone
                                  : FontAwesomeIcons.microphoneSlash,
                          color: enabled ? Colors.white : EllaColors.textDisabled,
                          size: 15,
                        ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}
