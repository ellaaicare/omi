import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_workspace_status.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaWorkspacePage extends StatelessWidget {
  const EllaWorkspacePage({super.key, this.statusOverride});

  final EllaWorkspaceStatus? statusOverride;

  @override
  Widget build(BuildContext context) {
    final status = statusOverride ?? EllaWorkspaceStatus.current();
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        foregroundColor: EllaColors.textPrimary,
        title: Text(context.l10n.ellaWorkspaceTitle),
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          Text(context.l10n.ellaWorkspacePrivacyDescription, style: const TextStyle(color: EllaColors.textSecondary)),
          const SizedBox(height: 20),
          _valueRow(context.l10n.ellaWorkspaceAccount, status.email),
          _valueRow(
            context.l10n.ellaWorkspacePrivateHermes,
            status.workspaceVerified ? context.l10n.ellaWorkspaceVerified : context.l10n.ellaWorkspaceNotVerified,
          ),
          _valueRow(
            context.l10n.ellaWorkspaceFingerprint,
            status.workspaceFingerprint.isEmpty ? context.l10n.ellaWorkspaceNotVerified : status.workspaceFingerprint,
          ),
          _valueRow(
            context.l10n.ellaWorkspaceBindingRevision,
            status.bindingRevision == 0 ? context.l10n.ellaWorkspaceNotVerified : status.bindingRevision.toString(),
          ),
          _valueRow(
            context.l10n.ellaWorkspaceLastVerified,
            status.lastVerifiedAt == null
                ? context.l10n.ellaWorkspaceNotVerified
                : _formatTimestamp(context, status.lastVerifiedAt!.toLocal()),
          ),
          _valueRow(
            context.l10n.ellaWorkspaceProtectedAudio,
            status.quarantinedAudioCount.toString(),
          ),
          if (status.quarantinedAudioCount > 0)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(
                context.l10n.ellaWorkspaceProtectedAudioExplanation,
                style: const TextStyle(color: EllaColors.textTertiary),
              ),
            ),
          const SizedBox(height: 24),
          Text(
            context.l10n.ellaWorkspaceRoutes,
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
          ),
          const SizedBox(height: 8),
          _routeRow(context, context.l10n.ellaWorkspaceChatRoute, status.chat),
          _routeRow(context, context.l10n.ellaWorkspaceVoiceRoute, status.voice),
          _routeRow(context, context.l10n.ellaWorkspaceWhispersRoute, status.whispers),
          const SizedBox(height: 12),
          Text(context.l10n.ellaWorkspaceRouteExplanation, style: const TextStyle(color: EllaColors.textTertiary)),
        ],
      ),
    );
  }

  Widget _valueRow(String label, String value) => Container(
        margin: const EdgeInsets.only(bottom: 10),
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: EllaColors.bgSecondary, borderRadius: BorderRadius.circular(16)),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(child: Text(label, style: const TextStyle(color: EllaColors.textSecondary))),
            const SizedBox(width: 12),
            Flexible(
              child: Text(
                value,
                textAlign: TextAlign.end,
                style: const TextStyle(fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
              ),
            ),
          ],
        ),
      );

  Widget _routeRow(BuildContext context, String label, EllaRouteVerification state) {
    final verified = state == EllaRouteVerification.verified;
    return Semantics(
      label: '$label: ${verified ? context.l10n.ellaWorkspaceVerified : context.l10n.ellaWorkspaceNotVerified}',
      child: ListTile(
        contentPadding: EdgeInsets.zero,
        leading: Icon(verified ? Icons.verified : Icons.help_outline,
            color: verified ? EllaColors.primary : EllaColors.textTertiary),
        title: Text(label, style: const TextStyle(color: EllaColors.textPrimary)),
        trailing: Text(
          verified ? context.l10n.ellaWorkspaceVerified : context.l10n.ellaWorkspaceNotVerified,
          style: TextStyle(color: verified ? EllaColors.primary : EllaColors.textTertiary),
        ),
      ),
    );
  }

  String _formatTimestamp(BuildContext context, DateTime timestamp) {
    final localizations = MaterialLocalizations.of(context);
    final date = localizations.formatFullDate(timestamp);
    final time = localizations.formatTimeOfDay(TimeOfDay.fromDateTime(timestamp));
    return '$date - $time';
  }
}
