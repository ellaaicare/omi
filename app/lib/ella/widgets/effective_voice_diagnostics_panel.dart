import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:omi/ella/services/diagnostic_presentation.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'package:omi/ella/services/effective_voice_settings_diagnostics.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EffectiveVoiceDiagnosticsPanel extends StatefulWidget {
  const EffectiveVoiceDiagnosticsPanel({super.key, this.api, this.authorityChanges, this.buildLabel});

  final EffectiveVoiceSettingsDiagnostics? api;
  @visibleForTesting
  final Stream<void>? authorityChanges;
  @visibleForTesting
  final Future<String> Function()? buildLabel;

  @override
  State<EffectiveVoiceDiagnosticsPanel> createState() => _EffectiveVoiceDiagnosticsPanelState();
}

class _EffectiveVoiceDiagnosticsPanelState extends State<EffectiveVoiceDiagnosticsPanel> {
  StreamSubscription<void>? _authoritySubscription;
  Timer? _authorityTimer;
  EffectiveVoiceDiagnosticsResult? _result;
  String? _build;
  int _generation = 0;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    if (!isEffectiveVoiceDiagnosticsEnabled) return;
    // Pure local invalidation, never a settings poll. Same-UID consent or
    // provisioning retirement need not emit a Firebase auth event.
    _authorityTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      final snapshot = _result?.snapshot;
      if (snapshot != null && !snapshot.isCurrent) _clearForAuthorityChange();
    });
    final changes = widget.authorityChanges ?? FirebaseAuth.instance.authStateChanges().map<void>((_) {});
    _authoritySubscription = changes.listen((_) => _clearForAuthorityChange());
  }

  void _clearForAuthorityChange() {
    if (!mounted) return;
    _generation++;
    setState(() {
      _result = null;
      _build = null;
      _busy = false;
    });
  }

  Future<void> _read() async {
    if (_busy || !isEffectiveVoiceDiagnosticsEnabled) return;
    final generation = ++_generation;
    setState(() {
      _busy = true;
      _result = null;
      _build = null;
    });
    String? build;
    try {
      if (widget.buildLabel != null) {
        build = await widget.buildLabel!().timeout(const Duration(seconds: 3));
      } else {
        final info = await PackageInfo.fromPlatform().timeout(const Duration(seconds: 3));
        build = '${info.version}+${info.buildNumber}';
      }
    } catch (_) {}
    if (!mounted || generation != _generation) return;
    final result = await (widget.api ?? EffectiveVoiceSettingsDiagnostics()).read();
    if (!mounted || generation != _generation) return;
    setState(() {
      _busy = false;
      _result = result;
      _build = build != null && RegExp(r'^\d+\.\d+\.\d+\+\d+$').hasMatch(build) ? build : null;
    });
  }

  @override
  void dispose() {
    _generation++;
    _authorityTimer?.cancel();
    _authoritySubscription?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!isEffectiveVoiceDiagnosticsEnabled) return const SizedBox.shrink();
    final snapshot = _result?.snapshot;
    final fields = snapshot?.isCurrent == true
        ? <String, Object>{
            ...snapshot!.fields,
            'app_build': _build ?? context.l10n.unknown,
            'platform': defaultTargetPlatform.name
          }
        : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        OutlinedButton(
          key: const Key('effective-voice-diagnostics-read'),
          style: OutlinedButton.styleFrom(
            foregroundColor: EllaPalette.of(context).textPrimary,
            disabledForegroundColor: EllaPalette.of(context).textSecondary,
            minimumSize: const Size(48, 48),
            shape: const StadiumBorder(),
            side: BorderSide(color: EllaPalette.of(context).textSecondary),
          ),
          onPressed: _busy ? null : _read,
          child: Text(context.l10n.diagnosticsRefreshVoiceSettings),
        ),
        if (fields != null)
          SelectableText(const JsonEncoder.withIndent('  ').convert(fields),
              style: TextStyle(fontFamily: 'monospace', color: EllaPalette.of(context).textPrimary),
              key: const Key('effective-voice-diagnostics-result'))
        else if (_result != null)
          SelectableText(
            jsonEncode({
              'result': _result!.failure?.name ?? EffectiveVoiceDiagnosticsFailure.authorityChanged.name,
              'http_status': _result!.httpStatus ?? context.l10n.unknown,
            }),
            key: const Key('effective-voice-diagnostics-unavailable'),
          ),
        if (fields != null)
          TextButton.icon(
            key: const Key('effective-voice-copy-support'),
            style: TextButton.styleFrom(
                foregroundColor: EllaPalette.of(context).textPrimary, minimumSize: const Size(48, 48)),
            onPressed: () {
              if (snapshot?.isCurrent != true) return;
              Clipboard.setData(
                  ClipboardData(text: const JsonEncoder.withIndent('  ').convert(supportVoiceFields(fields))));
            },
            icon: const Icon(Icons.copy),
            label: Text(context.l10n.diagnosticsCopyForSupport),
          ),
        const SizedBox(height: 16),
      ],
    );
  }
}
