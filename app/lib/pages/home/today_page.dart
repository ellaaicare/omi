import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/daily_summary.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/hardware/ella_hardware_artwork.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/pages/ella_daily_note_page.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/audio_route_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/utils/device.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef DailySummaryLoader = Future<List<DailySummary>> Function();

String whisperStatusLead(bool enabled) => enabled ? 'Whispers are on' : 'Whispers are off';

bool shouldShowMemoriesLoading({required bool hasLoaded, required bool isLoading, required bool hasMemories}) =>
    !hasMemories && (!hasLoaded || isLoading);

bool canReadDailyNote({required bool loading, required String text}) => !loading && text.trim().isNotEmpty;

String whisperStatusDetail(bool enabled) => enabled
    ? ' — Ella will speak up when she can help. 🪽'
    : " — Ella stays quiet, but she's still listening and remembering.";

List<ActionItemWithMetadata> todayUpcomingReminders(List<ActionItemWithMetadata> items, DateTime now) {
  final dayStart = DateTime(now.year, now.month, now.day);
  final tomorrow = dayStart.add(const Duration(days: 1));
  return items.where((item) {
    final dueAt = item.dueAt;
    return !item.completed && dueAt != null && !dueAt.isBefore(now) && dueAt.isBefore(tomorrow);
  }).toList()
    ..sort((a, b) => a.dueAt!.compareTo(b.dueAt!));
}

class TodayPage extends StatefulWidget {
  const TodayPage({super.key, this.dailySummaryLoader});

  final DailySummaryLoader? dailySummaryLoader;

  @override
  State<TodayPage> createState() => TodayPageState();
}

class TodayPageState extends State<TodayPage> {
  final ScrollController _scrollController = ScrollController();
  DailySummary? _dailySummary;
  bool _isLoading = true;
  bool _isReading = false;
  bool _whispersOn = true;
  bool _updatingWhispers = false;

  @override
  void initState() {
    super.initState();
    _loadDailySummary();
    _loadWhisperState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(context.read<ConversationProvider>().ensureFreshConversations());
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    ElevenLabsTts.stopOnDevice();
    super.dispose();
  }

  Future<void> _loadDailySummary() async {
    try {
      final summaries = await (widget.dailySummaryLoader?.call() ?? getDailySummaries(limit: 7));
      if (!mounted) return;
      setState(() {
        _dailySummary = summaries.isEmpty ? null : summaries.first;
        _isLoading = false;
      });
    } catch (_) {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  Future<void> _loadWhisperState() async {
    final info = await guardian_api.getGuardianMode();
    if (!mounted || info == null) return;
    setState(() {
      _whispersOn = !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);
    });
  }

  void scrollToTop() {
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
  }

  Future<void> _toggleReadAloud() async {
    if (_isReading) {
      await ElevenLabsTts.stopOnDevice();
      if (mounted) setState(() => _isReading = false);
      return;
    }
    final text = _noteText;
    if (text.isEmpty) return;
    setState(() => _isReading = true);
    try {
      await ElevenLabsTts.speakOnDevice(text);
    } finally {
      if (mounted) setState(() => _isReading = false);
    }
  }

  Future<void> _setWhispers(bool enabled) async {
    if (_updatingWhispers) return;
    final previous = _whispersOn;
    setState(() {
      _whispersOn = enabled;
      _updatingWhispers = true;
    });
    final state = enabled ? const GuardianModeState(features: ['ACTIVE_SUPPORT']) : const GuardianModeState();
    final success = SharedPreferencesUtil().demoMode || await guardian_api.setGuardianModeTwoTier(state);
    if (!mounted) return;
    setState(() {
      _updatingWhispers = false;
      if (!success) _whispersOn = previous;
    });
    if (!success) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Whispers could not be updated. Please try again.')));
    }
  }

  String get _noteText {
    final raw = _dailySummary?.overview.trim() ?? '';
    return raw.replaceFirst(RegExp(r'^\[Ella\]\s*'), '');
  }

  void _openDailyNote() {
    final summary = _dailySummary;
    if (summary == null) return;
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => EllaDailyNotePage(summary: summary)));
  }

  void _openLiveView(CaptureProvider capture, ConversationProvider conversations) {
    final topConversationId = conversations.conversations.isEmpty ? null : conversations.conversations.first.id;
    Navigator.of(
      context,
    ).push(MaterialPageRoute(builder: (_) => ConversationCapturingPage(topConversationId: topConversationId)));
  }

  @override
  Widget build(BuildContext context) {
    final now = DateTime.now();
    final reminders = todayUpcomingReminders(context.watch<ActionItemsProvider>().actionItems, now);
    final deviceConnected = context.select<DeviceProvider, bool>((provider) => provider.presentationIsConnected);
    final device = context.watch<DeviceProvider>();
    final deviceType =
        device.presentationConnectedDevice?.type ?? device.presentationPairedDevice?.type ?? DeviceType.omi;
    final audioRoute = context.watch<AudioRouteProvider>();
    final capture = context.watch<CaptureProvider>();
    final conversations = context.watch<ConversationProvider>();
    final visibleConversations = conversations.visibleConversations;
    final memoriesLoading = shouldShowMemoriesLoading(
      hasLoaded: conversations.hasLoadedConversations,
      isLoading: conversations.isLoadingConversations,
      hasMemories: visibleConversations.isNotEmpty,
    );
    final isLive = capture.recordingState != RecordingState.stop || capture.segments.isNotEmpty;
    final scale = MediaQuery.textScalerOf(context).scale(1);

    return SafeArea(
      bottom: false,
      child: RefreshIndicator(
        color: EllaColors.tealDeep,
        backgroundColor: EllaColors.card,
        onRefresh: () async {
          await Future.wait([
            _loadDailySummary(),
            _loadWhisperState(),
            context.read<ActionItemsProvider>().fetchActionItems(),
            context.read<ConversationProvider>().getInitialConversations(),
          ]);
        },
        child: ListView(
          key: const Key('today-scroll'),
          controller: _scrollController,
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 14, EllaSizes.screenPadding, 120),
          children: [
            _TodayHeader(now: now),
            const SizedBox(height: EllaSizes.sectionGap),
            _DailyNoteCard(
              loading: _isLoading,
              text: _noteText,
              isToday: _dailySummary?.date == DateFormat('yyyy-MM-dd').format(now),
              isReading: _isReading,
              enlarged: scale >= 1.45,
              hasConversations: visibleConversations.isNotEmpty,
              onTap: _openDailyNote,
              onReadAloud: _toggleReadAloud,
            ),
            const SizedBox(height: EllaSizes.cardGap),
            TodayHardwareStatusCard(
              necklaceConnected: deviceConnected,
              necklaceConnecting: device.isConnecting,
              batteryLevel: device.presentationBatteryLevel,
              deviceType: deviceType,
              fallbackDeviceImagePath: DeviceUtils.getDeviceImagePathWithState(
                deviceType: deviceType,
                modelNumber:
                    device.presentationConnectedDevice?.modelNumber ?? device.presentationPairedDevice?.modelNumber,
                deviceName: device.presentationConnectedDevice?.name ?? device.presentationPairedDevice?.name,
                isConnected: deviceConnected,
              ),
              headsetConnected: audioRoute.presentationHasHeadset,
              audioOutputName: audioRoute.presentationOutputName,
              usesPhoneSpeaker: audioRoute.presentationUsesPhoneSpeaker,
              onOpenNecklace: () =>
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectDevicePage())),
            ),
            const SizedBox(height: EllaSizes.sectionGap),
            _WhisperCard(
              enabled: _whispersOn,
              live: isLive,
              updating: _updatingWhispers,
              onChanged: _setWhispers,
              onOpenLive: () => _openLiveView(capture, conversations),
              onOpenHistory: () =>
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage())),
            ),
            if (reminders.isNotEmpty) ...[
              const SizedBox(height: EllaSizes.sectionGap),
              _RemindersSection(reminders: reminders.take(3).toList()),
            ],
            if (memoriesLoading || visibleConversations.isNotEmpty) ...[
              const SizedBox(height: EllaSizes.sectionGap),
              if (memoriesLoading)
                const _RecentMemoriesLoading()
              else
                _RecentMemories(
                  conversations: visibleConversations.take(4).toList(),
                  refreshing: conversations.isLoadingConversations,
                  onOpenAll: () =>
                      Navigator.of(context).push(MaterialPageRoute(builder: (_) => const EllaMemoriesPage())),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

class _TodayHeader extends StatelessWidget {
  const _TodayHeader({required this.now});

  final DateTime now;

  @override
  Widget build(BuildContext context) {
    final rawName = SharedPreferencesUtil().givenName.trim();
    final firstName = rawName.isNotEmpty ? rawName : (SharedPreferencesUtil().demoMode ? 'Margaret' : 'there');
    final greeting = now.hour < 12
        ? 'Good morning'
        : now.hour < 17
            ? 'Good afternoon'
            : 'Good evening';
    final date = DateFormat('EEEE · MMMM d').format(now).toUpperCase();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(date, style: EllaTextStyles.eyebrow),
        const SizedBox(height: 8),
        Text('$greeting, $firstName.', style: EllaTextStyles.display),
      ],
    );
  }
}

class _DailyNoteCard extends StatelessWidget {
  const _DailyNoteCard({
    required this.loading,
    required this.text,
    required this.isToday,
    required this.isReading,
    required this.enlarged,
    required this.hasConversations,
    required this.onTap,
    required this.onReadAloud,
  });

  final bool loading;
  final String text;
  final bool isToday;
  final bool isReading;
  final bool enlarged;
  final bool hasConversations;
  final VoidCallback onTap;
  final VoidCallback onReadAloud;

  String _preview(BuildContext context) {
    if (text.isEmpty) {
      return hasConversations
          ? context.l10n.todayDailyNoteEmptyPreparing
          : context.l10n.todayDailyNoteEmptyNoConversations;
    }
    final sentences = RegExp(r'.*?[.!?](?:\s|$)').allMatches(text).take(2).map((m) => m.group(0)!.trim()).toList();
    return sentences.isEmpty ? text : sentences.join(' ');
  }

  @override
  Widget build(BuildContext context) {
    final preview = _preview(context);
    final hasMore = text.isNotEmpty && preview.length < text.length;
    return EllaCardSurface(
      child: InkWell(
        key: const Key('daily-note-card'),
        onTap: text.isEmpty ? null : onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Padding(
          padding: const EdgeInsets.all(EllaSizes.notePadding),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(isToday ? "TODAY'S NOTE" : 'YESTERDAY EVENING', style: EllaTextStyles.eyebrow),
              const SizedBox(height: 12),
              if (loading)
                const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                )
              else
                Text(
                  preview,
                  style: EllaTextStyles.noteBody,
                  maxLines: enlarged ? null : 6,
                  overflow: enlarged ? TextOverflow.visible : TextOverflow.ellipsis,
                ),
              if (hasMore) ...[
                const SizedBox(height: 8),
                const Text(
                  'Read more',
                  style: TextStyle(
                    fontFamily: EllaTextStyles.uiFont,
                    color: EllaColors.tealDeep,
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
              const SizedBox(height: 18),
              Wrap(
                spacing: 12,
                runSpacing: 12,
                crossAxisAlignment: WrapCrossAlignment.center,
                alignment: WrapAlignment.spaceBetween,
                children: [
                  const Text('— Ella 🪽', style: EllaTextStyles.ellaSignOff),
                  if (canReadDailyNote(loading: loading, text: text))
                    Semantics(
                      button: true,
                      label: isReading ? 'Stop reading' : 'Read aloud',
                      child: Material(
                        color: EllaColors.cardDeep,
                        borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                        child: InkWell(
                          onTap: onReadAloud,
                          borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                          child: ConstrainedBox(
                            constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(horizontal: 15),
                              child: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(
                                    isReading ? Icons.stop_rounded : Icons.volume_up_rounded,
                                    color: EllaColors.tealDeep,
                                    size: 20,
                                  ),
                                  const SizedBox(width: 8),
                                  Text(
                                    isReading ? 'Stop' : 'Read aloud',
                                    style: const TextStyle(
                                      fontSize: 16,
                                      fontWeight: FontWeight.w700,
                                      color: EllaColors.tealDeep,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _WhisperCard extends StatelessWidget {
  const _WhisperCard({
    required this.enabled,
    required this.live,
    required this.updating,
    required this.onChanged,
    required this.onOpenLive,
    required this.onOpenHistory,
  });

  final bool enabled;
  final bool live;
  final bool updating;
  final ValueChanged<bool> onChanged;
  final VoidCallback onOpenLive;
  final VoidCallback onOpenHistory;

  @override
  Widget build(BuildContext context) {
    final lead = whisperStatusLead(enabled);
    final rest = whisperStatusDetail(enabled);
    return EllaCardSurface(
      child: Padding(
        padding: const EdgeInsets.all(EllaSizes.cardPadding),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 7),
                  child: EllaBreathingDot(active: enabled, live: live),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text.rich(
                        TextSpan(
                          style: EllaTextStyles.body,
                          children: [
                            TextSpan(
                              text: lead,
                              style: const TextStyle(fontWeight: FontWeight.w700),
                            ),
                            TextSpan(text: rest),
                          ],
                        ),
                      ),
                      if (live) ...[
                        const SizedBox(height: 10),
                        Material(
                          color: EllaColors.cardDeep,
                          borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                          child: InkWell(
                            key: const Key('live-listening-entry'),
                            onTap: onOpenLive,
                            borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                            child: ConstrainedBox(
                              constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                              child: Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 14),
                                child: Row(
                                  children: [
                                    const EllaBreathingDot(active: true, live: true),
                                    const SizedBox(width: 10),
                                    Expanded(
                                      child: Text(
                                        context.l10n.todayListeningButton,
                                        style: EllaTextStyles.secondary.copyWith(
                                          color: EllaColors.tealDeep,
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                    ),
                                    const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
                                  ],
                                ),
                              ),
                            ),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                SizedBox(
                  width: EllaSizes.minTouchTarget,
                  height: EllaSizes.minTouchTarget,
                  child: updating
                      ? const Padding(
                          padding: EdgeInsets.all(14),
                          child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                        )
                      : Switch(
                          value: enabled,
                          onChanged: onChanged,
                          activeTrackColor: EllaColors.teal,
                          activeThumbColor: EllaColors.paper,
                          inactiveTrackColor: EllaColors.cardDeep,
                          inactiveThumbColor: EllaColors.inkSoft,
                        ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            const Divider(height: 1, color: EllaColors.cardDeep),
            InkWell(
              key: const Key('whispers-history-entry'),
              onTap: onOpenHistory,
              borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
              child: ConstrainedBox(
                constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                child: Row(
                  children: [
                    const Icon(Icons.record_voice_over_rounded, color: EllaColors.tealDeep, size: 22),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        context.l10n.todayWhispersHistory,
                        style: EllaTextStyles.secondary.copyWith(
                          color: EllaColors.tealDeep,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class TodayHardwareStatusCard extends StatelessWidget {
  const TodayHardwareStatusCard({
    super.key,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.batteryLevel,
    required this.deviceType,
    required this.fallbackDeviceImagePath,
    required this.headsetConnected,
    required this.audioOutputName,
    required this.usesPhoneSpeaker,
    required this.onOpenNecklace,
  });

  final bool necklaceConnected;
  final bool necklaceConnecting;
  final int batteryLevel;
  final DeviceType deviceType;
  final String fallbackDeviceImagePath;
  final bool headsetConnected;
  final String audioOutputName;
  final bool usesPhoneSpeaker;
  final VoidCallback onOpenNecklace;

  @override
  Widget build(BuildContext context) {
    final lowBattery = necklaceConnected && batteryLevel >= 0 && batteryLevel < 20;
    final necklaceArtworkState =
        necklaceConnected && !necklaceConnecting ? EllaHardwareArtworkState.on : EllaHardwareArtworkState.off;
    final necklaceImagePath =
        EllaHardwareArtwork.forDeviceType(deviceType, necklaceArtworkState) ?? fallbackDeviceImagePath;
    final headsetImagePath = EllaHardwareArtwork.forWhisperHeadset(
      headsetConnected ? EllaHardwareArtworkState.on : EllaHardwareArtworkState.off,
    );
    final necklaceStatus = necklaceConnecting
        ? context.l10n.todayConnecting
        : necklaceConnected
            ? context.l10n.todayOn
            : context.l10n.todayOff;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(context.l10n.todayHardwareStatus, style: EllaTextStyles.eyebrow),
        const SizedBox(height: EllaSizes.cardGap),
        EllaCardSurface(
          child: Padding(
            padding: const EdgeInsets.all(EllaSizes.cardPadding),
            child: Column(
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: _HardwareTile(
                        label: context.l10n.todayNecklace,
                        status: necklaceStatus,
                        detail: necklaceConnected && batteryLevel >= 0
                            ? context.l10n.todayBatteryPercent(batteryLevel)
                            : context.l10n.todayNecklaceOffReconnect,
                        connected: necklaceConnected,
                        onTap: onOpenNecklace,
                        detailColor: lowBattery ? EllaColors.warning : null,
                        visual: _HardwareArtworkVisual(
                          imagePath: necklaceImagePath,
                          reconnecting: necklaceConnecting,
                        ),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: _HardwareTile(
                        label: context.l10n.todayHeadset,
                        status: headsetConnected ? context.l10n.todayOn : context.l10n.todayOff,
                        detail: headsetConnected ? audioOutputName : context.l10n.todayVoiceOnPhone,
                        connected: headsetConnected,
                        visual: _HardwareArtworkVisual(imagePath: headsetImagePath),
                      ),
                    ),
                  ],
                ),
                if (!headsetConnected || usesPhoneSpeaker) ...[
                  const SizedBox(height: 14),
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                    decoration: BoxDecoration(
                      color: EllaColors.paper,
                      borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Icon(Icons.volume_up_rounded, color: EllaColors.warning),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text(
                            context.l10n.todayHeadsetFallbackWarning,
                            style: EllaTextStyles.secondary.copyWith(
                              color: EllaColors.ink,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _HardwareTile extends StatelessWidget {
  const _HardwareTile({
    required this.label,
    required this.status,
    required this.detail,
    required this.connected,
    required this.visual,
    this.onTap,
    this.detailColor,
  });

  final String label;
  final String status;
  final String detail;
  final bool connected;
  final Widget visual;
  final VoidCallback? onTap;
  final Color? detailColor;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.start,
          children: [
            SizedBox(height: 72, child: Center(child: visual)),
            const SizedBox(height: 8),
            Text(label, style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(
              status,
              style: EllaTextStyles.body.copyWith(
                color: connected ? EllaColors.tealDeep : EllaColors.ink,
                fontWeight: FontWeight.w800,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              detail,
              textAlign: TextAlign.center,
              style: EllaTextStyles.caption.copyWith(color: detailColor),
            ),
          ],
        ),
      ),
    );
  }
}

class _HardwareArtworkVisual extends StatelessWidget {
  const _HardwareArtworkVisual({required this.imagePath, this.reconnecting = false});

  final String imagePath;
  final bool reconnecting;

  @override
  Widget build(BuildContext context) {
    return Stack(
      alignment: Alignment.center,
      children: [
        Image.asset(
          imagePath,
          width: 64,
          height: 64,
          fit: BoxFit.contain,
        ),
        if (reconnecting)
          const Positioned(
            right: 5,
            bottom: 6,
            child: EllaBreathingDot(active: true, live: true),
          ),
      ],
    );
  }
}

class _RemindersSection extends StatelessWidget {
  const _RemindersSection({required this.reminders});

  final List<ActionItemWithMetadata> reminders;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(context.l10n.todayRemindersTitle, style: EllaTextStyles.eyebrow),
        const SizedBox(height: EllaSizes.cardGap),
        EllaCardSurface(
          child: Column(
            children: [
              for (var index = 0; index < reminders.length; index++) ...[
                _ReminderRow(reminder: reminders[index]),
                if (index < reminders.length - 1)
                  const Divider(height: 1, indent: 20, endIndent: 20, color: EllaColors.cardDeep),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

class _ReminderRow extends StatelessWidget {
  const _ReminderRow({required this.reminder});

  final ActionItemWithMetadata reminder;

  void _showDetail(BuildContext context) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: EllaColors.paper,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(reminder.description, style: EllaTextStyles.display),
              const SizedBox(height: 20),
              FilledButton.icon(
                onPressed: () async {
                  await context.read<ActionItemsProvider>().updateActionItemState(reminder, true);
                  if (sheetContext.mounted) Navigator.pop(sheetContext);
                },
                icon: const Icon(Icons.check_rounded),
                label: const Text('Done'),
              ),
              const SizedBox(height: 8),
              TextButton.icon(
                onPressed: () async {
                  final evening = DateTime.now().copyWith(hour: 18, minute: 0, second: 0, millisecond: 0);
                  await context.read<ActionItemsProvider>().updateActionItemDueDate(reminder, evening);
                  if (sheetContext.mounted) Navigator.pop(sheetContext);
                },
                icon: const Icon(Icons.schedule_rounded, color: EllaColors.tealDeep),
                label: const Text('Snooze to evening', style: TextStyle(color: EllaColors.tealDeep)),
              ),
            ],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final time = reminder.dueAt == null ? 'Anytime' : DateFormat('h:mm a').format(reminder.dueAt!);
    final source = reminder.sourceLabel?.trim();
    final sourceText = source != null && source.isNotEmpty
        ? context.l10n.todayReminderFromSource(source)
        : reminder.conversationId != null
            ? context.l10n.todayReminderFromConversation
            : context.l10n.reminders;
    return InkWell(
      onTap: () => _showDetail(context),
      borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 64),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          child: Row(
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                ),
                child: Text(time, style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w700)),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(reminder.description, style: EllaTextStyles.body),
                    const SizedBox(height: 3),
                    Text(sourceText, style: EllaTextStyles.caption),
                  ],
                ),
              ),
              IconButton(
                tooltip: context.l10n.done,
                constraints: const BoxConstraints(
                  minWidth: EllaSizes.minTouchTarget,
                  minHeight: EllaSizes.minTouchTarget,
                ),
                onPressed: () => context.read<ActionItemsProvider>().updateActionItemState(reminder, true),
                icon: const Icon(Icons.check_circle_outline_rounded, color: EllaColors.tealDeep),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RecentMemories extends StatelessWidget {
  const _RecentMemories({required this.conversations, required this.refreshing, required this.onOpenAll});

  final List<ServerConversation> conversations;
  final bool refreshing;
  final VoidCallback onOpenAll;

  @override
  Widget build(BuildContext context) {
    final height = _memoryCarouselHeight(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Expanded(child: Text('RECENT MEMORIES', style: EllaTextStyles.eyebrow)),
            SizedBox(
              width: 18,
              height: 18,
              child: refreshing ? const CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep) : null,
            ),
          ],
        ),
        const SizedBox(height: EllaSizes.cardGap),
        SizedBox(
          height: height,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            padding: EdgeInsets.zero,
            itemCount: conversations.length + 1,
            separatorBuilder: (_, __) => const SizedBox(width: EllaSizes.cardGap),
            itemBuilder: (context, index) {
              if (index == conversations.length) {
                return _AllMemoriesCard(onTap: onOpenAll, height: height);
              }
              final conversation = conversations[index];
              return _MemoryPreviewCard(
                conversation: conversation,
                height: height,
                onTap: () => Navigator.of(
                  context,
                ).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation))),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _RecentMemoriesLoading extends StatelessWidget {
  const _RecentMemoriesLoading();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('RECENT MEMORIES', style: EllaTextStyles.eyebrow),
        const SizedBox(height: EllaSizes.cardGap),
        EllaCardSurface(
          child: SizedBox(
            height: _memoryCarouselHeight(context),
            child: const Center(
              child: SizedBox(
                width: 24,
                height: 24,
                child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

double _memoryCarouselHeight(BuildContext context) {
  final scale = MediaQuery.textScalerOf(context).scale(1);
  return scale >= 1.45
      ? 210.0
      : scale >= 1.15
          ? 176.0
          : 148.0;
}

class _MemoryPreviewCard extends StatelessWidget {
  const _MemoryPreviewCard({required this.conversation, required this.height, required this.onTap});

  final ServerConversation conversation;
  final double height;
  final VoidCallback onTap;

  String get _title =>
      conversation.structured.title.replaceFirst(RegExp(r'^🪽\s*'), '').replaceFirst(RegExp(r'^\[Ella\]\s*'), '');

  @override
  Widget build(BuildContext context) {
    return EllaCardSurface(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: SizedBox(
          width: 240,
          height: height,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: 40,
                  height: 40,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: EllaColors.cardDeep,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                  ),
                  child: Text(conversation.structured.emoji.isEmpty ? '🪽' : conversation.structured.emoji),
                ),
                const SizedBox(height: 10),
                Expanded(
                  child: Text(
                    _title,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                  ),
                ),
                Text(_relativeTime(conversation.startedAt ?? conversation.createdAt), style: EllaTextStyles.caption),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _AllMemoriesCard extends StatelessWidget {
  const _AllMemoriesCard({required this.onTap, required this.height});

  final VoidCallback onTap;
  final double height;

  @override
  Widget build(BuildContext context) {
    return EllaCardSurface(
      color: EllaColors.cardDeep,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: SizedBox(
          width: 180,
          height: height,
          child: const Center(
            child: Text(
              'All memories →',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700, color: EllaColors.tealDeep),
            ),
          ),
        ),
      ),
    );
  }
}

String _relativeTime(DateTime value) {
  final now = DateTime.now();
  final local = value.toLocal();
  if (local.year == now.year && local.month == now.month && local.day == now.day) {
    return local.hour < 12 ? 'This morning' : 'This afternoon';
  }
  return DateFormat('MMM d').format(local);
}
