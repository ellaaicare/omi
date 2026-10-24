import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/journal_projection_diagnostics.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/l10n_extensions.dart';

class JournalProjectionDiagnosticsPanel extends StatefulWidget {
  const JournalProjectionDiagnosticsPanel(
      {super.key,
      this.authorityProvider = WalOwnerAuthority.activeAccount,
      this.reader = readJournalProjectionDiagnostics});
  final ActiveAccountAuthorityProvider authorityProvider;
  final Future<Map<String, Object?>> Function(ExactAccountAuthorityVerifier) reader;
  @override
  State<JournalProjectionDiagnosticsPanel> createState() => _JournalProjectionDiagnosticsPanelState();
}

class _JournalProjectionDiagnosticsPanelState extends State<JournalProjectionDiagnosticsPanel> {
  EllaAccountCommitLease? _lease;
  Timer? _watch;
  Map<String, Object?>? _receipt;
  bool _busy = false;
  int _generation = 0;
  void _retire() {
    _generation++;
    _watch?.cancel();
    _watch = null;
    _lease?.close();
    _lease = null;
    _receipt = null;
    _busy = false;
  }

  Future<void> _read() async {
    if (_busy) return;
    _retire();
    final lease = EllaAccountCommitBarrier.begin(
        authorityProvider: widget.authorityProvider,
        onInvalidated: () {
          if (mounted) setState(_retire);
        });
    if (lease == null) {
      setState(() {});
      return;
    }
    _lease = lease;
    final generation = _generation;
    bool current() => mounted && generation == _generation && lease.isCurrent;
    _watch = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!lease.isCurrent && mounted) setState(_retire);
    });
    setState(() => _busy = true);
    try {
      final receipt = await widget.reader(lease);
      if (current()) setState(() => _receipt = receipt);
    } catch (_) {
      if (current()) setState(() => _receipt = {'result': 'read_failed'});
    } finally {
      if (current()) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _retire();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => EllaCardSurface(
      child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(context.l10n.irisJournal, style: EllaTextStyles.body),
            const SelectableText(journalDiagnosticConversationId),
            TextButton.icon(
                key: const Key('journal-synthetic-projection-read'),
                onPressed: _busy ? null : _read,
                icon: const Icon(Icons.refresh),
                label: Text(_busy ? context.l10n.loading : context.l10n.refresh)),
            if (_lease?.isCurrent == true && _receipt != null)
              SelectableText(const JsonEncoder.withIndent('  ').convert(_receipt),
                  key: const Key('journal-synthetic-projection-receipt'), style: EllaTextStyles.caption),
          ])));
}
