import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/fictional_transcript_run.dart';
import 'package:omi/ella/widgets/fictional_transcript_test_panel.dart';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'package:omi/ella/capture_host/ella_capture_protocol_trace.dart';
import 'dart:async';
import 'dart:convert';
import 'package:omi/providers/message_provider.dart';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:omi/ella/services/diagnostic_presentation.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/effective_voice_settings_diagnostics.dart';
import 'package:omi/ella/widgets/effective_voice_diagnostics_panel.dart';
import 'package:omi/ella/widgets/journal_projection_diagnostics_panel.dart';
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
  String? _appBuild;

  Future<void> _loadBuild() async {
    try {
      final info = await PackageInfo.fromPlatform().timeout(const Duration(seconds: 3));
      final label = '${info.version}+${info.buildNumber}';
      if (mounted && RegExp(r'^\d+\.\d+\.\d+\+\d+$').hasMatch(label)) setState(() => _appBuild = label);
    } catch (_) {}
  }

  CaptureProvider? _capture;
  EllaNativeDiscoveryDiagnostics? _native;
  String _nativeRead = 'not_read';
  Future<EllaNativeDiscoveryDiagnostics> Function()? _snapshotLoader;
  bool _readingNative = false;
  int _nativeGeneration = 0;

  Future<void> _refreshNative() async {
    final loader = EllaCaptureHost.nativeDiscoveryDiagnosticsLoader;
    if (_readingNative || loader == null) return;
    _readingNative = true;
    final generation = _nativeGeneration;
    bool current() =>
        mounted &&
        generation == _nativeGeneration &&
        identical(loader, EllaCaptureHost.nativeDiscoveryDiagnosticsLoader);
    try {
      final value = await loader().timeout(const Duration(seconds: 2));
      if (current()) {
        setState(() {
          _native = value;
          _snapshotLoader = loader;
          _nativeRead = 'available';
        });
      }
    } catch (error) {
      if (current()) {
        setState(() {
          _native = null;
          _snapshotLoader = loader;
          _nativeRead = DebugLogManager.safeDeviceFailure(error);
        });
      }
    } finally {
      _readingNative = false;
    }
  }

  @override
  void initState() {
    super.initState();
    unawaited(_refreshNative());
    unawaited(_loadBuild());
    _refreshTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) {
        setState(() {});
        unawaited(_refreshNative());
      }
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
    _nativeGeneration++;
    _refreshTimer?.cancel();
    _capture?.removeMetricsListener();
    super.dispose();
  }

  String _time(DateTime? value) =>
      value == null ? context.l10n.unknown : diagnosticTimestamp(value, Localizations.localeOf(context).toString());

  String _utcTime(DateTime value) => _time(value);

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

  String _safeBluetoothState(String state) =>
      const {
        'unknown',
        'resetting',
        'unsupported',
        'unauthorized',
        'poweredOff',
        'poweredOn',
        'notCalled',
      }.contains(state)
      ? state
      : 'unknown';

  String _safeStatus(int? status) =>
      status != null && status >= 100 && status <= 599 ? status.toString() : context.l10n.unknown;

  @override
  Widget build(BuildContext context) {
    final nativeCurrent = identical(_snapshotLoader, EllaCaptureHost.nativeDiscoveryDiagnosticsLoader);
    final native = nativeCurrent ? _native : null;
    final nativeRead = nativeCurrent ? _nativeRead : 'retired';
    final palette = EllaPalette.of(context);
    final connectivity = ConnectivityService();
    final capture = context.watch<CaptureProvider>();
    final upstream = EllaCaptureHost.upstreamCaptureActive ? EllaCaptureHost.captureDiagnostics : null;
    final bleConnected = context.watch<DeviceProvider>().presentationIsConnected;
    return Scaffold(
      backgroundColor: palette.bgPrimary,
      appBar: AppBar(
        backgroundColor: palette.bgPrimary,
        foregroundColor: palette.textPrimary,
        elevation: 0,
        title: Text(context.l10n.ellaRuntimeDiagnostics),
      ),
      body: ValueListenableBuilder<AiConsentLeaseDiagnostics>(
        valueListenable: AiConsentActiveSessionLease.diagnostics,
        builder: (context, lease, _) {
          final backendProbe = connectivity.backendReachable == null
              ? context.l10n.unknown
              : connectivity.backendReachable!
              ? context.l10n.diagnosticsReachable(_safeStatus(connectivity.lastBackendProbeStatus))
              : '${context.l10n.diagnosticsNotReachable} · ${_safeCode(connectivity.lastBackendProbeError)}';
          final apiResult = ApiTransportDiagnostics.lastStatusCode == null
              ? _safeCode(ApiTransportDiagnostics.lastError)
              : _safeStatus(ApiTransportDiagnostics.lastStatusCode);
          final leaseDetail = <String>[
            'state=${lease.phase.name}',
            if (lease.retryableFailures > 0) 'retry_failures=${lease.retryableFailures.clamp(0, 1000000)}',
            if (lease.supportCode.isNotEmpty) 'support_code=${_safeCode(lease.supportCode)}',
            if (lease.terminalReason.isNotEmpty) 'terminal_reason=${_safeCode(lease.terminalReason)}',
          ].join('\n');
          final artwork = MemoryArtworkQueueDiagnostics.latest;
          final artworkRead = artwork?.read;
          final artworkDetail = artwork == null
              ? context.l10n.unknown
              : '${artworkRead?.message ?? context.l10n.unknown}\nhome=${artwork.projection.name}';
          return ListView(
            key: const Key('ella-runtime-diagnostics'),
            padding: EdgeInsets.fromLTRB(16, 16, 16, 16 + MediaQuery.paddingOf(context).bottom),
            children: [
              Text(
                context.l10n.diagnosticsSupportSubtitle,
                style: EllaTextStyles.secondary.copyWith(color: palette.textSecondary),
              ),
              const SizedBox(height: 8),
              Text(context.l10n.diagnosticsSupportSummary, style: TextStyle(color: palette.textSecondary)),
              const SizedBox(height: 16),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-app-build'),
                label: context.l10n.diagnosticsAppBuild,
                value: _appBuild ?? context.l10n.unknown,
              ),
              _DiagnosticRow(
                key: const Key('dictation-admission-diagnostic'),
                label: context.l10n.ellaDictationTitle,
                value: jsonEncode(ForegroundDictationDiagnostics.shared.snapshot),
              ),
              if (Provider.of<MessageProvider?>(context) case final history?)
                _DiagnosticRow(
                  key: const Key('history-admission-diagnostic'),
                  label: context.l10n.ellaHistoryAdmissionDiagnostic,
                  value: jsonEncode(history.historyAdmissionDiagnostic),
                ),
              if (fictionalProducerEnabled) const FictionalTranscriptTestPanel(),
              if (isEffectiveVoiceDiagnosticsEnabled) ...[
                const EffectiveVoiceDiagnosticsPanel(),
                const JournalProjectionDiagnosticsPanel(),
              ],
              _DiagnosticRow(
                label: context.l10n.diagnosticsNetwork,
                value: connectivity.isConnected
                    ? context.l10n.diagnosticsAvailable
                    : context.l10n.diagnosticsUnavailable,
              ),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-ella-server'),
                label: context.l10n.diagnosticsEllaServer,
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
                label: context.l10n.diagnosticsPermission,
                value: '$leaseDetail\nlast_confirmation=${_time(lease.lastConfirmedAt)}',
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
                label: upstream == null ? context.l10n.diagnosticsWsRate : context.l10n.diagnosticsTranscription,
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
              if (upstream?.audioHealth != null)
                _DiagnosticRow(
                  key: const Key('runtime-diagnostics-audio-health'),
                  label: context.l10n.diagnosticsCapturePipeline,
                  value: const JsonEncoder.withIndent('  ').convert(upstream!.audioHealth),
                  copyForSupport: true,
                ),
              if (upstream != null && upstream.protocolTrace.isNotEmpty)
                _DiagnosticRow(
                  key: const Key('runtime-diagnostics-capture-trace'),
                  label: context.l10n.diagnosticsSocketAdmission,
                  value: EllaCaptureProtocolTrace.supportText(upstream.protocolTrace),
                  copyForSupport: true,
                ),
              if (native != null)
                _DiagnosticRow(
                  key: const Key('runtime-diagnostics-native-discovery-lifetime'),
                  label: 'Native discovery lifetime totals',
                  value: 'rediscoveryForwarded=${native.nameArrivedLate}\n'
                    'retrievedConnected=${native.retrievedConnectedCount}\n'
                    'retrievedKnown=${native.retrievedKnownCount}\n'
                    'restored=${native.restoredCount}\n'
                    'nilBridgeDrops=${native.flutterApiNilDropCount}\n'
                    'suppressedDedup=not_observed',
                  copyForSupport: true,
                ),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-discovery-provenance'),
                label: 'Discovery row provenance',
                value: const JsonEncoder.withIndent('  ').convert(DebugLogManager.discoveryProvenance.snapshotFor(
                  (uid: SharedPreferencesUtil().uid, generation: SharedPreferencesUtil().aiConsentAuthorityGeneration))),
                copyForSupport: true,
              ),
              const SizedBox(height: 6),
              Text(
                context.l10n.diagnosticsDiscoveryTotals(
                  DebugLogManager.deviceCandidatesAdmitted.clamp(0, 1000000),
                  DebugLogManager.deviceCandidatesSeen.clamp(0, 1000000),
                ),
                style: TextStyle(color: palette.textPrimary),
              ),
              if (DebugLogManager.deviceDiscoveryStage == 'cancelled' &&
                  DebugLogManager.deviceDiscoveryFailure == 'none')
                Text(context.l10n.diagnosticsDiscoveryCancelled, style: TextStyle(color: palette.textSecondary)),
              _DiagnosticRow(
                key: const Key('runtime-diagnostics-device-counts'),
                label: context.l10n.deviceDiagnostics,
                copyForSupport: true,
                value:
                    ('scansStarted=${DebugLogManager.deviceScansStarted.clamp(0, 1000000)} '
                            'scansStopped=${DebugLogManager.deviceScansStopped.clamp(0, 1000000)}\n'
                            'candidatesSeen=${DebugLogManager.deviceCandidatesSeen.clamp(0, 1000000)} '
                            'candidatesAdmitted=${DebugLogManager.deviceCandidatesAdmitted.clamp(0, 1000000)}\n'
                            'picker=${DebugLogManager.deviceDiscoveryStage} '
                            'failure=${DebugLogManager.deviceDiscoveryFailure}\n'
                            'permission=${DebugLogManager.deviceBluetoothPermission}\n'
                            'adapter=${DebugLogManager.deviceAdapterState} '
                            'adapterFailure=${DebugLogManager.deviceAdapterFailure}\n'
                            'bridgeRegistered=${DebugLogManager.bleFlutterApiSetUpAtMs != null}\n'
                            'nativeRead=$nativeRead '
                            'nativeScanInvoked=${native == null ? context.l10n.unknown : native.scansStartedImmediately + native.scansQueued > 0}\n'
                            'nativeState=${native == null ? context.l10n.unknown : _safeBluetoothState(native.lastStartScanCbState)}')
                        .replaceAll(RegExp(r' (?=[a-zA-Z][a-zA-Z0-9_]*=)'), '\n'),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DiagnosticRow extends StatelessWidget {
  const _DiagnosticRow({super.key, required this.label, required this.value, this.copyForSupport = false});

  final String label;
  final String value;
  final bool copyForSupport;

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
            Text(
              label,
              style: EllaTextStyles.body.copyWith(
                fontWeight: FontWeight.w700,
                color: EllaPalette.of(context).textPrimary,
              ),
            ),
            const SizedBox(height: 4),
            SelectableText(
              value,
              style: EllaTextStyles.caption.copyWith(
                fontFamily: 'monospace',
                color: EllaPalette.of(context).textSecondary,
              ),
            ),
            if (copyForSupport)
              TextButton.icon(
                style: TextButton.styleFrom(
                  foregroundColor: EllaPalette.of(context).textPrimary,
                  minimumSize: const Size(48, 48),
                ),
                onPressed: () => Clipboard.setData(ClipboardData(text: value)),
                icon: const Icon(Icons.copy),
                label: Text(context.l10n.diagnosticsCopyForSupport),
              ),
          ],
        ),
      ),
    ),
  );
}
