import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/daily_summary.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/conversations/conversations_page.dart';
import 'package:omi/pages/settings/daily_summary_detail_page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/utils/display_text.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef DailySummaryLoader = Future<List<DailySummary>> Function();

List<ActionItemWithMetadata> todayUpcomingReminders(
  List<ActionItemWithMetadata> items,
  DateTime now,
) {
  final tomorrow = DateTime(now.year, now.month, now.day + 1);
  return items.where((item) {
    final dueAt = item.dueAt;
    return !item.completed && dueAt != null && !dueAt.isBefore(now) && dueAt.isBefore(tomorrow);
  }).toList()
    ..sort((a, b) => a.dueAt!.compareTo(b.dueAt!));
}

class TodayPage extends StatefulWidget {
  final DailySummaryLoader? dailySummaryLoader;

  const TodayPage({super.key, this.dailySummaryLoader});

  @override
  State<TodayPage> createState() => TodayPageState();
}

class TodayPageState extends State<TodayPage> {
  final ScrollController _scrollController = ScrollController();
  DailySummary? _dailySummary;
  bool _isLoading = true;
  bool _isReading = false;

  @override
  void initState() {
    super.initState();
    _loadDailySummary();
  }

  @override
  void dispose() {
    _scrollController.dispose();
    ElevenLabsTts.stopOnDevice();
    super.dispose();
  }

  Future<void> _loadDailySummary() async {
    final summaries = await (widget.dailySummaryLoader?.call() ?? getDailySummaries(limit: 7));
    final now = DateTime.now();
    final today = '${now.year.toString().padLeft(4, '0')}-${now.month.toString().padLeft(2, '0')}-'
        '${now.day.toString().padLeft(2, '0')}';
    if (!mounted) return;
    setState(() {
      _dailySummary = summaries.cast<DailySummary?>().firstWhere(
            (summary) => summary?.date == today,
            orElse: () => null,
          );
      _isLoading = false;
    });
  }

  void scrollToTop() {
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 350), curve: Curves.easeOutCubic);
  }

  Future<void> _toggleReadAloud() async {
    if (_isReading) {
      await ElevenLabsTts.stopOnDevice();
      if (mounted) setState(() => _isReading = false);
      return;
    }
    final text = _dailySummary?.overview.trim() ?? '';
    if (text.isEmpty) return;
    setState(() => _isReading = true);
    await ElevenLabsTts.speakOnDevice(text);
    if (mounted) setState(() => _isReading = false);
  }

  void _openDailySummary() {
    final summary = _dailySummary;
    if (summary == null) return;
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => DailySummaryDetailPage(summaryId: summary.id, summary: summary),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final reminders = todayUpcomingReminders(context.watch<ActionItemsProvider>().actionItems, DateTime.now());
    final isConnected = context.select<DeviceProvider, bool>((provider) => provider.presentationIsConnected);
    final colors = Theme.of(context).colorScheme;

    return RefreshIndicator(
      onRefresh: () async {
        await Future.wait([
          _loadDailySummary(),
          context.read<ActionItemsProvider>().fetchActionItems(),
        ]);
      },
      child: ListView(
        controller: _scrollController,
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(20, 12, 20, 112),
        children: [
          Material(
            color: const Color(0xFFF6EDE5),
            borderRadius: BorderRadius.circular(8),
            child: InkWell(
              onTap: _dailySummary == null ? null : _openDailySummary,
              borderRadius: BorderRadius.circular(8),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(20, 20, 12, 20),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            context.l10n.today,
                            style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: Color(0xFF302B28)),
                          ),
                          const SizedBox(height: 10),
                          if (_isLoading)
                            const SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          else
                            Text(
                              _dailySummary?.overview.trim().isNotEmpty == true
                                  ? stripEllaDisplayPrefix(_dailySummary!.overview.trim())
                                  : context.l10n.dailyNoteEmpty,
                              style: const TextStyle(fontSize: 17, height: 1.45, color: Color(0xFF4C4641)),
                            ),
                        ],
                      ),
                    ),
                    if (_dailySummary?.overview.trim().isNotEmpty == true)
                      IconButton(
                        tooltip: _isReading ? context.l10n.stopReading : context.l10n.readAloud,
                        onPressed: _toggleReadAloud,
                        icon: Icon(_isReading ? Icons.stop_rounded : Icons.volume_up_rounded),
                        color: const Color(0xFF397A6B),
                      ),
                  ],
                ),
              ),
            ),
          ),
          if (reminders.isNotEmpty) ...[
            const SizedBox(height: 18),
            _ReminderStrip(reminders: reminders.take(3).toList()),
          ],
          const SizedBox(height: 22),
          SizedBox(
            width: double.infinity,
            height: 58,
            child: FilledButton.icon(
              onPressed: () => context.read<HomeProvider>().setIndex(2),
              icon: const Icon(Icons.call_rounded),
              label: Text(context.l10n.talkToElla, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
              style: FilledButton.styleFrom(
                backgroundColor: const Color(0xFF397A6B),
                foregroundColor: Colors.white,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
              ),
            ),
          ),
          const SizedBox(height: 12),
          ListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 4),
            leading: const Icon(Icons.auto_stories_rounded),
            title: Text(context.l10n.memories, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
            trailing: const Icon(Icons.chevron_right_rounded),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const _MemoriesPage()),
            ),
          ),
          if (!isConnected) ...[
            Divider(color: colors.outlineVariant),
            ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 4),
              leading: const Icon(Icons.watch_outlined),
              title: Text(context.l10n.necklaceResting),
              trailing: const Icon(Icons.chevron_right_rounded),
              onTap: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const ConnectDevicePage()),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _ReminderStrip extends StatelessWidget {
  final List<ActionItemWithMetadata> reminders;

  const _ReminderStrip({required this.reminders});

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(context.l10n.reminders, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700)),
        const SizedBox(height: 8),
        ...reminders.map(
          (reminder) => Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Padding(
                  padding: EdgeInsets.only(top: 5),
                  child: Icon(Icons.circle, size: 7, color: Color(0xFF397A6B)),
                ),
                const SizedBox(width: 10),
                Expanded(child: Text(reminder.description, style: const TextStyle(fontSize: 16, height: 1.35))),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _MemoriesPage extends StatelessWidget {
  const _MemoriesPage();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(context.l10n.memories)),
      body: const ConversationsPage(),
    );
  }
}
