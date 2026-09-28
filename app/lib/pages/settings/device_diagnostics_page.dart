import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Redacted, always-on BLE discovery diagnostics — never gated behind the
/// "Debug Logs" dev toggle, so testers can read scan behavior straight from
/// the app when a necklace doesn't show up during discovery. See
/// ellaaicare/ella-ai#1280 RUN-010 / #1287.
///
/// Shows only counts and booleans (scans started/stopped, candidates seen/
/// admitted/rejected-by-reason, hasAdvName/hasPeripheralName/uuidCount, and a
/// coarse RSSI bucket) — never device names, UUIDs, or MAC addresses.
class DeviceDiagnosticsPage extends StatefulWidget {
  const DeviceDiagnosticsPage({super.key});

  @override
  State<DeviceDiagnosticsPage> createState() => _DeviceDiagnosticsPageState();
}

class _DeviceDiagnosticsPageState extends State<DeviceDiagnosticsPage> {
  String _text() {
    final lines = DebugLogManager.deviceDiagnosticsBuffer;
    if (lines.isEmpty) {
      return DebugLogManager.deviceDiagnosticsSummaryText();
    }
    return '${DebugLogManager.deviceDiagnosticsSummaryText()}\n\n${lines.join('\n')}';
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
            onPressed: () => setState(() {}),
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
                DebugLogManager.deviceDiagnosticsSummaryText(),
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
