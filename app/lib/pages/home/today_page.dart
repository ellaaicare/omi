import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/demo/today_card_fixtures.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/hardware/ella_hardware_artwork.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/guardian_mode_service.dart' as guardian_native;
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef TodayCardTalkRouteOpener = Future<void> Function(BuildContext context, TodayCard card);
typedef TodayCardAuthoritySnapshot = ({String uid, String authorityKey, bool isProvisioningReady});
typedef TodayCardAuthoritySnapshotProvider = TodayCardAuthoritySnapshot Function();
typedef TodayNowProvider = DateTime Function();
typedef GuardianModeLoader = Future<GuardianModeInfo?> Function();
typedef GuardianModeSetter = Future<bool> Function(GuardianModeState state);
typedef GuardianNativeLifecycle = Future<void> Function();
typedef GuardianAvailability = bool Function();

enum _HomeCaptureSource { phone, necklaceOwned, necklaceContinuous }

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
  const TodayPage({
    super.key,
    this.todayCardRepository,
    this.todayCardCache,
    this.todayCardTalkRouteOpener,
    this.todayCardUidOverride,
    this.todayCardAuthorityKeyOverride,
    this.todayCardReadyOverride,
    this.todayCardAuthoritySnapshotProvider,
    this.todayCardAuthorityChanges,
    this.nowProvider,
    this.guardianModeLoader,
    this.guardianModeSetter,
    this.guardianNativeStart,
    this.guardianNativeStop,
    this.guardianAvailability,
  });

  final TodayCardRepository? todayCardRepository;
  final TodayCardCache? todayCardCache;
  final TodayCardTalkRouteOpener? todayCardTalkRouteOpener;
  final String? todayCardUidOverride;
  final String? todayCardAuthorityKeyOverride;
  final bool? todayCardReadyOverride;
  final TodayCardAuthoritySnapshotProvider? todayCardAuthoritySnapshotProvider;
  final Listenable? todayCardAuthorityChanges;
  final TodayNowProvider? nowProvider;
  final GuardianModeLoader? guardianModeLoader;
  final GuardianModeSetter? guardianModeSetter;
  final GuardianNativeLifecycle? guardianNativeStart;
  final GuardianNativeLifecycle? guardianNativeStop;
  final GuardianAvailability? guardianAvailability;

  @visibleForTesting
  static V2VSessionScope sessionScopeFor(TodayCard card) =>
      V2VSessionScope.dailyCard(cardId: card.id, expectedVersion: card.version);

  @visibleForTesting
  static String cacheAuthorityKey(ActiveWalAuthority authority) {
    final owner = authority.owner;
    return sha256
        .convert(
          utf8.encode(
            '${owner.uid}\n${owner.profileBindingId}\n${owner.bindingRevision}\n'
            '${owner.consentReceiptId}\n${owner.authorityGenerationAtCapture}',
          ),
        )
        .toString();
  }

  @override
  State<TodayPage> createState() => TodayPageState();
}

class TodayPageState extends State<TodayPage> with WidgetsBindingObserver {
  final ScrollController _scrollController = ScrollController();
  late final TodayCardController _todayCardController;
  late final Listenable _todayCardAuthorityChanges;
  EllaProvisioningProvider? _provisioningProvider;
  bool _homeCaptureActive = false;
  bool _homeCaptureStarting = false;
  bool _homeCaptureFinalizationPending = false;
  Future<bool>? _homeCaptureFinalizationInFlight;
  bool _abandonHomeCaptureAfterFinalization = false;
  int _homeCaptureAuthorityGeneration = 0;
  _HomeCaptureSource? _homeCaptureSource;
  bool _whispersOn = false;
  bool _whispersVerified = false;
  bool _updatingWhispers = false;
  final Set<String> _deletingMemoryIds = <String>{};

  bool get _guardianAvailable => widget.guardianAvailability?.call() ?? allowsGuardianSurface();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _todayCardController = TodayCardController(
      repository: widget.todayCardRepository ??
          (SharedPreferencesUtil.isTodayDesignPreviewEnabled
              ? const _DemoTodayCardRepository()
              : HttpTodayCardRepository()),
      cache: widget.todayCardCache ?? SharedPreferencesTodayCardCache(),
      onRevalidationRequired: () => _syncTodayCardAuthority(forceReload: true),
    )..addListener(_onTodayCardChanged);
    _todayCardAuthorityChanges = widget.todayCardAuthorityChanges ?? SharedPreferencesUtil.aiConsentAuthorityChanges;
    _todayCardAuthorityChanges.addListener(_onTodayCardAuthorityChanged);
    if (_guardianAvailable) {
      _loadWhisperState();
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(context.read<ConversationProvider>().ensureFreshConversations());
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final nextProvider = Provider.of<EllaProvisioningProvider?>(context, listen: false);
    if (!identical(nextProvider, _provisioningProvider)) {
      _provisioningProvider?.removeListener(_onProvisioningChanged);
      _provisioningProvider = nextProvider;
      _provisioningProvider?.addListener(_onProvisioningChanged);
    }
    unawaited(_syncTodayCardAuthority());
  }

  TodayCardAuthoritySnapshot _captureTodayCardAuthority() {
    final injected = widget.todayCardAuthoritySnapshotProvider;
    if (injected != null) return injected();
    const isPreview = SharedPreferencesUtil.isTodayDesignPreviewEnabled;
    final uid = widget.todayCardUidOverride ?? (isPreview ? 'today-card-preview' : SharedPreferencesUtil().uid);
    final authority = isPreview || widget.todayCardUidOverride != null ? null : WalOwnerAuthority.active();
    final authorityKey = widget.todayCardAuthorityKeyOverride ??
        (widget.todayCardUidOverride != null
            ? 'test-authority:$uid'
            : isPreview
                ? 'today-card-preview'
                : authority?.uid == uid
                    ? TodayPage.cacheAuthorityKey(authority!)
                    : '');
    final isReady = widget.todayCardReadyOverride ??
        (isPreview || (_provisioningProvider?.isOperational == true && authority?.uid == uid));
    return (uid: uid, authorityKey: authorityKey, isProvisioningReady: isReady);
  }

  Future<void> _syncTodayCardAuthority({bool forceReload = false}) async {
    final snapshot = _captureTodayCardAuthority();
    await _todayCardController.updateAuthority(
      uid: snapshot.uid,
      authorityKey: snapshot.authorityKey,
      isProvisioningReady: snapshot.isProvisioningReady,
      forceReload: forceReload,
    );
  }

  void _onProvisioningChanged() => unawaited(_syncTodayCardAuthority());

  void _onTodayCardAuthorityChanged() {
    // In-memory content disappears synchronously; the exact replacement
    // authority is recaptured only after the old card is no longer renderable.
    _homeCaptureAuthorityGeneration++;
    _todayCardController.invalidateAuthority();
    final finalization = _homeCaptureFinalizationInFlight;
    if (finalization != null) {
      _abandonHomeCaptureAfterFinalization = true;
      unawaited(_joinHomeCaptureFinalization(finalization));
    } else if (_homeCaptureFinalizationPending && mounted) {
      setState(() {
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      });
    }
    unawaited(_syncTodayCardAuthority(forceReload: true));
  }

  void _onTodayCardChanged() {
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(_syncTodayCardAuthority(forceReload: true));
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _provisioningProvider?.removeListener(_onProvisioningChanged);
    _todayCardAuthorityChanges.removeListener(_onTodayCardAuthorityChanged);
    _todayCardController
      ..removeListener(_onTodayCardChanged)
      ..dispose();
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _loadWhisperState() async {
    if (!_guardianAvailable) return;
    final info = await _readWhisperState();
    if (info == null) {
      try {
        await _reconcileWhisperNative(false);
      } catch (_) {}
      if (!mounted) return;
      setState(() {
        _whispersOn = false;
        _whispersVerified = false;
      });
      return;
    }
    if (!mounted) return;
    final serverEnabled = _whispersEnabled(info);
    var resolvedEnabled = serverEnabled;
    var resolvedVerified = false;
    try {
      await _reconcileWhisperNative(serverEnabled);
      resolvedVerified = true;
    } catch (_) {
      // If native capture cannot match an authoritative ON response, disable
      // the server mode only when both the write and readback confirm OFF.
      // Otherwise keep the last authoritative value behind an unavailable
      // control and make no ON/OFF claim.
      if (serverEnabled && await _writeWhisperState(const GuardianModeState())) {
        final confirmed = await _readWhisperState();
        if (confirmed != null && !_whispersEnabled(confirmed)) {
          resolvedEnabled = false;
          try {
            await _reconcileWhisperNative(false);
            resolvedVerified = true;
          } catch (_) {}
        }
      }
    }
    if (!mounted) return;
    setState(() {
      _whispersOn = resolvedEnabled;
      _whispersVerified = resolvedVerified;
    });
  }

  Future<void> _confirmMemoryDelete(ServerConversation conversation) async {
    if (_deletingMemoryIds.contains(conversation.id)) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(context.l10n.deleteMemory),
        content: Text(context.l10n.deleteMemoryConfirmation),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialogContext).pop(false), child: Text(context.l10n.cancel)),
          FilledButton(
            key: const Key('confirm-home-delete-memory'),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            style: FilledButton.styleFrom(backgroundColor: EllaColors.warning, foregroundColor: Colors.white),
            child: Text(context.l10n.delete),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    setState(() => _deletingMemoryIds.add(conversation.id));
    final deleted = await context.read<ConversationProvider>().deleteConversationPermanently(conversation);
    if (!mounted) return;
    setState(() => _deletingMemoryIds.remove(conversation.id));
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(deleted ? context.l10n.memoryDeleted : context.l10n.anErrorOccurredTryAgain)),
    );
  }

  Future<GuardianModeInfo?> _readWhisperState() async {
    try {
      final loader = widget.guardianModeLoader;
      if (loader != null) return loader();
      final result = await guardian_api.getGuardianMode();
      return result.isSuccess ? result.value : null;
    } catch (_) {
      return null;
    }
  }

  bool _whispersEnabled(GuardianModeInfo info) =>
      !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);

  Future<void> _reconcileWhisperNative(bool enabled) => enabled
      ? (widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start())
      : (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());

  Future<bool> _writeWhisperState(GuardianModeState state) async {
    try {
      final setter = widget.guardianModeSetter;
      if (setter != null) return setter(state);
      return (await guardian_api.setGuardianModeTwoTier(state)).isSuccess;
    } catch (_) {
      return false;
    }
  }

  void scrollToTop() {
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
  }

  Future<void> _setWhispers(bool enabled) async {
    if (_updatingWhispers || !_guardianAvailable) return;
    final previousEnabled = _whispersOn;
    setState(() {
      _whispersOn = enabled;
      _updatingWhispers = true;
    });
    final state = enabled ? const GuardianModeState(features: ['ACTIVE_SUPPORT']) : const GuardianModeState();
    var success = false;
    try {
      if (!enabled) {
        await (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());
      }
      success = await _writeWhisperState(state);
      if (success && enabled) {
        await (widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start());
      }
    } catch (_) {
      success = false;
    }
    if (!success && enabled) {
      try {
        await (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());
        await _writeWhisperState(const GuardianModeState());
      } catch (_) {}
    }
    var resolvedEnabled = enabled;
    var resolvedVerified = success;
    if (!success) {
      final authoritative = await _readWhisperState();
      if (authoritative != null) {
        resolvedEnabled = _whispersEnabled(authoritative);
        try {
          await _reconcileWhisperNative(resolvedEnabled);
          resolvedVerified = true;
        } catch (_) {
          resolvedVerified = false;
        }
      } else {
        // The write may have reached the server even though the response did
        // not. Keep the last verified display value but mark it unavailable;
        // never claim OFF (or ON) without an authoritative readback.
        resolvedEnabled = previousEnabled;
        resolvedVerified = false;
        try {
          await _reconcileWhisperNative(previousEnabled);
        } catch (_) {}
      }
    }
    if (!mounted) return;
    setState(() {
      _updatingWhispers = false;
      _whispersOn = resolvedEnabled;
      _whispersVerified = resolvedVerified;
    });
    if (!success) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.anErrorOccurredTryAgain)));
    }
  }

  Future<void> _openTodayCardTalk() async {
    await _syncTodayCardAuthority();
    if (!mounted) return;
    final card = _todayCardController.state.card;
    if (card == null) return;
    final routeOpener = widget.todayCardTalkRouteOpener;
    if (routeOpener != null) {
      await routeOpener(context, card);
      return;
    }
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      isDismissible: false,
      enableDrag: false,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      clipBehavior: Clip.antiAlias,
      builder: (_) => FractionallySizedBox(
        heightFactor: 0.94,
        child: EllaVoiceChatPage(
          sessionScope: TodayPage.sessionScopeFor(card),
          memoryTitle: card.headline,
          modalPresentation: true,
        ),
      ),
    );
  }

  Future<void> _openTodayCardDetail() async {
    await _syncTodayCardAuthority();
    if (!mounted) return;
    final card = _todayCardController.state.card;
    if (card == null) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      clipBehavior: Clip.antiAlias,
      builder: (sheetContext) => FractionallySizedBox(
        heightFactor: 0.82,
        child: _TodayCardDetailSheet(
          card: card,
          onTalk: () {
            Navigator.of(sheetContext).pop();
            unawaited(_openTodayCardTalk());
          },
        ),
      ),
    );
  }

  String _phoneCaptureFailureMessage(PhoneCaptureStartResult result) => switch (result) {
        PhoneCaptureStartResult.microphonePermissionDenied => context.l10n.todayMicrophonePermissionDenied,
        PhoneCaptureStartResult.transcriptionUnavailable => context.l10n.todayTranscriptionUnavailable,
        PhoneCaptureStartResult.accountNotReady ||
        PhoneCaptureStartResult.consentUnavailable ||
        PhoneCaptureStartResult.recorderUnavailable ||
        PhoneCaptureStartResult.cancelled =>
          context.l10n.todayRecordingUnavailable,
        PhoneCaptureStartResult.started => context.l10n.todayRecordingUnavailable,
      };

  Future<bool> _finalizeHomeMoment(
    CaptureProvider capture, {
    bool Function()? isCurrent,
  }) async {
    final finalized = await capture.finalizeCurrentConversation();
    if (isCurrent != null && !isCurrent()) return false;
    if (finalized) return true;
    if (!mounted) return false;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayNoWordsCaptured)));
    return false;
  }

  Future<void> _joinHomeCaptureFinalization(Future<bool> operation) async {
    try {
      await operation;
    } catch (_) {
      // The initiating UI reports the failure. This join exists only to keep
      // old capture work behind the transition/route barrier until it settles.
    }
  }

  Future<bool> _finishHomeCapture(CaptureProvider capture) {
    final existing = _homeCaptureFinalizationInFlight;
    if (existing != null) return existing;

    final completer = Completer<bool>();
    final operation = completer.future;
    _homeCaptureFinalizationInFlight = operation;
    if (mounted) setState(() {});
    unawaited(_runHomeCaptureFinalization(capture, operation, completer));
    return operation;
  }

  Future<void> _runHomeCaptureFinalization(
    CaptureProvider capture,
    Future<bool> operation,
    Completer<bool> completer,
  ) async {
    Object? error;
    StackTrace? stackTrace;
    var result = false;
    try {
      result = await _finishHomeCaptureOnce(capture);
    } catch (caughtError, caughtStackTrace) {
      error = caughtError;
      stackTrace = caughtStackTrace;
    }

    if (identical(_homeCaptureFinalizationInFlight, operation)) {
      _homeCaptureFinalizationInFlight = null;
      if (_abandonHomeCaptureAfterFinalization) {
        _abandonHomeCaptureAfterFinalization = false;
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      }
      if (mounted) setState(() {});
    }

    if (error != null) {
      completer.completeError(error, stackTrace!);
    } else {
      completer.complete(result);
    }
  }

  Future<bool> _finishHomeCaptureOnce(CaptureProvider capture) async {
    final authorityGeneration = _homeCaptureAuthorityGeneration;
    if (_homeCaptureFinalizationPending) {
      final finalized = await _finalizeHomeMoment(
        capture,
        isCurrent: () => authorityGeneration == _homeCaptureAuthorityGeneration,
      );
      final isCurrent = authorityGeneration == _homeCaptureAuthorityGeneration;
      if (finalized && isCurrent && mounted) {
        setState(() {
          _homeCaptureFinalizationPending = false;
          _homeCaptureSource = null;
        });
      }
      return finalized && isCurrent;
    }

    final source = _homeCaptureSource;
    switch (source) {
      case _HomeCaptureSource.phone:
        await capture.stopStreamRecording();
        break;
      case _HomeCaptureSource.necklaceOwned:
        await capture.stopStreamDeviceRecording();
        if (capture.recordingState == RecordingState.deviceRecord) {
          throw StateError('Home-owned necklace stream did not stop');
        }
        break;
      case _HomeCaptureSource.necklaceContinuous:
        break;
      case null:
        if (capture.recordingState == RecordingState.record) {
          await capture.stopStreamRecording();
        }
        break;
    }
    if (authorityGeneration != _homeCaptureAuthorityGeneration) {
      if (mounted) {
        setState(() {
          _homeCaptureActive = false;
          _homeCaptureFinalizationPending = false;
          _homeCaptureSource = null;
        });
      }
      return false;
    }
    if (mounted) {
      setState(() {
        _homeCaptureActive = false;
        if (source == _HomeCaptureSource.phone || source == _HomeCaptureSource.necklaceOwned) {
          _homeCaptureFinalizationPending = true;
        } else {
          _homeCaptureSource = null;
        }
      });
    }
    final finalized = await _finalizeHomeMoment(
      capture,
      isCurrent: () => authorityGeneration == _homeCaptureAuthorityGeneration,
    );
    final isCurrent = authorityGeneration == _homeCaptureAuthorityGeneration;
    if (finalized && isCurrent && mounted) {
      setState(() {
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      });
    }
    return finalized && isCurrent;
  }

  Future<void> _openLiveTranscript(CaptureProvider capture) async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ConversationCapturingPage(
          onProcessNow: () async {
            if (_homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null) {
              return _finishHomeCapture(capture);
            }
            if (capture.recordingState == RecordingState.deviceRecord) {
              return _finalizeHomeMoment(capture);
            }
            if (capture.recordingState == RecordingState.record) {
              await capture.stopStreamRecording();
              return _finalizeHomeMoment(capture);
            }
            return false;
          },
        ),
      ),
    );
    final finalization = _homeCaptureFinalizationInFlight;
    if (finalization != null) {
      _abandonHomeCaptureAfterFinalization = true;
      await _joinHomeCaptureFinalization(finalization);
      return;
    }
    if (_homeCaptureFinalizationPending && mounted) {
      setState(() {
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      });
    }
  }

  Future<void> _toggleHomeCapture({
    required CaptureProvider capture,
    required bool isActive,
    required bool necklaceConnected,
    required BtDevice? connectedDevice,
  }) async {
    if (_homeCaptureStarting) return;
    setState(() => _homeCaptureStarting = true);
    try {
      final finalization = _homeCaptureFinalizationInFlight;
      if (finalization != null) {
        await _joinHomeCaptureFinalization(finalization);
        return;
      }
      if (_homeCaptureFinalizationPending) {
        await _finishHomeCapture(capture);
        return;
      }
      if (isActive) {
        await _finishHomeCapture(capture);
        return;
      }

      if (necklaceConnected && connectedDevice != null) {
        var startedHere = false;
        if (capture.recordingState == RecordingState.stop || capture.recordingState == RecordingState.error) {
          await capture.streamDeviceRecording(device: connectedDevice);
          startedHere = capture.recordingState == RecordingState.deviceRecord;
        } else if (capture.recordingState == RecordingState.deviceRecord) {
          // The necklace may already be streaming continuously. Finalize that
          // pre-tap audio so this intentional moment starts at an exact boundary
          // without stopping the user's ambient capture.
          if (capture.hasCapturableContent) {
            await capture.finalizeCurrentConversation();
          }
        }
        if (!mounted) return;
        final started = capture.recordingState == RecordingState.deviceRecord;
        setState(() {
          _homeCaptureActive = started;
          _homeCaptureSource =
              started ? (startedHere ? _HomeCaptureSource.necklaceOwned : _HomeCaptureSource.necklaceContinuous) : null;
        });
        if (!started) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
        }
        return;
      }

      final result = await capture.streamRecording();
      if (!mounted) return;
      final started = result == PhoneCaptureStartResult.started && capture.recordingState == RecordingState.record;
      setState(() {
        _homeCaptureActive = started;
        _homeCaptureSource = started ? _HomeCaptureSource.phone : null;
      });
      if (!started) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(_phoneCaptureFailureMessage(result))));
      }
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
    } finally {
      if (mounted) setState(() => _homeCaptureStarting = false);
    }
  }

  Future<void> _showHomeControls({
    required bool hasNecklace,
    required bool necklaceConnected,
    required bool necklaceConnecting,
    required int batteryLevel,
    required DeviceType deviceType,
    required bool showGuardianSurfaces,
  }) async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      builder: (sheetContext) => _HomeControlsSheet(
        hasNecklace: hasNecklace,
        necklaceConnected: necklaceConnected,
        necklaceConnecting: necklaceConnecting,
        batteryLevel: batteryLevel,
        deviceType: deviceType,
        showWhispers: showGuardianSurfaces,
        whispersEnabled: _whispersOn,
        whispersVerified: _whispersVerified,
        whispersUpdating: _updatingWhispers,
        onWhispersChanged: (enabled) {
          Navigator.of(sheetContext).pop();
          unawaited(_setWhispers(enabled));
        },
        onWhispersHistory: () {
          Navigator.of(sheetContext).pop();
          Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage()));
        },
        onManageNecklace: () {
          Navigator.of(sheetContext).pop();
          Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectDevicePage()));
        },
      ),
    );
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
    final capture = context.watch<CaptureProvider>();
    final conversations = context.watch<ConversationProvider>();
    final visibleConversations = conversations.visibleConversations;
    final memoriesLoading = shouldShowMemoriesLoading(
      hasLoaded: conversations.hasLoadedConversations,
      isLoading: conversations.isLoadingConversations,
      hasMemories: visibleConversations.isNotEmpty,
    );
    final showGuardianSurfaces = _guardianAvailable;
    final homeCaptureOwned =
        _homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null;
    final homeCaptureUsesNecklace = _homeCaptureSource == _HomeCaptureSource.necklaceOwned ||
        _homeCaptureSource == _HomeCaptureSource.necklaceContinuous;

    return SafeArea(
      bottom: false,
      child: RefreshIndicator(
        color: EllaColors.tealDeep,
        backgroundColor: EllaColors.card,
        onRefresh: () async {
          await Future.wait([
            _todayCardController.retry(),
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
            _TodayHeader(
              now: now,
              onOpenControls: () => _showHomeControls(
                hasNecklace: hasNecklace,
                necklaceConnected: deviceConnected,
                necklaceConnecting: device.isConnecting,
                batteryLevel: device.presentationBatteryLevel,
                deviceType: deviceType,
                showGuardianSurfaces: showGuardianSurfaces,
              ),
            ),
            const SizedBox(height: 22),
            TodayCardSurface(
              state: _todayCardController.state,
              onReadMore: _todayCardController.state.card == null ? null : _openTodayCardDetail,
              onTalk: _todayCardController.state.card == null ? null : _openTodayCardTalk,
            ),
            const SizedBox(height: 20),
            TodayRecordMomentControl(
              homeCaptureOwned: homeCaptureOwned,
              homeCaptureUsesNecklace: homeCaptureUsesNecklace,
              starting: _homeCaptureStarting,
              necklaceConnected: deviceConnected,
              necklaceConnecting: device.isConnecting,
              recordingState: capture.recordingState,
              necklaceContinuouslyRecording:
                  deviceConnected && capture.recordingState == RecordingState.deviceRecord && !homeCaptureOwned,
              onViewTranscript: () => _openLiveTranscript(capture),
              onTap: () => _toggleHomeCapture(
                capture: capture,
                isActive: homeCaptureOwned,
                necklaceConnected: deviceConnected,
                connectedDevice: device.presentationConnectedDevice,
              ),
            ),
            if (showGuardianSurfaces) ...[
              const SizedBox(height: EllaSizes.sectionGap),
              _WhispersHomeCard(
                enabled: _whispersOn,
                verified: _whispersVerified,
                onOpen: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage())),
              ),
            ],
            const SizedBox(height: EllaSizes.sectionGap),
            if (memoriesLoading)
              const _RecentMemoriesLoading()
            else
              _RecentMemories(
                conversations: visibleConversations.take(6).toList(),
                refreshing: conversations.isLoadingConversations,
                deletingConversationIds: _deletingMemoryIds,
                onDelete: _confirmMemoryDelete,
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

class _DemoTodayCardRepository implements TodayCardRepository {
  const _DemoTodayCardRepository();

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => TodayCardFixtures.recap();
}

class _TodayCardDetailSheet extends StatelessWidget {
  const _TodayCardDetailSheet({required this.card, required this.onTalk});

  final TodayCard card;
  final VoidCallback onTalk;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 10),
          Center(
            child: Container(
              width: 44,
              height: 4,
              decoration: BoxDecoration(color: EllaColors.cardDeep, borderRadius: BorderRadius.circular(2)),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 8, 0),
            child: Row(
              children: [
                Expanded(child: Text(context.l10n.todayCardPreparingEyebrow, style: EllaTextStyles.eyebrow)),
                IconButton(
                  key: const Key('today-card-detail-close'),
                  tooltip: context.l10n.close,
                  onPressed: () => Navigator.of(context).pop(),
                  icon: const Icon(Icons.close_rounded, color: EllaColors.tealDeep),
                ),
              ],
            ),
          ),
          Expanded(
            child: SingleChildScrollView(
              key: const Key('today-card-detail-scroll'),
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Semantics(
                    header: true,
                    child: Text(card.headline, style: EllaTextStyles.noteBody.copyWith(fontSize: 30, height: 1.15)),
                  ),
                  const SizedBox(height: 18),
                  Text(card.body, style: EllaTextStyles.noteBody.copyWith(fontSize: 20, height: 1.45)),
                ],
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
            child: FilledButton.icon(
              key: const Key('today-card-detail-talk'),
              onPressed: onTalk,
              icon: const Icon(Icons.graphic_eq_rounded),
              label: Text(context.l10n.memoryTalkAction),
              style: FilledButton.styleFrom(
                backgroundColor: EllaColors.tealDeep,
                foregroundColor: Colors.white,
                minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _TodayHeader extends StatelessWidget {
  const _TodayHeader({required this.now, required this.onOpenControls});

  final DateTime now;
  final VoidCallback onOpenControls;

  @override
  Widget build(BuildContext context) {
    final rawName = SharedPreferencesUtil().givenName.trim();
    final firstName = rawName.isNotEmpty
        ? rawName
        : (SharedPreferencesUtil().demoMode ? 'Margaret' : context.l10n.todayGreetingFallbackName);
    final greeting = now.hour < 12
        ? context.l10n.todayGreetingMorning(firstName)
        : now.hour < 17
            ? context.l10n.todayGreetingAfternoon(firstName)
            : context.l10n.todayGreetingEvening(firstName);
    final locale = Localizations.localeOf(context).toLanguageTag();
    final date = context.l10n
        .todayDateEyebrow(DateFormat('EEEE', locale).format(now), DateFormat('MMMM d', locale).format(now))
        .toUpperCase();
    return Row(
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(date, style: EllaTextStyles.eyebrow),
              const SizedBox(height: 8),
              Text(greeting, style: EllaTextStyles.noteBody.copyWith(fontSize: 24, height: 1.14)),
            ],
          ),
        ),
        const SizedBox(width: 12),
        Semantics(
          button: true,
          label: context.l10n.todayControlsButton,
          child: IconButton(
            key: const Key('today-controls-button'),
            onPressed: onOpenControls,
            tooltip: context.l10n.todayControlsButton,
            icon: const Icon(Icons.tune_rounded, color: EllaColors.tealDeep),
            iconSize: 24,
            constraints: const BoxConstraints.tightFor(
              width: EllaSizes.minTouchTarget,
              height: EllaSizes.minTouchTarget,
            ),
            style: IconButton.styleFrom(
              backgroundColor: EllaColors.card,
              side: const BorderSide(color: EllaColors.cardEdge),
            ),
          ),
        ),
      ],
    );
  }
}

class TodayRecordMomentControl extends StatelessWidget {
  const TodayRecordMomentControl({
    super.key,
    required this.homeCaptureOwned,
    required this.homeCaptureUsesNecklace,
    required this.starting,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.recordingState,
    required this.necklaceContinuouslyRecording,
    required this.onViewTranscript,
    required this.onTap,
  });

  final bool homeCaptureOwned;
  final bool homeCaptureUsesNecklace;
  final bool starting;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final RecordingState recordingState;
  final bool necklaceContinuouslyRecording;
  final VoidCallback onViewTranscript;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.disableAnimationsOf(context);
    final initialising = starting || recordingState == RecordingState.initialising;
    final confirmedPhoneRecording = recordingState == RecordingState.record;
    final confirmedNecklaceRecording = recordingState == RecordingState.deviceRecord;
    final primaryOpensLiveTranscript = !homeCaptureOwned &&
        (confirmedPhoneRecording || (confirmedNecklaceRecording && !necklaceContinuouslyRecording));
    final homeCaptureTransportActive = homeCaptureOwned && (confirmedPhoneRecording || confirmedNecklaceRecording);
    final label = homeCaptureTransportActive
        ? context.l10n.todayRecordListening
        : homeCaptureOwned
            ? context.l10n.stopRecording
            : primaryOpensLiveTranscript
                ? context.l10n.liveTranscript
                : initialising
                    ? context.l10n.initialisingRecorder
                    : necklaceConnecting
                        ? context.l10n.todayStripReconnecting
                        : context.l10n.startRecording;
    final source = necklaceConnected || homeCaptureUsesNecklace
        ? context.l10n.todayRecordWithNecklace
        : context.l10n.todayRecordOnPhone;
    final showLiveTranscript = confirmedPhoneRecording || confirmedNecklaceRecording || homeCaptureOwned;
    final captureFailed = recordingState == RecordingState.error;
    final primaryEnabled = homeCaptureOwned || (!initialising && !necklaceConnecting);
    return Semantics(
      button: true,
      label: '${context.l10n.todayRecordMoment}. $label. $source',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            context.l10n.todayRecordMoment,
            key: const Key('today-record-moment-heading'),
            textAlign: TextAlign.center,
            style: EllaTextStyles.eyebrow.copyWith(color: EllaColors.tealDeep),
          ),
          const SizedBox(height: 8),
          AnimatedContainer(
            duration: reduceMotion ? Duration.zero : const Duration(milliseconds: 240),
            curve: Curves.easeOut,
            height: 72,
            decoration: BoxDecoration(
              color: homeCaptureOwned ? EllaColors.tealDeep : const Color(0xFFD0E4DE),
              borderRadius: BorderRadius.circular(36),
            ),
            child: Material(
              color: Colors.transparent,
              borderRadius: BorderRadius.circular(36),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                key: const Key('today-record-moment'),
                onTap: primaryEnabled ? (primaryOpensLiveTranscript ? onViewTranscript : onTap) : null,
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Container(
                      width: 52,
                      height: 52,
                      alignment: Alignment.center,
                      decoration: const BoxDecoration(color: EllaColors.paper, shape: BoxShape.circle),
                      child: initialising && !homeCaptureOwned
                          ? const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                            )
                          : homeCaptureTransportActive
                              ? const EllaBreathingDot(active: true, live: true, size: 14)
                              : homeCaptureOwned
                                  ? const Icon(Icons.stop_circle_outlined, size: 30, color: EllaColors.tealDeep)
                                  : const Icon(Icons.mic_none_rounded, size: 30, color: EllaColors.tealDeep),
                    ),
                    const SizedBox(width: 16),
                    Flexible(
                      child: Text(
                        label,
                        style: EllaTextStyles.noteBody.copyWith(
                          fontSize: 21,
                          color: homeCaptureOwned ? EllaColors.paper : EllaColors.tealDeep,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            source,
            key: const Key('today-record-source'),
            textAlign: TextAlign.center,
            style: EllaTextStyles.caption.copyWith(fontSize: 14),
          ),
          if (confirmedPhoneRecording || (confirmedNecklaceRecording && homeCaptureOwned)) ...[
            const SizedBox(height: 8),
            Semantics(
              liveRegion: true,
              label: '$source. ${context.l10n.todayRecordListening}',
              child: Row(
                key: const Key('today-confirmed-recording-status'),
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(Icons.sync_rounded, size: 18, color: EllaColors.tealDeep),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      '$source · ${context.l10n.todayRecordListening}',
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.caption.copyWith(
                        color: EllaColors.tealDeep,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (showLiveTranscript && !primaryOpensLiveTranscript) ...[
            const SizedBox(height: 4),
            TextButton.icon(
              key: const Key('today-view-live-transcript'),
              onPressed: onViewTranscript,
              icon: const Icon(Icons.subject_rounded, size: 18),
              label: Text(context.l10n.liveTranscript),
            ),
          ],
          if (homeCaptureOwned && necklaceConnecting) ...[
            const SizedBox(height: 4),
            Semantics(
              liveRegion: true,
              label: context.l10n.todayStripReconnecting,
              child: Row(
                key: const Key('today-recording-reconnecting-status'),
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const EllaBreathingDot(active: true, live: true, size: 10),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      context.l10n.todayStripReconnecting,
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.caption.copyWith(
                        color: EllaColors.tealDeep,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (captureFailed) ...[
            const SizedBox(height: 8),
            Semantics(
              liveRegion: true,
              label: context.l10n.todayRecordingUnavailable,
              child: Row(
                key: const Key('today-recording-error-status'),
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(Icons.error_outline_rounded, size: 18, color: EllaColors.error),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      context.l10n.todayRecordingUnavailable,
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.caption.copyWith(
                        color: EllaColors.error,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (necklaceContinuouslyRecording) ...[
            const SizedBox(height: 8),
            Semantics(
              liveRegion: true,
              label: context.l10n.todayNecklaceRecordingContinuously,
              child: Row(
                key: const Key('today-necklace-continuous-recording'),
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const EllaBreathingDot(active: true, live: true, size: 10),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      context.l10n.todayNecklaceRecordingContinuously,
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.caption.copyWith(color: EllaColors.tealDeep, fontWeight: FontWeight.w700),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _WhispersHomeCard extends StatelessWidget {
  const _WhispersHomeCard({required this.enabled, required this.verified, required this.onOpen});

  final bool enabled;
  final bool verified;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    final status = !verified
        ? context.l10n.todayWhispersUnavailable
        : enabled
            ? context.l10n.todayWhispersOnDescription
            : context.l10n.todayWhispersOffDescription;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(context.l10n.todayWhispersTitle, style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.1)),
        const SizedBox(height: 14),
        EllaCardSurface(
          child: InkWell(
            key: const Key('today-whispers-card'),
            onTap: onOpen,
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 84),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 14),
                child: Row(
                  children: [
                    EllaBreathingDot(active: verified && enabled, live: verified && enabled),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(status, style: EllaTextStyles.secondary),
                          const SizedBox(height: 4),
                          Text(
                            context.l10n.todayWhispersHistory,
                            style: EllaTextStyles.secondary.copyWith(
                              color: EllaColors.tealDeep,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
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
    );
  }
}

class _HomeControlsSheet extends StatelessWidget {
  const _HomeControlsSheet({
    required this.hasNecklace,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.batteryLevel,
    required this.deviceType,
    required this.showWhispers,
    required this.whispersEnabled,
    required this.whispersVerified,
    required this.whispersUpdating,
    required this.onWhispersChanged,
    required this.onWhispersHistory,
    required this.onManageNecklace,
  });

  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final int batteryLevel;
  final DeviceType deviceType;
  final bool showWhispers;
  final bool whispersEnabled;
  final bool whispersVerified;
  final bool whispersUpdating;
  final ValueChanged<bool> onWhispersChanged;
  final VoidCallback onWhispersHistory;
  final VoidCallback onManageNecklace;

  String _recordingSource(BuildContext context) {
    if (!necklaceConnected) return context.l10n.todayRecordOnPhone;
    if (batteryLevel >= 0) return '${context.l10n.todayRecordWithNecklace} · $batteryLevel%';
    return context.l10n.todayRecordWithNecklace;
  }

  @override
  Widget build(BuildContext context) {
    final necklaceGlyph = EllaHardwareArtwork.glyphForDeviceType(deviceType);
    return Padding(
      padding: EdgeInsets.fromLTRB(
        EllaSizes.screenPadding,
        16,
        EllaSizes.screenPadding,
        24 + MediaQuery.paddingOf(context).bottom,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 40,
              height: 4,
              decoration: BoxDecoration(color: EllaColors.cardDeep, borderRadius: BorderRadius.circular(2)),
            ),
          ),
          const SizedBox(height: 20),
          Text(context.l10n.todayControlsTitle, style: EllaTextStyles.noteBody.copyWith(fontSize: 28)),
          const SizedBox(height: 18),
          EllaCardSurface(
            child: Column(
              children: [
                _ControlRow(
                  icon: necklaceConnected && necklaceGlyph != null
                      ? Image.asset(necklaceGlyph, width: 28, height: 28)
                      : const Icon(Icons.phone_iphone_rounded, color: EllaColors.tealDeep),
                  title: context.l10n.todayRecordingSource,
                  detail: _recordingSource(context),
                ),
                const Divider(height: 1, indent: 64, color: EllaColors.cardDeep),
                InkWell(
                  key: const Key('today-manage-necklace'),
                  onTap: onManageNecklace,
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: EllaSizes.listItemMinHeight),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Row(
                        children: [
                          const Icon(Icons.bluetooth_rounded, color: EllaColors.tealDeep),
                          const SizedBox(width: 16),
                          Expanded(
                            child: Text(
                              necklaceConnecting
                                  ? context.l10n.todayStripReconnecting
                                  : hasNecklace && !necklaceConnected
                                      ? context.l10n.todayNecklaceOffReconnect
                                      : context.l10n.todayManageNecklace,
                              style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w600),
                            ),
                          ),
                          const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (showWhispers) ...[
            const SizedBox(height: 12),
            EllaCardSurface(
              child: Column(
                children: [
                  ConstrainedBox(
                    key: const Key('guardian-whispers-control'),
                    constraints: const BoxConstraints(minHeight: 64),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Row(
                        children: [
                          EllaBreathingDot(active: whispersVerified && whispersEnabled),
                          const SizedBox(width: 16),
                          Expanded(
                            child: Text(
                              whispersVerified
                                  ? whisperStatusLead(whispersEnabled)
                                  : context.l10n.todayWhispersUnavailable,
                              style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w600),
                            ),
                          ),
                          if (whispersUpdating)
                            const SizedBox(
                              width: 24,
                              height: 24,
                              child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                            )
                          else
                            Switch(
                              value: whispersEnabled,
                              onChanged: whispersVerified ? onWhispersChanged : null,
                              activeTrackColor: EllaColors.tealDeep,
                              activeThumbColor: EllaColors.paper,
                            ),
                        ],
                      ),
                    ),
                  ),
                  const Divider(height: 1, indent: 64, color: EllaColors.cardDeep),
                  InkWell(
                    key: const Key('whispers-history-entry'),
                    onTap: onWhispersHistory,
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(minHeight: EllaSizes.listItemMinHeight),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Row(
                          children: [
                            const SizedBox(width: 40),
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
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _ControlRow extends StatelessWidget {
  const _ControlRow({required this.icon, required this.title, required this.detail});

  final Widget icon;
  final String title;
  final String detail;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 68),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            SizedBox(width: 32, child: Center(child: icon)),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(title, style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 2),
                  Text(detail, style: EllaTextStyles.secondary),
                ],
              ),
            ),
          ],
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

/// The journal is the Home product: real source photos when present, with a
/// house-style app-owned illustration only when a memory has no source media.
class _RecentMemories extends StatelessWidget {
  const _RecentMemories({
    required this.conversations,
    required this.refreshing,
    required this.deletingConversationIds,
    required this.onDelete,
    required this.onOpenAll,
  });

  final List<ServerConversation> conversations;
  final bool refreshing;
  final Set<String> deletingConversationIds;
  final ValueChanged<ServerConversation> onDelete;
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
            Expanded(
              child: Text(
                context.l10n.todayRecentMemories,
                style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.1),
              ),
            ),
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
                    context.l10n.todaySeeAllMemories,
                    style: EllaTextStyles.secondary.copyWith(color: EllaColors.tealDeep, fontWeight: FontWeight.w700),
                  ),
                ),
              ),
          ],
        ),
        const SizedBox(height: 14),
        if (conversations.isEmpty)
          const _MemoryJournalEmptyState()
        else
          LayoutBuilder(
            builder: (context, constraints) {
              final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
              final stack = constraints.maxWidth < 320 || textScale > 1.3 || conversations.length == 1;
              final cards = conversations
                  .map(
                    (conversation) => _MemoryJournalCard(
                      conversation: conversation,
                      compact: !stack,
                      deleting: deletingConversationIds.contains(conversation.id),
                      onDelete: () => onDelete(conversation),
                      onTap: () => _openDetail(context, conversation),
                    ),
                  )
                  .toList(growable: false);
              if (stack) {
                return Column(
                  children: [
                    for (var index = 0; index < cards.length; index++) ...[
                      cards[index],
                      if (index < cards.length - 1) const SizedBox(height: EllaSizes.cardGap),
                    ],
                  ],
                );
              }
              return Column(
                children: [
                  for (var index = 0; index < cards.length; index += 2) ...[
                    IntrinsicHeight(
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Expanded(child: cards[index]),
                          const SizedBox(width: EllaSizes.cardGap),
                          Expanded(child: index + 1 < cards.length ? cards[index + 1] : const SizedBox.shrink()),
                        ],
                      ),
                    ),
                    if (index + 2 < cards.length) const SizedBox(height: EllaSizes.cardGap),
                  ],
                ],
              );
            },
          ),
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

class _MemoryJournalCard extends StatelessWidget {
  const _MemoryJournalCard({
    required this.conversation,
    required this.compact,
    required this.deleting,
    required this.onDelete,
    required this.onTap,
  });

  final ServerConversation conversation;
  final bool compact;
  final bool deleting;
  final VoidCallback onDelete;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final overview = _memoryOverview(conversation);
    final date = DateFormat('MMM d, y').format(conversation.startedAt ?? conversation.createdAt);
    return EllaCardSurface(
      borderRadius: 20,
      color: const Color(0xFFF8F1E8),
      child: InkWell(
        key: Key('memory-journal-card-${conversation.id}'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            AspectRatio(
              aspectRatio: compact ? 1.28 : 2.1,
              child: _MemoryArtwork(conversation: conversation),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 16, 18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        child: Text(
                          _memoryTitle(conversation),
                          maxLines: compact ? 2 : null,
                          overflow: compact ? TextOverflow.ellipsis : null,
                          style: EllaTextStyles.noteBody.copyWith(fontSize: compact ? 20 : 22, height: 1.15),
                        ),
                      ),
                      const SizedBox(width: 6),
                      if (deleting)
                        const SizedBox(
                          key: Key('home-deleting-memory-progress'),
                          width: EllaSizes.minTouchTarget,
                          height: EllaSizes.minTouchTarget,
                          child: Center(
                            child: SizedBox(
                              width: 20,
                              height: 20,
                              child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                            ),
                          ),
                        )
                      else
                        IconButton(
                          key: Key('home-delete-memory-${conversation.id}'),
                          tooltip: context.l10n.deleteMemory,
                          onPressed: onDelete,
                          constraints: const BoxConstraints(
                            minWidth: EllaSizes.minTouchTarget,
                            minHeight: EllaSizes.minTouchTarget,
                          ),
                          icon: const Icon(Icons.delete_outline_rounded, color: EllaColors.warning),
                        ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  Text(
                    date,
                    style: EllaTextStyles.caption.copyWith(
                      color: EllaColors.warning,
                      fontSize: 14,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  if (overview.isNotEmpty) ...[
                    const SizedBox(height: 12),
                    Container(width: 28, height: 1, color: EllaColors.warning),
                    const SizedBox(height: 10),
                    Text(
                      overview,
                      maxLines: compact ? 3 : null,
                      overflow: compact ? TextOverflow.ellipsis : null,
                      style: EllaTextStyles.secondary.copyWith(fontSize: compact ? 14 : 16),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MemoryArtwork extends StatelessWidget {
  const _MemoryArtwork({required this.conversation});

  final ServerConversation conversation;

  Uint8List? _photoBytes() {
    for (final photo in conversation.photos) {
      if (photo.discarded || photo.base64.trim().isEmpty) continue;
      try {
        return base64Decode(photo.base64);
      } on FormatException {
        continue;
      }
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final bytes = _photoBytes();
    if (bytes != null) {
      return Semantics(
        image: true,
        label: context.l10n.todayMemoryPhotoLabel,
        child: Image.memory(
          key: const Key('memory-source-photo'),
          bytes,
          fit: BoxFit.cover,
          gaplessPlayback: true,
          errorBuilder: (_, __, ___) => const _MemoryFallbackArtwork(),
        ),
      );
    }
    return const _MemoryFallbackArtwork();
  }
}

class _MemoryFallbackArtwork extends StatelessWidget {
  const _MemoryFallbackArtwork();

  @override
  Widget build(BuildContext context) {
    return ExcludeSemantics(
      child: Image.asset(
        key: const Key('memory-fallback-art'),
        'assets/images/ella-memory-watercolor-fallback.png',
        fit: BoxFit.cover,
        alignment: Alignment.center,
      ),
    );
  }
}

class _MemoryJournalEmptyState extends StatelessWidget {
  const _MemoryJournalEmptyState();

  @override
  Widget build(BuildContext context) {
    return EllaCardSurface(
      key: const Key('memory-journal-empty'),
      borderRadius: 20,
      color: const Color(0xFFF8F1E8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const AspectRatio(aspectRatio: 2.4, child: _MemoryFallbackArtwork()),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 22),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  context.l10n.todayFirstMemoryHeadline,
                  style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.15),
                ),
                const SizedBox(height: 8),
                Text(context.l10n.todayFirstMemoryBody, style: EllaTextStyles.secondary),
              ],
            ),
          ),
        ],
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
