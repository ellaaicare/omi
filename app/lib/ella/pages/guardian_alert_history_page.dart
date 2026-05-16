import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';
import 'package:omi/utils/l10n_extensions.dart';

class GuardianAlertHistoryPage extends StatefulWidget {
  const GuardianAlertHistoryPage({super.key});

  @override
  State<GuardianAlertHistoryPage> createState() => _GuardianAlertHistoryPageState();
}

class _GuardianAlertHistoryPageState extends State<GuardianAlertHistoryPage> {
  late Future<GuardianAlertHistoryResult> _future;

  @override
  void initState() {
    super.initState();
    _future = GuardianAlertHistoryApi.fetch();
  }

  Future<void> _refresh() async {
    setState(() {
      _future = GuardianAlertHistoryApi.fetch();
    });
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        foregroundColor: EllaColors.textPrimary,
        title: Text(
          context.l10n.guardianAlertsHistoryTitle,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: FutureBuilder<GuardianAlertHistoryResult>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator(color: EllaColors.primary));
          }

          final result = snapshot.data;
          if (snapshot.hasError || result == null) {
            return _EmptyState(
              title: context.l10n.guardianAlertsLoadFailed,
              subtitle: context.l10n.guardianAlertsPullToRetry,
              onRefresh: _refresh,
            );
          }

          return RefreshIndicator(
            onRefresh: _refresh,
            color: EllaColors.primary,
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
              children: [
                if (result.isLocalFallback) _FallbackBanner(message: context.l10n.guardianAlertsLocalFallback),
                if (result.records.isEmpty)
                  _EmptyState(
                    title: context.l10n.guardianAlertsEmptyTitle,
                    subtitle: context.l10n.guardianAlertsEmptySubtitle,
                    onRefresh: _refresh,
                  )
                else
                  ...result.records.map((record) => _GuardianAlertCard(record: record)),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _FallbackBanner extends StatelessWidget {
  const _FallbackBanner({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: EllaColors.warning.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        border: Border.all(color: EllaColors.warning.withValues(alpha: 0.35)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.info_outline, color: EllaColors.warning, size: 22),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary, height: 1.35),
            ),
          ),
        ],
      ),
    );
  }
}

class _GuardianAlertCard extends StatelessWidget {
  const _GuardianAlertCard({required this.record});

  final GuardianAlertRecord record;

  @override
  Widget build(BuildContext context) {
    final status = _displayStatus(context, record.playbackStatus);
    final details = _details(context);
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: _statusColor(record.playbackStatus).withValues(alpha: 0.14),
                ),
                child: Icon(_statusIcon(record.playbackStatus), color: _statusColor(record.playbackStatus), size: 22),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      record.alertText,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                        color: EllaColors.textPrimary,
                        height: 1.25,
                      ),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      _formatTime(context, record.createdAt),
                      style: const TextStyle(fontSize: 15, color: EllaColors.textTertiary),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _Chip(label: status, color: _statusColor(record.playbackStatus)),
              _Chip(
                  label: _pretty(record.triggerType, unknownLabel: context.l10n.guardianAlertsUnknown),
                  color: EllaColors.primary),
              _Chip(
                label: _pretty(record.deliveryTarget, unknownLabel: context.l10n.guardianAlertsUnknown),
                color: EllaColors.textTertiary,
              ),
              if (record.isTest) _Chip(label: context.l10n.guardianAlertsTestTag, color: EllaColors.warning),
              if (record.escalation) _Chip(label: context.l10n.guardianAlertsEscalatedTag, color: EllaColors.error),
            ],
          ),
          if (details.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(
              details,
              style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary, height: 1.35),
            ),
          ],
        ],
      ),
    );
  }

  String _details(BuildContext context) {
    final parts = <String>[];
    if (record.sourceConversationId != null) {
      parts.add(context.l10n.guardianAlertsConversationDetail(record.sourceConversationId!));
    }
    if (record.escalationStatus != null) {
      parts.add(context.l10n.guardianAlertsEscalationDetail(
        _pretty(record.escalationStatus!, unknownLabel: context.l10n.guardianAlertsUnknown),
      ));
    }
    if (record.traceId != null) parts.add(context.l10n.guardianAlertsTraceDetail(record.traceId!));
    if (record.fromLocalDebugLog) parts.add(context.l10n.guardianAlertsLocalDebugDetail);
    return parts.join('  |  ');
  }

  static String _formatTime(BuildContext context, DateTime? value) {
    if (value == null) return context.l10n.guardianAlertsTimeUnavailable;
    return DateFormat('MMM d, h:mm a').format(value.toLocal());
  }

  static String _displayStatus(BuildContext context, String value) {
    final normalized = value.toLowerCase();
    if (normalized.contains('fail')) return context.l10n.guardianAlertsStatusFailed;
    if (normalized.contains('miss')) return context.l10n.guardianAlertsStatusMissed;
    if (normalized.contains('play') || normalized.contains('complete')) return context.l10n.guardianAlertsStatusPlayed;
    if (normalized.contains('queue')) return context.l10n.guardianAlertsStatusQueued;
    return _pretty(value, unknownLabel: context.l10n.guardianAlertsUnknown);
  }

  static String _pretty(String value, {required String unknownLabel}) {
    if (value.trim().isEmpty) return unknownLabel;
    return value
        .replaceAll('_', ' ')
        .replaceAll('-', ' ')
        .split(' ')
        .where((word) => word.isNotEmpty)
        .map((word) => word[0].toUpperCase() + word.substring(1))
        .join(' ');
  }

  static Color _statusColor(String value) {
    final normalized = value.toLowerCase();
    if (normalized.contains('fail') || normalized.contains('miss')) return EllaColors.error;
    if (normalized.contains('play') || normalized.contains('complete')) return EllaColors.success;
    if (normalized.contains('queue')) return EllaColors.warning;
    return EllaColors.primary;
  }

  static IconData _statusIcon(String value) {
    final normalized = value.toLowerCase();
    if (normalized.contains('fail') || normalized.contains('miss')) return Icons.error_outline;
    if (normalized.contains('play') || normalized.contains('complete')) return Icons.volume_up;
    if (normalized.contains('queue')) return Icons.schedule;
    return Icons.notifications_active;
  }
}

class _Chip extends StatelessWidget {
  const _Chip({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600, color: color),
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({
    required this.title,
    required this.subtitle,
    required this.onRefresh,
  });

  final String title;
  final String subtitle;
  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: MediaQuery.of(context).size.height * 0.62,
      child: RefreshIndicator(
        onRefresh: onRefresh,
        color: EllaColors.primary,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          children: [
            const SizedBox(height: 96),
            const Icon(Icons.notifications_none, size: 56, color: EllaColors.textDisabled),
            const SizedBox(height: 18),
            Text(
              title,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
            ),
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Text(
                subtitle,
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary, height: 1.4),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
