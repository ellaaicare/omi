import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

class CaptureDiagnosticsPanel extends StatelessWidget {
  const CaptureDiagnosticsPanel({super.key, required this.diagnostics});

  final CaptureDiagnostics diagnostics;

  String _phaseLabel(BuildContext context) => switch (diagnostics.phase) {
        CaptureDiagnosticPhase.idle => context.l10n.statusPending,
        CaptureDiagnosticPhase.checkingPermission ||
        CaptureDiagnosticPhase.waitingForAccount ||
        CaptureDiagnosticPhase.connectingTranscription ||
        CaptureDiagnosticPhase.startingCapture =>
          context.l10n.initializing,
        CaptureDiagnosticPhase.waitingForAudio => context.l10n.waitingForDevice,
        CaptureDiagnosticPhase.streaming => context.l10n.recordingActive,
        CaptureDiagnosticPhase.receivingTranscript => context.l10n.transcriptReceived,
        CaptureDiagnosticPhase.stopping || CaptureDiagnosticPhase.finalizing => context.l10n.processing,
        CaptureDiagnosticPhase.completed => context.l10n.completed,
        CaptureDiagnosticPhase.disconnected => context.l10n.disconnected,
        CaptureDiagnosticPhase.failed => context.l10n.failedStatus,
      };

  String? _failureLabel(BuildContext context) => switch (diagnostics.failure) {
        CaptureDiagnosticFailure.none => null,
        CaptureDiagnosticFailure.microphonePermissionDenied => context.l10n.todayMicrophonePermissionDenied,
        CaptureDiagnosticFailure.transcriptionUnavailable => context.l10n.todayTranscriptionUnavailable,
        CaptureDiagnosticFailure.necklaceAudioSubscriptionUnavailable =>
          context.l10n.todayNecklaceAudioSubscriptionUnavailable,
        CaptureDiagnosticFailure.physicalAudioUnavailable => context.l10n.todayNecklaceAudioSilent,
        CaptureDiagnosticFailure.necklaceConnectionUnavailable => context.l10n.todayNecklaceConnectionUnavailable,
        CaptureDiagnosticFailure.socketClosed ||
        CaptureDiagnosticFailure.socketError =>
          context.l10n.todayTranscriptionUnavailable,
        CaptureDiagnosticFailure.noTranscript => context.l10n.todayNoWordsCaptured,
        CaptureDiagnosticFailure.finalizationFailed => context.l10n.processingFailed,
        CaptureDiagnosticFailure.consentUnavailable ||
        CaptureDiagnosticFailure.accountNotReady ||
        CaptureDiagnosticFailure.recorderUnavailable ||
        CaptureDiagnosticFailure.deviceDisconnected =>
          context.l10n.todayRecordingUnavailable,
      };

  @override
  Widget build(BuildContext context) {
    final failure = _failureLabel(context);
    final memoryStatus = switch (diagnostics.phase) {
      CaptureDiagnosticPhase.finalizing => context.l10n.statusProcessing,
      CaptureDiagnosticPhase.completed => context.l10n.statusCompleted,
      CaptureDiagnosticPhase.failed when diagnostics.physicalFrames > 0 => context.l10n.statusFailed,
      _ => context.l10n.statusPending,
    };
    return Semantics(
      container: true,
      child: EllaCardSurface(
        borderRadius: 14,
        child: Padding(
          key: const Key('device-capture-diagnostics-panel'),
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Icon(Icons.query_stats_rounded, size: 20, color: EllaColors.tealDeep),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      context.l10n.transcriptionDiagnostics,
                      style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w700),
                    ),
                  ),
                  Text(_phaseLabel(context), style: EllaTextStyles.caption),
                ],
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 12,
                runSpacing: 6,
                children: [
                  Text(
                    '${context.l10n.audioBytes}: ${diagnostics.physicalBytes} · ${diagnostics.physicalFrames}',
                    key: const Key('device-capture-audio-proof'),
                    style: EllaTextStyles.caption,
                  ),
                  Text(
                    '${context.l10n.transcription}: ${diagnostics.transmittedFrames}',
                    key: const Key('device-capture-delivery-proof'),
                    style: EllaTextStyles.caption,
                  ),
                  Text(
                    context.l10n.segmentsCount(diagnostics.transcriptSegments),
                    key: const Key('device-capture-transcript-proof'),
                    style: EllaTextStyles.caption,
                  ),
                  Text(
                    '${context.l10n.statusLabel}: $memoryStatus'
                    '${diagnostics.finalizationAttempts > 0 ? ' · ${diagnostics.finalizationAttempts}' : ''}',
                    key: const Key('device-capture-memory-proof'),
                    style: EllaTextStyles.caption,
                  ),
                ],
              ),
              if (diagnostics.latestTranscript.trim().isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(
                  '${context.l10n.transcript}: ${diagnostics.latestTranscript.trim()}',
                  key: const Key('device-capture-latest-transcript'),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: EllaTextStyles.caption.copyWith(color: EllaColors.tealDeep),
                ),
              ],
              if (failure != null) ...[
                const SizedBox(height: 8),
                Text(
                  failure,
                  key: const Key('device-capture-failure-proof'),
                  style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft, fontWeight: FontWeight.w700),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
