import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Redacted, always-on BLE discovery diagnostics — never gated behind the
/// "Debug Logs" dev toggle, so testers can read scan behavior straight from
/// the app when a necklace doesn't show up during discovery. See
/// ellaaicare/ella-ai#1280 RUN-010 / #1287.
///
/// Surfaces all three layers of the discovery pipeline together so a single
/// copy/paste is enough to diagnose a one-run failure:
///  - native: CoreBluetooth state at the last startScan call, started-vs-
///    queued outcome, the native didDiscover count, and the flutterApi-nil
///    drop count (a nil flutterApi silently swallows a discovery);
///  - bridge: when BleFlutterApi.setUp(BleBridge.instance) last ran;
///  - Dart: candidates seen/admitted/rejected-by-reason (as before).
/// Never device names, UUIDs, or MAC addresses.
///
/// This page is reachable from the flag-OFF legacy entry point, so it must
/// never import `lib/upstream_capture/**`; the native layer is read through
/// [EllaCaptureHost.nativeDiscoveryDiagnosticsLoader], which the flag-ON graph
/// installs (null when that graph isn't active).
class DeviceDiagnosticsPage extends StatefulWidget {
  const DeviceDiagnosticsPage({super.key, this.nativeDiagnosticsLoader});

  /// Injectable for tests; production reads [EllaCaptureHost]'s loader.
  final Future<EllaNativeDiscoveryDiagnostics> Function()? nativeDiagnosticsLoader;

  @override
  State<DeviceDiagnosticsPage> createState() => _DeviceDiagnosticsPageState();
}

class _DeviceDiagnosticsPageState extends State<DeviceDiagnosticsPage> {
  EllaNativeDiscoveryDiagnostics? _native;
  Object? _nativeError;

  @override
  void initState() {
    super.initState();
    _loadNative();
  }

  Future<void> _loadNative() async {
    final loader = widget.nativeDiagnosticsLoader ?? EllaCaptureHost.nativeDiscoveryDiagnosticsLoader;
    if (loader == null) {
      if (!mounted) return;
      setState(() {
        _native = null;
        _nativeError = StateError('upstream capture is not active');
      });
      return;
    }
    try {
      final native = await loader();
      if (!mounted) return;
      setState(() {
        _native = native;
        _nativeError = null;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _native = null;
        _nativeError = error;
      });
    }
  }

  String _nativeSummaryText() {
    final native = _native;
    if (native == null) {
      return 'native: unavailable${_nativeError != null ? ' (${_nativeError.runtimeType})' : ''}';
    }
    return 'native: cbStateAtLastStartScan=${native.lastStartScanCbState} '
        'scansStartedImmediately=${native.scansStartedImmediately} scansQueued=${native.scansQueued} '
        'queuedScansFired=${native.queuedScansFired} didDiscoverCount=${native.didDiscoverCount} '
        'flutterApiNilDropCount=${native.flutterApiNilDropCount}';
  }

  String _bridgeSummaryText() {
    final setUpAtMs = DebugLogManager.bleFlutterApiSetUpAtMs;
    final setUpAt =
        setUpAtMs == null ? 'never' : DateTime.fromMillisecondsSinceEpoch(setUpAtMs, isUtc: true).toIso8601String();
    return 'bridge: setUpAt=$setUpAt';
  }

  String _dartSummaryText() => 'dart: ${DebugLogManager.deviceDiagnosticsSummaryText()}';

  String _summaryText() => '${_nativeSummaryText()}\n${_bridgeSummaryText()}\n${_dartSummaryText()}';

  String _text() {
    final lines = DebugLogManager.deviceDiagnosticsBuffer;
    if (lines.isEmpty) {
      return _summaryText();
    }
    return '${_summaryText()}\n\n${lines.join('\n')}';
  }

  Future<void> _refresh() async {
    await _loadNative();
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final lines = DebugLogManager.deviceDiagnosticsBuffer;
    return Scaffold(
      backgroundColor: const Color(0xFF0D0D0D),
      appBar: AppBar(
        backgroundColor: const Color(0xFF0D0D0D),
        elevation: 0,
        leading: IconButton(
          icon: const FaIcon(FontAwesomeIcons.chevronLeft, size: 18),
          onPressed: () => Navigator.of(context).pop(),
        ),
        title: Text(
          context.l10n.deviceDiagnostics,
          style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 18),
        ),
        centerTitle: true,
        actions: [
          IconButton(
            icon: const FaIcon(FontAwesomeIcons.arrowsRotate, size: 16),
            onPressed: () => _refresh(),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
              child: Text(
                _summaryText(),
                style: const TextStyle(color: Colors.white, fontFamily: 'Ubuntu Mono', fontSize: 13),
              ),
            ),
            const SizedBox(height: 12),
            Expanded(
              child: lines.isEmpty
                  ? Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text(
                          context.l10n.noDiagnosticsYet,
                          textAlign: TextAlign.center,
                          style: TextStyle(color: Colors.grey.shade500, fontSize: 14),
                        ),
                      ),
                    )
                  : Container(
                      margin: const EdgeInsets.symmetric(horizontal: 20),
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: const Color(0xFF1C1C1E),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: ListView.builder(
                        itemCount: lines.length,
                        itemBuilder: (context, index) => Padding(
                          padding: const EdgeInsets.symmetric(vertical: 2),
                          child: Text(
                            lines[index],
                            style: TextStyle(color: Colors.grey.shade300, fontFamily: 'Ubuntu Mono', fontSize: 12),
                          ),
                        ),
                      ),
                    ),
            ),
            Padding(
              padding: const EdgeInsets.all(20),
              child: GestureDetector(
                onTap: () {
                  Clipboard.setData(ClipboardData(text: _text()));
                  AppSnackbar.showSnackbar(context.l10n.labelCopied(context.l10n.deviceDiagnostics));
                },
                child: Container(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  decoration: BoxDecoration(
                    color: const Color(0xFF2A2A2E),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      FaIcon(FontAwesomeIcons.copy, color: Colors.grey.shade300, size: 14),
                      const SizedBox(width: 8),
                      Text(
                        context.l10n.copyDiagnostics,
                        style: TextStyle(color: Colors.grey.shade300, fontSize: 14, fontWeight: FontWeight.w500),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
