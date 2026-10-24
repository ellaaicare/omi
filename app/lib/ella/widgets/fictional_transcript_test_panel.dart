import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/fictional_transcript_local_load.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/fictional_transcript_run.dart';
import 'package:omi/ella/services/fictional_transcript_client.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

/// Internal QA build only. No calls on open or load; no production navigation.
class FictionalTranscriptTestPanel extends StatefulWidget {
  const FictionalTranscriptTestPanel({super.key,
    this.authorityProvider = WalOwnerAuthority.active, this.createRun = createFictionalTranscriptRun,
    this.authorityChanges});
  final Listenable? authorityChanges;
  final ActiveAccountAuthorityProvider authorityProvider;
  final Future<FictionalTranscriptRun> Function(FictionalTranscriptPlan, ExactAccountAuthorityVerifier) createRun;
  @override
  State<FictionalTranscriptTestPanel> createState() => _FictionalTranscriptTestPanelState();
}
class _FictionalTranscriptTestPanelState extends State<FictionalTranscriptTestPanel> {
  final _input = TextEditingController();
  EllaAccountCommitLease? _lease;
  FictionalTranscriptRun? _run;
  Timer? _watch;
  bool _busy = false;
  FictionalLocalLoadDiagnostic? _diagnostic;
  int _diagnosticGeneration = 0;
  late final Listenable _authorityChanges;
  @override
  void initState() {
    super.initState();
    _authorityChanges = widget.authorityChanges ?? SharedPreferencesUtil.aiConsentAuthorityChanges;
    _authorityChanges.addListener(_clearDiagnostic);
  }
  void _clearDiagnostic() {
    _diagnosticGeneration++;
    if (mounted) setState(() => _diagnostic = null);
  }
  String _status = 'No fictional test loaded. No server call has been made.';
  void _retire() {
    _diagnosticGeneration++; _diagnostic = null;
    _run?.retire(); _run = null; _lease?.close(); _lease = null;
    _watch?.cancel(); _watch = null; if (mounted) _input.clear();
    _status = 'Test scope retired. Server work is not cancelled by leaving this page.';
  }
  Future<void> _load() async {
    if (_busy || !fictionalProducerEnabled || _run != null) return;
    final generation = ++_diagnosticGeneration;
    final input = _input.text;
    // Hash the actual field value only on explicit Load. It is never persisted.
    final fingerprint = FictionalInputFingerprint(input);
    var failure = FictionalLocalLoadCode.inputTooLarge;
    setState(() { _busy = true; _diagnostic = null; });
    try {
      if (fingerprint.bytes > 65536) throw const FormatException();
      failure = FictionalLocalLoadCode.jsonInvalid;
      final value = jsonDecode(input);
      failure = FictionalLocalLoadCode.jsonObjectRequired;
      if (value is! Map<String, dynamic>) throw const FormatException();
      failure = FictionalLocalLoadCode.planInvalid;
      final plan = FictionalTranscriptPlan.fromJson(value);
      failure = FictionalLocalLoadCode.authorityUnavailable;
      final lease = EllaAccountCommitBarrier.begin(authorityProvider: widget.authorityProvider,
        onInvalidated: () { if (mounted) setState(_retire); });
      if (lease == null) throw StateError('Current signed-in consent required');
      _lease = lease;
      failure = FictionalLocalLoadCode.runCreationFailed;
      final run = await widget.createRun(plan, lease);
      if (!mounted || !lease.isCurrent) {
        run.retire();
        if (mounted) _retire();
        return;
      }
      _run = run; _input.clear(); _status = 'Fictional binding loaded locally; review the exact text before Prepare.';
      _diagnostic = generation == _diagnosticGeneration
        ? FictionalLocalLoadDiagnostic(FictionalLocalLoadCode.loadedLocally, fingerprint) : null;
      _watch = Timer.periodic(const Duration(milliseconds: 250), (_) {
        if (mounted && !run.valid) setState(_retire);
      });
    } catch (error) {
      final retired = _lease != null && !_lease!.isCurrent;
      final redact = generation != _diagnosticGeneration || retired;
      final code = retired ? FictionalLocalLoadCode.authorityRetired :
        error is FictionalLocalLoadException ? error.code : failure;
      _retire();
      if (mounted) {
        _status = 'Binding unavailable. No server request was made.';
        // An account transition must not republish a previous account's digest.
        _diagnostic = FictionalLocalLoadDiagnostic(code, redact ? null : fingerprint);
      }
    }
    finally { if (mounted) setState(() => _busy = false); }
  }
  Future<void> _act(Future<void> Function() action) async {
    if (_busy || _run?.valid != true) return;
    setState(() => _busy = true);
    try { await action(); }
    catch (_) { if (_run?.valid != true) _retire(); }
    finally { if (mounted) setState(() => _busy = false); }
  }
  @override
  void dispose() { _authorityChanges.removeListener(_clearDiagnostic); _retire(); _input.dispose(); super.dispose(); }
  @override
  Widget build(BuildContext context) {
    if (!fictionalProducerEnabled) return const SizedBox.shrink();
    final run = _run;
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(
      crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text('Internal fictional transcript test'),
        const Text('Separate backend window required. No microphone, personal recovery or Thin/Muse qualification.'),
        Text(_status),
        if (_diagnostic != null) Text(_diagnostic!.safeText, key: const Key('fictional-local-load-diagnostic')),
        if (run == null) ...[
          TextField(controller: _input, minLines: 2, maxLines: 6,
            decoration: const InputDecoration(labelText: 'Operator-prebound fictional plan JSON')),
          TextButton(onPressed: _busy ? null : _load, child: const Text('Load local binding')),
        ] else ...[
          SelectableText('Request ${run.plan.requestId}\nSource ${run.plan.sourceId}\nExpires ${run.plan.expiresAt.toLocal()}\n${run.plan.text}'),
          Text('State: ${run.phase.name}'),
          TextButton(key: const Key('fictional-prepare'),
            onPressed: !_busy && run.phase == FictionalPhase.idle ? () => _act(run.prepare) : null,
            child: const Text('Prepare this fictional source once')),
          TextButton(key: const Key('fictional-complete'),
            onPressed: !_busy && run.phase == FictionalPhase.prepared ? () => _act(run.complete) : null,
            child: const Text('Complete prepared source once')),
          TextButton(key: const Key('fictional-reconcile'),
            onPressed: !_busy && {FictionalPhase.stockCompleted, FictionalPhase.unknown}.contains(run.phase) ? () => _act(run.reconcile) : null,
            child: const Text('Read exact source')),
          const Text('A timeout is unknown, not cancelled. Do not repeat Prepare/Complete. Stock completion is not Hermes success.'),
          if (run.receipts.isNotEmpty) SelectableText(const JsonEncoder.withIndent('  ').convert(run.receipts)),
        ],
      ])));
  }
}
