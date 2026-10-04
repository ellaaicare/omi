import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaRuntimeDiagnosticsPage extends StatefulWidget {
  const EllaRuntimeDiagnosticsPage({super.key});

  @override
  State<EllaRuntimeDiagnosticsPage> createState() => _EllaRuntimeDiagnosticsPageState();
}

class _EllaRuntimeDiagnosticsPageState extends State<EllaRuntimeDiagnosticsPage> {
  Timer? _refreshTimer;
  CaptureProvider? _capture;

  @override
  void initState() {
    super.initState();
    _refreshTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (EllaCaptureHost.upstreamCaptureActive) return;
    final capture = context.read<CaptureProvider>();
    if (identical(capture, _capture)) return;
    _capture?.removeMetricsListener();
    _capture = capture;
    capture.addMetricsListener();
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _capture?.removeMetricsListener();
    super.dispose();
  }

  String _time(DateTime? value) => value?.toLocal().toIso8601String() ?? context.l10n.unknown;

  String _utcTime(DateTime value) => value.toUtc().toIso8601String();

  String _transcriptionSocketState(String state) => switch (state) {
    'connected' => context.l10n.connected,
    'disconnected' => context.l10n.disconnected,
    _ => context.l10n.unknown,
  };

  String _safeCode(String code) {
    const known = {
      'timeout',
      'no_network_interface',
      'refresh_exception',
      'same_uid_rollover',
      'transport_exception',
      'authority_superseded',
      'subject_mismatch',
      'grant_not_explicitly_terminal',
      'consent_policy_mismatch',
      'account_epoch_unavailable',
      'consent_grant_not_current',
      'explicit_not_accepted',
      'explicit_local_revoke',
      'persisted_authority_invalid',
      'explicit_deleted',
      'explicit_notAccepted',
      'explicit_reconsentRequired',
    };
    if (known.contains(code)) return code;
    final http = RegExp(r'^http_([1-5][0-9]{2})$').firstMatch(code);
    return http == null ? context.l10n.unknown : 'http_${http.group(1)}';
  }

  String _safeStatus(int? status) =>
      status != null && status >= 100 && status <= 599 ? status.toString() : context.l10n.unknown;

  @override
  Widget build(BuildContext context) {
    final connectivity = ConnectivityService();
    final capture = context.watch<CaptureProvider>();
    final upstream = EllaCaptureHost.upstreamCaptureActive ? EllaCaptureHost.captureDiagnostics : null;
    final bleConnected = context.watch<DeviceProvider>().presentationIsConnected;
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        title: Text(context.l10n.ellaRuntimeDiagnostics),
      ),
      body: ValueListenableBuilder<AiConsentLeaseDiagnostics>(
        valueListenable: AiConsentActiveSessionLease.diagnostics,
        builder: (context, lease, _) {
          final backendProbe = connectivity.backendReachable == null
              ? context.l10n.unknown
              : connectivity.backendReachable!
              ? '${context.l10n.connected} · ${_safeStatus(connectivity.lastBackendProbeStatus)}'
              : '${context.l10n.disconnected} · ${_safeCode(connectivity.lastBackendProbeError)}';
          final apiResult = ApiTransportDiagnostics.lastStatusCode == null
              ? _safeCode(ApiTransportDiagnostics.lastError)
              : _safeStatus(ApiTransportDiagnostics.lastStatusCode);
          final leaseDetail = <String>[
            lease.phase.name,
            if (lease.retryableFailures > 0) 'retry ${lease.retryableFailures.clamp(0, 1000000)}',
            if (lease.supportCode.isNotEmpty) _safeCode(lease.supportCode),
            if (lease.terminalReason.isNotEmpty) _safeCode(lease.terminalReason),
          ].join(' · ');
          final artwork = MemoryArtworkQueueDiagnostics.latest;
          final artworkRead = artwork?.read;
          final artworkDetail = artwork == null
              ? context.l10n.unknown
              : '${artworkRead?.message ?? context.l10n.unknown}\nhome=${artwork.projection.name}';
          return ListView(
            key: const Key('ella-runtime-diagnostics'),
            padding: const EdgeInsets.all(16),
            children: [
              Text(context.l10n.ellaRuntimeDiagnosticsSubtitle, style: EllaTextStyles.secondary),
              const SizedBox(height: 16),
              _DiagnosticRow(
                label: context.l10n.diagnosticsNetworkInterface,
                value: connectivity.isConnected ? context.l10n.yes : context.l10n.no,
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsBackendProbe,
                value: '$backendProbe\n${_time(connectivity.lastBackendProbeAt)}',
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsLastApi,
                value: '$apiResult\n${_time(ApiTransportDiagnostics.lastAttemptAt)}',
              ),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-artwork-read'),
                label: context.l10n.memoryArtworkStudio,
                value: artworkDetail,
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsConsentLease,
                value: '$leaseDetail\n${_time(lease.lastConfirmedAt)}',
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsCapturePipeline,
                value: upstream == null
                    ? context.l10n.diagnosticsLegacyCapture
                    : upstream.initialized
                    ? context.l10n.diagnosticsUpstreamCapture
                    : context.l10n.diagnosticsCaptureUninitialized,
              ),
              _DiagnosticRow(
                label: upstream == null ? context.l10n.diagnosticsBleRate : context.l10n.diagnosticsAudioReceived,
                value: upstream == null
                    ? '${bleConnected ? context.l10n.yes : context.l10n.no} · ${capture.bleBytesPerSecond.toStringAsFixed(0)} B/s'
                    : '${bleConnected ? context.l10n.yes : context.l10n.no} · ${upstream.receivedBytes ?? context.l10n.unknown} B',
              ),
              _DiagnosticRow(
                label: upstream == null ? context.l10n.diagnosticsWsRate : context.l10n.diagnosticsAudioSent,
                value: upstream == null
                    ? '${_transcriptionSocketState(capture.transcriptionSocketState)} · ${capture.wsSendRateKbps.toStringAsFixed(2)} kbps'
                    : '${upstream.initialized ? (upstream.ready ? context.l10n.connected : context.l10n.disconnected) : context.l10n.unknown} · ${upstream.sentBytes ?? context.l10n.unknown} B',
              ),
              if (upstream != null)
                _DiagnosticRow(
                  label: context.l10n.diagnosticsSocketAdmission,
                  value: upstream.lastAttempt != null
                      ? '${upstream.lastAttempt!.phase.code} · ${upstream.lastAttempt!.status.name} · ${upstream.lastAttempt!.closeCode ?? context.l10n.unknown}\n${_utcTime(upstream.lastAttempt!.at)}'
                      : upstream.lastFailure == null
                      ? context.l10n.unknown
                      : '${upstream.lastFailure!.reason.code} · ${upstream.lastFailure!.closeCode ?? '-'}\n${_utcTime(upstream.lastFailure!.at)}',
                ),
              const SizedBox(height: 6),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-device-counts'),
                label: context.l10n.deviceDiagnostics,
                value:
                    'scansStarted=${DebugLogManager.deviceScansStarted.clamp(0, 1000000)} '
                    'scansStopped=${DebugLogManager.deviceScansStopped.clamp(0, 1000000)}\n'
                    'candidatesSeen=${DebugLogManager.deviceCandidatesSeen.clamp(0, 1000000)} '
                    'candidatesAdmitted=${DebugLogManager.deviceCandidatesAdmitted.clamp(0, 1000000)}',
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DiagnosticRow extends StatelessWidget {
  const _DiagnosticRow({super.key, required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 10),
    child: EllaCardSurface(
      borderRadius: 14,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label, style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 4),
            SelectableText(value, style: EllaTextStyles.caption),
          ],
        ),
      ),
    ),
  );
}
