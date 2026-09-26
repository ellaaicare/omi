import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
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

  String _transcriptionSocketState(String state) => switch (state) {
        'connected' => context.l10n.connected,
        'disconnected' => context.l10n.disconnected,
        _ => context.l10n.unknown,
      };

  @override
  Widget build(BuildContext context) {
    final connectivity = ConnectivityService();
    final capture = context.watch<CaptureProvider>();
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
                  ? '${context.l10n.connected} · ${connectivity.lastBackendProbeStatus ?? '-'}'
                  : '${context.l10n.disconnected} · ${connectivity.lastBackendProbeError}';
          final apiResult = ApiTransportDiagnostics.lastStatusCode?.toString() ??
              (ApiTransportDiagnostics.lastError.isEmpty ? context.l10n.unknown : ApiTransportDiagnostics.lastError);
          final leaseDetail = <String>[
            lease.phase.name,
            if (lease.retryableFailures > 0) 'retry ${lease.retryableFailures}',
            if (lease.supportCode.isNotEmpty) lease.supportCode,
            if (lease.terminalReason.isNotEmpty) lease.terminalReason,
          ].join(' · ');
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
                label: context.l10n.diagnosticsConsentLease,
                value: '$leaseDetail\n${_time(lease.lastConfirmedAt)}',
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsBleRate,
                value:
                    '${bleConnected ? context.l10n.yes : context.l10n.no} · ${capture.bleBytesPerSecond.toStringAsFixed(0)} B/s',
              ),
              _DiagnosticRow(
                label: context.l10n.diagnosticsWsRate,
                value:
                    '${_transcriptionSocketState(capture.transcriptionSocketState)} · ${capture.wsSendRateKbps.toStringAsFixed(2)} kbps',
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DiagnosticRow extends StatelessWidget {
  const _DiagnosticRow({required this.label, required this.value});

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
