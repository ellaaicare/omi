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
import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/hardware/ella_hardware_artwork.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/pages/ella_daily_note_page.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
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
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef DailySummaryLoader = Future<List<DailySummary>> Function();
typedef TodayNowProvider = DateTime Function();

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
  const TodayPage({super.key, this.dailySummaryLoader, this.nowProvider});

  final DailySummaryLoader? dailySummaryLoader;
  final TodayNowProvider? nowProvider;

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
    if (allowsUnverifiedEllaSurface()) {
      _loadDailySummary();
    }
    if (allowsGuardianSurface()) {
      _loadWhisperState();
    }
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
      final summaries = SharedPreferencesUtil.isTodayDesignPreviewEnabled
          ? DemoFixtures.dailySummaries(now: DateTime(2025, 7, 24, 9, 41))
          : await (widget.dailySummaryLoader?.call() ?? getDailySummaries(limit: 7));
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
    if (!allowsGuardianSurface()) return;
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
    if (_updatingWhispers || !allowsGuardianSurface()) return;
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
    final now = SharedPreferencesUtil.isTodayDesignPreviewEnabled
        ? DateTime(2025, 7, 24, 9, 41)
        : (widget.nowProvider?.call() ?? DateTime.now());
    final reminders = todayUpcomingReminders(context.watch<ActionItemsProvider>().actionItems, now);
    final deviceConnected = context.select<DeviceProvider, bool>((provider) => provider.presentationIsConnected);
    final device = context.watch<DeviceProvider>();
    final hasNecklace = device.presentationPairedDevice != null;
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
    final showUnverifiedSurfaces = allowsUnverifiedEllaSurface();
    final showGuardianSurfaces = allowsGuardianSurface();

    return SafeArea(
      bottom: false,
      child: RefreshIndicator(
        color: EllaColors.tealDeep,
        backgroundColor: EllaColors.card,
        onRefresh: () async {
          await Future.wait([
            if (showUnverifiedSurfaces) _loadDailySummary(),
            if (showGuardianSurfaces) _loadWhisperState(),
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
            // #1106 order: header · status strip · (actionable card) · note ·
            // whisper pill · memories · reminders — memories forward, status
            // compact; the big device surface must earn its place.
            _TodayHeader(now: now),
            const SizedBox(height: 14),
            TodayStatusStrip(
              hasNecklace: hasNecklace,
              necklaceConnected: deviceConnected,
              necklaceConnecting: device.isConnecting,
              batteryLevel: device.presentationBatteryLevel,
              deviceType: deviceType,
              headsetConnected: audioRoute.presentationHasHeadset,
              audioOutputName: audioRoute.presentationOutputName,
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectDevicePage())),
            ),
            if ((hasNecklace && !deviceConnected) || audioRoute.presentationUsesPhoneSpeaker) ...[
              const SizedBox(height: EllaSizes.cardGap),
              TodayActionableDeviceCard(
                hasNecklace: hasNecklace,
                necklaceConnected: deviceConnected,
                necklaceConnecting: device.isConnecting,
                deviceType: deviceType,
                usesPhoneSpeaker: audioRoute.presentationUsesPhoneSpeaker,
                onReconnect: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectDevicePage())),
              ),
            ],
            if (showUnverifiedSurfaces) ...[
              const SizedBox(height: EllaSizes.cardGap),
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
            ],
            if (showGuardianSurfaces) ...[
              const SizedBox(height: EllaSizes.cardGap),
              KeyedSubtree(
                key: const Key('guardian-whispers-control'),
                child: _WhisperPill(
                  enabled: _whispersOn,
                  live: isLive,
                  updating: _updatingWhispers,
                  onChanged: _setWhispers,
                  onOpenLive: () => _openLiveView(capture, conversations),
                ),
              ),
              _SeeWhispersLink(
                onTap: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage())),
              ),
            ],
            const SizedBox(height: EllaSizes.cardGap),
            if (memoriesLoading)
              const _RecentMemoriesLoading()
            else
              _RecentMemories(
                conversations: visibleConversations.take(3).toList(),
                refreshing: conversations.isLoadingConversations,
                onOpenAll: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const EllaMemoriesPage())),
              ),
            if (reminders.isNotEmpty) ...[
              const SizedBox(height: EllaSizes.sectionGap),
              _RemindersSection(reminders: reminders.take(3).toList()),
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
                Text.rich(
                  TextSpan(
                    children: [
                      TextSpan(text: hasMore ? '${preview.replaceFirst(RegExp(r'[.!?]$'), '')}… ' : preview),
                      if (hasMore)
                        TextSpan(
                          text: context.l10n.todayReadMore,
                          style: const TextStyle(color: EllaColors.tealDeep, fontWeight: FontWeight.w600),
                        ),
                    ],
                  ),
                  style: EllaTextStyles.noteBody,
                  maxLines: enlarged ? null : 6,
                  overflow: enlarged ? TextOverflow.visible : TextOverflow.ellipsis,
                ),
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

/// Single-line whisper pill (#1106): breathing dot + bold status + toggle.
/// While live capture is active the pill relaxes to radius 24 and grows one
/// line with the "See what she hears" entry. Locked OFF copy is unchanged.
class _WhisperPill extends StatelessWidget {
  const _WhisperPill({
    required this.enabled,
    required this.live,
    required this.updating,
    required this.onChanged,
    required this.onOpenLive,
  });

  final bool enabled;
  final bool live;
  final bool updating;
  final ValueChanged<bool> onChanged;
  final VoidCallback onOpenLive;

  @override
  Widget build(BuildContext context) {
    final lead = whisperStatusLead(enabled);
    final rest = whisperStatusDetail(enabled);
    return EllaCardSurface(
      borderRadius: live ? 24 : EllaSizes.radiusCircular,
      child: Padding(
        padding: EdgeInsets.symmetric(horizontal: 16, vertical: live ? 12 : 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 40),
              child: Row(
                children: [
                  EllaBreathingDot(active: enabled, live: live),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text.rich(
                      TextSpan(
                        style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
                        children: [
                          TextSpan(
                            text: lead,
                            style: const TextStyle(fontWeight: FontWeight.w700),
                          ),
                          if (!enabled) TextSpan(text: rest),
                        ],
                      ),
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
                            activeTrackColor: EllaColors.tealDeep,
                            activeThumbColor: EllaColors.paper,
                            inactiveTrackColor: EllaColors.cardDeep,
                            inactiveThumbColor: EllaColors.inkSoft,
                          ),
                  ),
                ],
              ),
            ),
            if (live)
              InkWell(
                key: const Key('live-listening-entry'),
                onTap: onOpenLive,
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                  child: Padding(
                    padding: const EdgeInsets.only(left: 20),
                    child: Text.rich(
                      TextSpan(
                        style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft),
                        children: [
                          TextSpan(text: context.l10n.todayListeningLead),
                          TextSpan(
                            text: context.l10n.todayListeningLink,
                            style: const TextStyle(color: EllaColors.tealDeep, fontWeight: FontWeight.w700),
                          ),
                        ],
                      ),
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

/// Caption link under the whisper pill — the whispers-log entry point.
class _SeeWhispersLink extends StatelessWidget {
  const _SeeWhispersLink({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: Alignment.centerLeft,
      child: InkWell(
        key: const Key('whispers-history-entry'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  context.l10n.todayWhispersHistory,
                  style: EllaTextStyles.caption.copyWith(color: EllaColors.tealDeep, fontWeight: FontWeight.w700),
                ),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep, size: 18),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Compact always-visible status strip (#1106): one 48pt row — necklace glyph
/// + dot + battery/state, divider, headset glyph + dot + route — the whole
/// strip is a single tap target to the device detail page.
class TodayStatusStrip extends StatelessWidget {
  const TodayStatusStrip({
    super.key,
    this.hasNecklace = true,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.batteryLevel,
    required this.deviceType,
    required this.headsetConnected,
    required this.audioOutputName,
    required this.onTap,
  });

  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final int batteryLevel;
  final DeviceType deviceType;
  final bool headsetConnected;
  final String audioOutputName;
  final VoidCallback onTap;

  Color get _necklaceDot => necklaceConnecting
      ? EllaColors.warning
      : necklaceConnected
          ? (batteryLevel >= 0 && batteryLevel < 20 ? EllaColors.warning : EllaColors.teal)
          : EllaColors.inkSoft;

  String _necklaceLabel(BuildContext context) {
    if (!hasNecklace) return context.l10n.todayPhoneOnly;
    if (necklaceConnecting) return context.l10n.todayStripReconnecting;
    if (!necklaceConnected) return context.l10n.todayOff;
    return batteryLevel >= 0 ? '$batteryLevel%' : context.l10n.todayOn;
  }

  @override
  Widget build(BuildContext context) {
    final scale = MediaQuery.textScalerOf(context).scale(1);
    final glyphSize = scale >= 1.45 ? 28.0 : 24.0;
    final dotSize = scale >= 1.45 ? 10.0 : 8.0;
    final necklaceGlyph = hasNecklace ? EllaHardwareArtwork.glyphForDeviceType(deviceType) : null;
    final labelStyle = EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w600);
    return EllaCardSurface(
      borderRadius: 14,
      child: InkWell(
        key: const Key('today-status-strip'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(14),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14),
            child: Row(
              children: [
                if (!hasNecklace)
                  Icon(Icons.phone_iphone_rounded, size: glyphSize, color: EllaColors.tealDeep)
                else if (necklaceGlyph != null)
                  Image.asset(necklaceGlyph, width: glyphSize, height: glyphSize)
                else
                  Icon(Icons.circle_outlined, size: glyphSize, color: EllaColors.ink),
                const SizedBox(width: 8),
                _StatusDot(color: hasNecklace ? _necklaceDot : EllaColors.teal, size: dotSize),
                const SizedBox(width: 8),
                Text(
                  _necklaceLabel(context),
                  style: labelStyle.copyWith(color: necklaceConnecting ? EllaColors.warning : EllaColors.inkSoft),
                ),
                Container(
                  width: 1,
                  height: 20,
                  color: EllaColors.cardDeep,
                  margin: const EdgeInsets.symmetric(horizontal: 8),
                ),
                Opacity(
                  opacity: headsetConnected ? 1 : 0.55,
                  child: Image.asset(EllaHardwareArtwork.whisperHeadsetGlyph, width: glyphSize, height: glyphSize),
                ),
                const SizedBox(width: 8),
                _StatusDot(color: headsetConnected ? EllaColors.teal : EllaColors.inkSoft, size: dotSize),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    headsetConnected ? audioOutputName : context.l10n.todayStripPhoneSpeaker,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: labelStyle.copyWith(color: EllaColors.inkSoft),
                  ),
                ),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft, size: 20),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.color, required this.size});

  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}

/// The large illustrated device surface (#1106): mounts below the strip ONLY
/// when something is actionable — necklace disconnected/reconnecting, or audio
/// falling back to the loudspeaker — and unmounts when resolved. The
/// loudspeaker privacy line is compact here but never removed.
class TodayActionableDeviceCard extends StatelessWidget {
  const TodayActionableDeviceCard({
    super.key,
    this.hasNecklace = true,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.deviceType,
    required this.usesPhoneSpeaker,
    required this.onReconnect,
  });

  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final DeviceType deviceType;
  final bool usesPhoneSpeaker;
  final VoidCallback onReconnect;

  @override
  Widget build(BuildContext context) {
    final necklaceIssue = hasNecklace && !necklaceConnected;
    // Runtime uses OFF artwork + the live breathing dot for reconnecting; the
    // baked-dot reconnecting asset is reserved for static contexts (goldens).
    final artwork = EllaHardwareArtwork.forDeviceType(deviceType, EllaHardwareArtworkState.off);
    return EllaCardSurface(
      child: Padding(
        padding: const EdgeInsets.all(EllaSizes.cardPadding),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (necklaceIssue) ...[
              Row(
                children: [
                  if (artwork != null)
                    Stack(
                      alignment: Alignment.bottomRight,
                      children: [
                        Image.asset(artwork, width: 52, height: 52),
                        if (necklaceConnecting) const EllaBreathingDot(active: true, live: true),
                      ],
                    ),
                  if (artwork != null) const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          necklaceConnecting ? context.l10n.todayNecklaceReconnectingTitle : context.l10n.todayNecklace,
                          style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          necklaceConnecting
                              ? context.l10n.todayNecklaceReconnectingBody
                              : context.l10n.todayNecklaceOffReconnect,
                          style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ],
            if (usesPhoneSpeaker) ...[
              if (necklaceIssue) const SizedBox(height: 14),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(Icons.volume_up_rounded, color: EllaColors.warning, size: 18),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        context.l10n.todayHeadsetFallbackWarning,
                        style: EllaTextStyles.secondary.copyWith(
                          color: EllaColors.warning,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            if (necklaceIssue) ...[
              const SizedBox(height: 12),
              Material(
                color: EllaColors.cardDeep,
                borderRadius: BorderRadius.circular(24),
                child: InkWell(
                  key: const Key('help-reconnect'),
                  onTap: onReconnect,
                  borderRadius: BorderRadius.circular(24),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                    child: Center(
                      child: Text(
                        context.l10n.todayHelpReconnect,
                        style: EllaTextStyles.secondary.copyWith(
                          color: EllaColors.tealDeep,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
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

/// Recent memories (#1106): memories are the product — a featured full-width
/// card, then a two-up grid, with "See all" in the section header. New users
/// get a warm empty state instead of a hidden section.
class _RecentMemories extends StatelessWidget {
  const _RecentMemories({required this.conversations, required this.refreshing, required this.onOpenAll});

  final List<ServerConversation> conversations;
  final bool refreshing;
  final VoidCallback onOpenAll;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            const Expanded(child: Text('RECENT MEMORIES', style: EllaTextStyles.eyebrow)),
            if (refreshing)
              const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
              )
            else if (conversations.isNotEmpty)
              InkWell(
                key: const Key('memories-see-all'),
                onTap: onOpenAll,
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
                  child: Text(
                    '${context.l10n.todaySeeAllMemories} ›',
                    style: EllaTextStyles.caption.copyWith(color: EllaColors.tealDeep, fontWeight: FontWeight.w700),
                  ),
                ),
              ),
          ],
        ),
        const SizedBox(height: EllaSizes.cardGap),
        if (conversations.isEmpty)
          EllaCardSurface(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 24),
              child: Center(
                child: Text(
                  context.l10n.todayMemoriesEmpty,
                  textAlign: TextAlign.center,
                  style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft, height: 1.5),
                ),
              ),
            ),
          )
        else ...[
          _FeaturedMemoryCard(
            conversation: conversations.first,
            onTap: () => _openDetail(context, conversations.first),
          ),
          if (conversations.length > 1) ...[
            const SizedBox(height: EllaSizes.cardGap),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: _MemoryGridCard(
                    conversation: conversations[1],
                    onTap: () => _openDetail(context, conversations[1]),
                  ),
                ),
                const SizedBox(width: EllaSizes.cardGap),
                Expanded(
                  child: conversations.length > 2
                      ? _MemoryGridCard(
                          conversation: conversations[2],
                          onTap: () => _openDetail(context, conversations[2]),
                        )
                      : const SizedBox.shrink(),
                ),
              ],
            ),
          ],
        ],
      ],
    );
  }

  void _openDetail(BuildContext context, ServerConversation conversation) {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation)));
  }
}

String _memoryTitle(ServerConversation conversation) =>
    conversation.structured.title.replaceFirst(RegExp(r'^🪽\s*'), '').replaceFirst(RegExp(r'^\[Ella\]\s*'), '');

String _memoryOverview(ServerConversation conversation) =>
    conversation.structured.overview.replaceFirst(RegExp(r'^\[Ella\]\s*'), '').trim();

class _FeaturedMemoryCard extends StatelessWidget {
  const _FeaturedMemoryCard({required this.conversation, required this.onTap});

  final ServerConversation conversation;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final overview = _memoryOverview(conversation);
    return EllaCardSurface(
      child: InkWell(
        key: const Key('featured-memory'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 48,
                height: 48,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                ),
                child: Text(
                  conversation.structured.emoji.isEmpty ? '🪽' : conversation.structured.emoji,
                  style: const TextStyle(fontSize: 24),
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _memoryTitle(conversation),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                    ),
                    if (overview.isNotEmpty) ...[
                      const SizedBox(height: 2),
                      Text(
                        overview,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft),
                      ),
                    ],
                    const SizedBox(height: 4),
                    Text(
                      _relativeTime(conversation.startedAt ?? conversation.createdAt),
                      style: EllaTextStyles.caption,
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MemoryGridCard extends StatelessWidget {
  const _MemoryGridCard({required this.conversation, required this.onTap});

  final ServerConversation conversation;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return EllaCardSurface(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                conversation.structured.emoji.isEmpty ? '🪽' : conversation.structured.emoji,
                style: const TextStyle(fontSize: 22),
              ),
              const SizedBox(height: 8),
              Text(
                _memoryTitle(conversation),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 4),
              Text(_relativeTime(conversation.startedAt ?? conversation.createdAt), style: EllaTextStyles.caption),
            ],
          ),
        ),
      ),
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

String _relativeTime(DateTime value) {
  final now = DateTime.now();
  final local = value.toLocal();
  if (local.year == now.year && local.month == now.month && local.day == now.day) {
    return local.hour < 12 ? 'This morning' : 'This afternoon';
  }
  return DateFormat('MMM d').format(local);
}
