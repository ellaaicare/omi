import 'dart:async';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class GuardianAlertHistoryPage extends StatefulWidget {
  const GuardianAlertHistoryPage({
    super.key,
    this.embedded = false,
    @visibleForTesting this.historyLoader,
    @visibleForTesting this.authorityProvider,
    @visibleForTesting this.surfaceAllowed,
  });
  final bool embedded;
  final Future<GuardianAlertHistoryResult> Function()? historyLoader;
  final ExactAccountAuthorityVerifier? Function()? authorityProvider;
  final bool Function()? surfaceAllowed;

  @override
  State<GuardianAlertHistoryPage> createState() => _GuardianAlertHistoryPageState();
}

class _GuardianAlertHistoryPageState extends State<GuardianAlertHistoryPage> {
  late Future<GuardianAlertHistoryResult> _future;
  ExactAccountAuthorityVerifier? _authority;
  Timer? _authorityWatch;
  int _generation = 0;
  static const _disabled = GuardianAlertHistoryResult(records: [], source: GuardianAlertHistorySource.disabled);
  bool get _allowed => widget.surfaceAllowed?.call() ?? allowsGuardianSurface();
  bool get _current => _allowed && (_authority?.isExactCurrent() ?? false);

  @override
  void initState() {
    super.initState();
    _load();
    // Local authority checks only; this never polls the backend or rebinds old data.
    _authorityWatch = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (_authority != null && !_current && mounted) {
        setState(() {
          _authority = null;
          _generation++;
          _future = Future.value(_disabled);
        });
      }
    });
  }

  void _load() {
    _authority = _allowed ? (widget.authorityProvider?.call() ?? WalOwnerAuthority.active()) : null;
    final authority = _authority;
    final generation = ++_generation;
    _future = authority == null || !authority.isExactCurrent()
        ? Future.value(_disabled)
        : (widget.historyLoader?.call() ?? GuardianAlertHistoryApi.fetch()).then((result) {
            return mounted && generation == _generation && identical(authority, _authority) && _current
                ? result
                : _disabled;
          });
  }

  Future<void> _refresh() async {
    if (!_allowed) return;
    setState(_load);
    await _future;
  }

  @override
  void dispose() {
    _authorityWatch?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.embedded) return _buildPage(context);
    return Theme(
      data: MediaQuery.platformBrightnessOf(context) == Brightness.dark ? ellaDarkThemeData() : ellaThemeData(),
      child: Builder(builder: _buildPage),
    );
  }

  Widget _buildPage(BuildContext context) {
    if (!_allowed) return const SizedBox.shrink();
    final renderedAuthority = _authority;
    final renderedGeneration = _generation;
    bool renderedCurrent() =>
        renderedGeneration == _generation &&
        identical(renderedAuthority, _authority) &&
        (renderedAuthority?.isExactCurrent() ?? false) &&
        _allowed;
    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        surfaceTintColor: Colors.transparent,
        scrolledUnderElevation: 0,
        automaticallyImplyLeading: false,
        leading: widget.embedded
            ? null
            : IconButton(
                icon: Icon(Icons.arrow_back_ios_new_rounded, color: EllaPalette.of(context).tealDeep),
                onPressed: () => Navigator.pop(context),
              ),
        title: const Text('Whispers'),
      ),
      body: FutureBuilder<GuardianAlertHistoryResult>(
        key: ValueKey(_generation),
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return Center(child: CircularProgressIndicator(color: EllaPalette.of(context).tealDeep));
          }
          if (snapshot.hasError || snapshot.data == null) {
            return _WhisperEmptyState(onRefresh: _refresh);
          }
          if (!_current) return _WhisperEmptyState(onRefresh: _refresh);
          final records =
              snapshot.data!.records.where((record) => !record.isSystemWakeAcknowledgement).toList(growable: false);
          if (records.isEmpty) return _WhisperEmptyState(onRefresh: _refresh);
          final grouped = _groupByDay(records);
          return RefreshIndicator(
            color: EllaPalette.of(context).tealDeep,
            backgroundColor: EllaPalette.of(context).card,
            onRefresh: _refresh,
            child: ListView(
              padding: EdgeInsets.fromLTRB(MediaQuery.textScalerOf(context).scale(1) >= 2 ? 4 : 20, 12,
                  MediaQuery.textScalerOf(context).scale(1) >= 2 ? 4 : 20, 96),
              children: [
                for (final entry in grouped.entries) ...[
                  Text(entry.key, style: EllaTypography.of(context).eyebrow),
                  const SizedBox(height: EllaSizes.cardGap),
                  for (final record in entry.value) ...[
                    _WhisperEntry(record: record, isCurrent: renderedCurrent),
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
  const _WhisperEntry({required this.record, required this.isCurrent});
  final bool Function() isCurrent;

  final GuardianAlertRecord record;

  bool get _played {
    final status = record.playbackStatus.toLowerCase();
    return const {'played', 'completed', 'complete'}.contains(status);
  }

  ServerConversation? _relatedConversation(BuildContext context) {
    final id = record.sourceConversationId;
    if (id == null || !isCurrent()) return null;
    for (final conversation in context.read<ConversationProvider>().conversations) {
      if (conversation.id == id) return conversation;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final related = _relatedConversation(context);
    return EllaCardSurface(
      child: Padding(
        padding: EdgeInsets.symmetric(
          horizontal: MediaQuery.textScalerOf(context).scale(1) >= 2 ? 4 : EllaSizes.cardPadding,
          vertical: EllaSizes.cardPadding,
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
                    color: _played ? EllaPalette.of(context).teal : Colors.transparent,
                    border: Border.all(
                        color: _played ? EllaPalette.of(context).teal : EllaPalette.of(context).inkSoft, width: 1.5),
                  ),
                ),
                const SizedBox(width: 12),
                Text(_time(record.createdAt), style: EllaTypography.of(context).secondary),
              ],
            ),
            const SizedBox(height: 16),
            Text(
              '“${record.alertText}”',
              style: TextStyle(
                fontFamily: EllaTextStyles.noteFont,
                fontSize: 18,
                fontWeight: FontWeight.w400,
                fontStyle: FontStyle.italic,
                height: 1.45,
                color: EllaPalette.of(context).ink,
              ),
            ),
            const SizedBox(height: 12),
            Text(whisperWhyText(record), style: EllaTypography.of(context).secondary),
            if (related != null) ...[
              const SizedBox(height: 16),
              Material(
                color: EllaPalette.of(context).cardDeep,
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                child: InkWell(
                  onTap: () {
                    if (!isCurrent()) return;
                    Navigator.of(context)
                        .push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: related)));
                  },
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 14),
                      child: MediaQuery.textScalerOf(context).scale(1) >= 1.28
                          ? Text('Related memory',
                              style: TextStyle(
                                  fontSize: 16, fontWeight: FontWeight.w700, color: EllaPalette.of(context).tealDeep))
                          : Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Flexible(
                                    child: Text(
                                  'Related memory',
                                  style: TextStyle(
                                      fontSize: 16,
                                      fontWeight: FontWeight.w700,
                                      color: EllaPalette.of(context).tealDeep),
                                )),
                                const SizedBox(width: 4),
                                Icon(Icons.chevron_right_rounded, color: EllaPalette.of(context).tealDeep, size: 20),
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
    );
  }
}

class _WhisperEmptyState extends StatelessWidget {
  const _WhisperEmptyState({required this.onRefresh});

  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      color: EllaPalette.of(context).tealDeep,
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
                        color: EllaPalette.of(context).teal,
                        shape: BoxShape.circle,
                        boxShadow: [
                          BoxShadow(color: EllaPalette.of(context).teal.withValues(alpha: 0.15), spreadRadius: 8)
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                    Text(
                      "When Ella whispers something helpful, you'll find it here. 🪽",
                      style: EllaTypography.of(context).body.copyWith(color: EllaPalette.of(context).inkSoft),
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

String whisperWhyText(GuardianAlertRecord record) {
  final explanation = record.why?.trim();
  if (explanation != null && explanation.isNotEmpty) return explanation;
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
