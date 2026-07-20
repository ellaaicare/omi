import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/conversation_provider.dart';

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
    setState(() => _future = GuardianAlertHistoryApi.fetch());
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_ios_new_rounded, color: EllaColors.tealDeep),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text('Whispers'),
      ),
      body: FutureBuilder<GuardianAlertHistoryResult>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator(color: EllaColors.tealDeep));
          }
          if (snapshot.hasError || snapshot.data == null) {
            return _WhisperEmptyState(onRefresh: _refresh);
          }
          final records = snapshot.data!.records;
          if (records.isEmpty) return _WhisperEmptyState(onRefresh: _refresh);
          final grouped = _groupByDay(records);
          return RefreshIndicator(
            color: EllaColors.tealDeep,
            backgroundColor: EllaColors.card,
            onRefresh: _refresh,
            child: ListView(
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 40),
              children: [
                for (final entry in grouped.entries) ...[
                  Text(entry.key, style: EllaTextStyles.eyebrow),
                  const SizedBox(height: EllaSizes.cardGap),
                  for (final record in entry.value) ...[
                    _WhisperEntry(record: record),
                    const SizedBox(height: EllaSizes.cardGap),
                  ],
                  const SizedBox(height: 16),
                ],
              ],
            ),
          );
        },
      ),
    );
  }
}

class _WhisperEntry extends StatelessWidget {
  const _WhisperEntry({required this.record});

  final GuardianAlertRecord record;

  bool get _played {
    final status = record.playbackStatus.toLowerCase();
    return status.contains('play') || status.contains('complete');
  }

  ServerConversation? _relatedConversation(BuildContext context) {
    final id = record.sourceConversationId;
    if (id == null) return null;
    for (final conversation in context.read<ConversationProvider>().conversations) {
      if (conversation.id == id) return conversation;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final related = _relatedConversation(context);
    return Container(
      padding: const EdgeInsets.all(EllaSizes.cardPadding),
      decoration: BoxDecoration(
        color: EllaColors.card,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 10,
                height: 10,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: _played ? EllaColors.teal : Colors.transparent,
                  border: Border.all(color: _played ? EllaColors.teal : EllaColors.inkSoft, width: 1.5),
                ),
              ),
              const SizedBox(width: 12),
              Text(_time(record.createdAt), style: EllaTextStyles.secondary),
            ],
          ),
          const SizedBox(height: 16),
          Text(
            '“${record.alertText}”',
            style: const TextStyle(
              fontFamily: EllaTextStyles.noteFont,
              fontSize: 18,
              fontWeight: FontWeight.w400,
              fontStyle: FontStyle.italic,
              height: 1.45,
              color: EllaColors.ink,
            ),
          ),
          const SizedBox(height: 12),
          Text(whisperWhyText(record.triggerType), style: EllaTextStyles.secondary),
          if (related != null) ...[
            const SizedBox(height: 16),
            Material(
              color: EllaColors.cardDeep,
              borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
              child: InkWell(
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: related)),
                ),
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                  child: const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 14),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          'Related memory',
                          style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700, color: EllaColors.tealDeep),
                        ),
                        SizedBox(width: 4),
                        Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep, size: 20),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _WhisperEmptyState extends StatelessWidget {
  const _WhisperEmptyState({required this.onRefresh});

  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      color: EllaColors.tealDeep,
      onRefresh: onRefresh,
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          SliverFillRemaining(
            hasScrollBody: false,
            child: Center(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 44),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      width: 10,
                      height: 10,
                      decoration: BoxDecoration(
                        color: EllaColors.teal,
                        shape: BoxShape.circle,
                        boxShadow: [
                          BoxShadow(
                            color: EllaColors.teal.withValues(alpha: 0.15),
                            spreadRadius: 8,
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                    Text(
                      "When Ella whispers something helpful, you'll find it here. 🪽",
                      style: EllaTextStyles.body.copyWith(color: EllaColors.inkSoft),
                      textAlign: TextAlign.center,
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

String whisperWhyText(String trigger) {
  final normalized = trigger.toLowerCase().replaceAll(RegExp(r'[_-]+'), ' ');
  if (normalized.contains('scissor')) return 'You asked where they were.';
  if (normalized.contains('biometric') || normalized.contains('appointment')) {
    return 'You asked about your appointment.';
  }
  if (normalized.contains('dinner')) return 'You mentioned Tuesday dinner.';
  if (normalized.contains('walk')) return 'You were deciding when to go.';
  if (normalized.contains('call') || normalized.contains('phone')) return 'You wanted to call back.';
  return 'This came up in your conversation.';
}

String _time(DateTime? value) => value == null ? '' : DateFormat('h:mm a').format(value.toLocal());

Map<String, List<GuardianAlertRecord>> _groupByDay(List<GuardianAlertRecord> records) {
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final result = <String, List<GuardianAlertRecord>>{};
  for (final record in records) {
    final value = record.createdAt?.toLocal() ?? now;
    final day = DateTime(value.year, value.month, value.day);
    final label = day == today
        ? 'TODAY'
        : day == today.subtract(const Duration(days: 1))
            ? 'YESTERDAY'
            : DateFormat('EEEE · MMMM d').format(day).toUpperCase();
    result.putIfAbsent(label, () => []).add(record);
  }
  return result;
}
