import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
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
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
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

enum TodayCaptureDockMode { ready, starting, recording, unavailable }

class TodayCaptureDockPresentation {
  const TodayCaptureDockPresentation({
    required this.mode,
    required this.status,
    required this.primaryLabel,
    required this.primaryIcon,
    required this.opensTranscript,
    required this.primaryEnabled,
  });

  final TodayCaptureDockMode mode;
  final String status;
  final String primaryLabel;
  final IconData primaryIcon;
  final bool opensTranscript;
  final bool primaryEnabled;

  bool get emphasized => mode == TodayCaptureDockMode.ready || mode == TodayCaptureDockMode.recording;
}

String whisperStatusLead(bool enabled) => enabled ? 'Whispers are on' : 'Whispers are off';

bool canReadDailyNote({required bool loading, required String text}) => !loading && text.trim().isNotEmpty;

String whisperStatusDetail(bool enabled) => enabled
    ? ' — Ella will speak up when she can help. 🪽'
    : " — Ella stays quiet, but she's still listening and remembering.";

List<ServerConversation> homeMemoryCanvasSelection(List<ServerConversation> conversations, {int limit = 2}) {
  final sorted = List<ServerConversation>.of(conversations)
    ..sort((a, b) => (b.startedAt ?? b.createdAt).compareTo(a.startedAt ?? a.createdAt));
  return sorted.take(limit).toList(growable: false);
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
  bool _whisperStateLoading = false;
  bool _whisperStateReloadPending = false;
  bool _updatingWhispers = false;

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

  void _onProvisioningChanged() {
    unawaited(_syncTodayCardAuthority());
    if (_guardianAvailable && !_whispersVerified) unawaited(_loadWhisperState());
  }

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
    if (mounted) {
      setState(() {
        // Never retain a previous account's mode while exact authority changes.
        _whispersOn = false;
        _whispersVerified = false;
      });
    }
    if (_guardianAvailable) unawaited(_loadWhisperState());
    unawaited(_syncTodayCardAuthority(forceReload: true));
  }

  void _onTodayCardChanged() {
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      unawaited(_syncTodayCardAuthority(forceReload: true));
      if (_guardianAvailable) unawaited(_loadWhisperState());
    }
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
    if (_whisperStateLoading) {
      _whisperStateReloadPending = true;
      return;
    }
    _whisperStateLoading = true;
    try {
      do {
        _whisperStateReloadPending = false;
        await _loadWhisperStateOnce();
      } while (_whisperStateReloadPending && mounted && _guardianAvailable);
    } finally {
      _whisperStateLoading = false;
    }
  }

  Future<void> _loadWhisperStateOnce() async {
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

  Future<bool> _finalizeHomeMoment(CaptureProvider capture, {bool Function()? isCurrent}) async {
    final finalized = capture.recordingState == RecordingState.deviceRecord
        ? await capture.finalizeCurrentDeviceConversationAndContinue()
        : await capture.finalizeCurrentConversation();
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
    bool? transportFinalized;
    switch (source) {
      case _HomeCaptureSource.phone:
        transportFinalized = await capture.stopStreamRecordingAndFinalize();
        break;
      case _HomeCaptureSource.necklaceOwned:
        transportFinalized = await capture.stopStreamDeviceRecordingAndFinalize();
        if (capture.recordingState == RecordingState.deviceRecord) {
          throw StateError('Home-owned necklace stream did not stop');
        }
        break;
      case _HomeCaptureSource.necklaceContinuous:
        break;
      case null:
        if (capture.recordingState == RecordingState.record) {
          transportFinalized = await capture.stopStreamRecordingAndFinalize();
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
    if (transportFinalized != null) {
      if (transportFinalized) {
        if (mounted) {
          setState(() {
            _homeCaptureFinalizationPending = false;
            _homeCaptureSource = null;
          });
        }
        return true;
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayNoWordsCaptured)));
      }
      return false;
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
            if (capture.phoneCaptureOwnsMobileAudio || capture.recordingState == RecordingState.record) {
              return capture.stopStreamRecordingAndFinalize();
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
          if (capture.hasCapturableContent || capture.hasActiveDeviceCaptureBoundaryEvidence) {
            if (!await _finalizeHomeMoment(capture)) return;
          }
        }
        if (!mounted) return;
        final started = capture.recordingState == RecordingState.deviceRecord;
        if (started) {
          setState(() {
            _homeCaptureActive = true;
            _homeCaptureSource = startedHere ? _HomeCaptureSource.necklaceOwned : _HomeCaptureSource.necklaceContinuous;
          });
          return;
        }
        // A paired necklace must not make the phone recorder unavailable.
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

  void _recordFromMemoryArchive() {
    if (!mounted) return;
    final device = context.read<DeviceProvider>();
    final connectedDevice = device.presentationConnectedDevice;
    final deviceConnected = device.presentationIsConnected && connectedDevice != null;
    unawaited(
      _toggleHomeCapture(
        capture: context.read<CaptureProvider>(),
        isActive: _homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null,
        necklaceConnected: deviceConnected,
        connectedDevice: connectedDevice,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final now = SharedPreferencesUtil.isTodayDesignPreviewEnabled
        ? DateTime(2025, 7, 24, 9, 41)
        : (widget.nowProvider?.call() ?? DateTime.now());
    final device = context.watch<DeviceProvider>();
    final connectedDevice = device.presentationConnectedDevice;
    final deviceConnected = device.presentationIsConnected && connectedDevice != null;
    final hasNecklace = device.presentationPairedDevice != null;
    final deviceType =
        device.presentationConnectedDevice?.type ?? device.presentationPairedDevice?.type ?? DeviceType.omi;
    final capture = context.watch<CaptureProvider>();
    final conversations = context.watch<ConversationProvider>();
    final visibleConversations = conversations.visibleConversations;
    final canvasMemories = homeMemoryCanvasSelection(visibleConversations);
    final heroMemory = canvasMemories.isEmpty ? null : canvasMemories.first;
    final continuityMemory = canvasMemories.length > 1 ? canvasMemories[1] : null;
    final showGuardianSurfaces = _guardianAvailable;
    final homeCaptureOwned =
        _homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null;
    final homeCaptureUsesNecklace = _homeCaptureSource == _HomeCaptureSource.necklaceOwned ||
        _homeCaptureSource == _HomeCaptureSource.necklaceContinuous;
    final dockClearance = todayDockScrollClearance(
      textScale: MediaQuery.textScalerOf(context).scale(1),
      safeBottom: MediaQuery.paddingOf(context).bottom,
    );

    void openControls() => unawaited(
          _showHomeControls(
            hasNecklace: hasNecklace,
            necklaceConnected: deviceConnected,
            necklaceConnecting: device.isConnecting,
            batteryLevel: device.presentationBatteryLevel,
            deviceType: deviceType,
            showGuardianSurfaces: showGuardianSurfaces,
          ),
        );
    return SafeArea(
      bottom: false,
      child: Stack(
        children: [
          RefreshIndicator(
            color: EllaColors.tealDeep,
            backgroundColor: EllaColors.card,
            onRefresh: () async {
              await Future.wait([
                _todayCardController.retry(),
                if (showGuardianSurfaces) _loadWhisperState(),
                context.read<ConversationProvider>().getInitialConversations(),
              ]);
            },
            child: ListView(
              key: const Key('today-scroll'),
              controller: _scrollController,
              physics: const AlwaysScrollableScrollPhysics(),
              padding: EdgeInsets.fromLTRB(EllaSizes.screenPadding, 14, EllaSizes.screenPadding, dockClearance),
              children: [
                _TodayHeader(now: now),
                const SizedBox(height: 22),
                if (heroMemory == null)
                  const _MemoryJournalEmptyState()
                else
                  _MemoryJournalCard(conversation: heroMemory, onTap: () => _openMemoryDetail(heroMemory)),
                const SizedBox(height: 18),
                TodayCardSurface(
                  compact: true,
                  surfaceColor: EllaColors.elevatedCard,
                  state: _todayCardController.state,
                  onReadMore: _todayCardController.state.card == null ? null : _openTodayCardDetail,
                  onTalk: _todayCardController.state.card == null ? null : _openTodayCardTalk,
                ),
                if (continuityMemory != null) ...[
                  const SizedBox(height: 18),
                  _MemoryJournalCard(
                    conversation: continuityMemory,
                    compact: true,
                    onTap: () => _openMemoryDetail(continuityMemory),
                  ),
                ],
                if (visibleConversations.isNotEmpty) ...[
                  const SizedBox(height: 6),
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton(
                      key: const Key('memories-see-all'),
                      onPressed: () => Navigator.of(
                        context,
                      ).push(MaterialPageRoute(builder: (_) => EllaMemoriesPage(onRecord: _recordFromMemoryArchive))),
                      style: TextButton.styleFrom(
                        foregroundColor: EllaColors.tealDeep,
                        minimumSize: const Size(0, EllaSizes.minTouchTarget),
                      ),
                      child: Text(context.l10n.todaySeeAllMemories),
                    ),
                  ),
                ],
              ],
            ),
          ),
          Positioned(
            left: 14,
            right: 14,
            bottom: EllaSizes.navBarHeight + MediaQuery.paddingOf(context).bottom + 16,
            child: TodayRecordMomentControl(
              homeCaptureOwned: homeCaptureOwned,
              homeCaptureUsesNecklace: homeCaptureUsesNecklace,
              starting: _homeCaptureStarting,
              hasNecklace: hasNecklace,
              necklaceConnected: deviceConnected,
              necklaceConnecting: device.isConnecting,
              recordingState: capture.recordingState,
              diagnostics: capture.captureDiagnostics,
              necklaceContinuouslyRecording:
                  deviceConnected && capture.recordingState == RecordingState.deviceRecord && !homeCaptureOwned,
              showWhispers: showGuardianSurfaces,
              whispersEnabled: _whispersOn,
              whispersVerified: _whispersVerified,
              onOpenControls: openControls,
              onOpenWhispers: () =>
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage())),
              onViewTranscript: () => _openLiveTranscript(capture),
              onUnavailable: () => ScaffoldMessenger.of(
                context,
              ).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable))),
              onTap: () => _toggleHomeCapture(
                capture: capture,
                isActive: homeCaptureOwned,
                necklaceConnected: deviceConnected,
                connectedDevice: connectedDevice,
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _openMemoryDetail(ServerConversation conversation) {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation)));
  }
}

double todayDockScrollClearance({required double textScale, required double safeBottom}) =>
    EllaSizes.navBarHeight + safeBottom + 156 * textScale.clamp(1.0, 2.0) + 24;

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
  const _TodayHeader({required this.now});

  final DateTime now;

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
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(date, style: EllaTextStyles.eyebrow),
        const SizedBox(height: 8),
        Text(greeting, style: EllaTextStyles.noteBody.copyWith(fontSize: 24, height: 1.14)),
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
    this.hasNecklace = false,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.recordingState,
    this.diagnostics = const CaptureDiagnostics(),
    required this.necklaceContinuouslyRecording,
    this.showWhispers = false,
    this.whispersEnabled = false,
    this.whispersVerified = false,
    this.onOpenControls,
    this.onOpenWhispers,
    required this.onViewTranscript,
    this.onUnavailable,
    required this.onTap,
  });

  final bool homeCaptureOwned;
  final bool homeCaptureUsesNecklace;
  final bool starting;
  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final RecordingState recordingState;
  final CaptureDiagnostics diagnostics;
  final bool necklaceContinuouslyRecording;
  final bool showWhispers;
  final bool whispersEnabled;
  final bool whispersVerified;
  final VoidCallback? onOpenControls;
  final VoidCallback? onOpenWhispers;
  final VoidCallback onViewTranscript;
  final VoidCallback? onUnavailable;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final initialising = starting || recordingState == RecordingState.initialising;
    final phoneRecording = recordingState == RecordingState.record;
    final necklaceRecording = recordingState == RecordingState.deviceRecord;
    final active = homeCaptureOwned && (phoneRecording || necklaceRecording);
    final usesNecklace = homeCaptureUsesNecklace ||
        necklaceRecording ||
        (initialising && diagnostics.source == CaptureDiagnosticSource.necklace) ||
        (!phoneRecording && !initialising && necklaceConnected);
    final presentation = _presentation(
      context,
      initialising: initialising,
      phoneRecording: phoneRecording,
      necklaceRecording: necklaceRecording,
      usesNecklace: usesNecklace,
    );

    return Material(
      key: const Key('today-capture-dock'),
      elevation: 12,
      shadowColor: Colors.black.withValues(alpha: 0.14),
      color: EllaColors.elevatedCard,
      borderRadius: BorderRadius.circular(24),
      clipBehavior: Clip.antiAlias,
      child: DecoratedBox(
        decoration: BoxDecoration(
          border: Border.all(color: EllaColors.cardEdge),
          borderRadius: BorderRadius.circular(24),
        ),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              InkWell(
                key: const Key('today-dock-status'),
                onTap: onOpenControls,
                borderRadius: BorderRadius.circular(16),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    child: Row(
                      children: [
                        EllaBreathingDot(active: active || necklaceContinuouslyRecording, live: active),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Semantics(
                            liveRegion: true,
                            child: Text(
                              presentation.status,
                              key: const Key('today-record-source'),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: EllaTextStyles.caption.copyWith(
                                color: EllaColors.inkSoft,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 4),
              Row(
                children: [
                  Expanded(
                    child: _TodayDockAction(
                      actionKey: const Key('today-record-moment'),
                      icon: presentation.primaryIcon,
                      label: presentation.primaryLabel,
                      emphasized: presentation.emphasized,
                      enabled: presentation.primaryEnabled,
                      onDisabledTap: presentation.mode == TodayCaptureDockMode.unavailable ? onUnavailable : null,
                      onTap: presentation.opensTranscript ? onViewTranscript : onTap,
                    ),
                  ),
                  if (!presentation.opensTranscript) ...[
                    const SizedBox(width: 6),
                    Expanded(
                      child: _TodayDockAction(
                        actionKey: const Key('today-view-live-transcript'),
                        icon: Icons.subject_rounded,
                        label: context.l10n.transcript,
                        onTap: onViewTranscript,
                      ),
                    ),
                  ],
                  if (showWhispers) ...[
                    const SizedBox(width: 6),
                    Expanded(
                      child: _TodayDockAction(
                        actionKey: const Key('today-whispers-card'),
                        icon: Icons.record_voice_over_rounded,
                        label: context.l10n.todayWhispersTitle,
                        selected: whispersEnabled && whispersVerified,
                        onTap: onOpenWhispers,
                      ),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  TodayCaptureDockPresentation _presentation(
    BuildContext context, {
    required bool initialising,
    required bool phoneRecording,
    required bool necklaceRecording,
    required bool usesNecklace,
  }) {
    final status = initialising
        ? context.l10n.todayDockStarting
        : recordingState == RecordingState.error
            ? context.l10n.todayDockRecordingUnavailable
            : necklaceConnecting
                ? context.l10n.todayDockNecklaceConnecting
                : phoneRecording
                    ? context.l10n.todayDockRecordingPhone
                    : necklaceRecording || (homeCaptureOwned && usesNecklace)
                        ? context.l10n.todayDockRecordingNecklace
                        : hasNecklace && necklaceConnected
                            ? context.l10n.todayDockNecklaceReady
                            : hasNecklace
                                ? context.l10n.todayDockNecklaceNotConnected
                                : context.l10n.todayDockPhoneReady;
    final opensTranscript =
        !homeCaptureOwned && (phoneRecording || (necklaceRecording && !necklaceContinuouslyRecording));
    if (initialising) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.starting,
        status: status,
        primaryLabel: context.l10n.todayDockRecord,
        primaryIcon: Icons.mic_none_rounded,
        opensTranscript: false,
        primaryEnabled: false,
      );
    }
    if (recordingState == RecordingState.error && !homeCaptureOwned) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.unavailable,
        status: status,
        primaryLabel: context.l10n.todayDockRecord,
        primaryIcon: Icons.mic_none_rounded,
        opensTranscript: false,
        primaryEnabled: false,
      );
    }
    if (opensTranscript) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.recording,
        status: status,
        primaryLabel: context.l10n.transcript,
        primaryIcon: Icons.subject_rounded,
        opensTranscript: true,
        primaryEnabled: true,
      );
    }
    if (homeCaptureOwned) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.recording,
        status: status,
        primaryLabel: context.l10n.todayDockFinish,
        primaryIcon: Icons.stop_rounded,
        opensTranscript: false,
        primaryEnabled: true,
      );
    }
    return TodayCaptureDockPresentation(
      mode: TodayCaptureDockMode.ready,
      status: status,
      primaryLabel: context.l10n.todayDockRecord,
      primaryIcon: Icons.mic_none_rounded,
      opensTranscript: false,
      primaryEnabled: true,
    );
  }
}

class _TodayDockAction extends StatelessWidget {
  const _TodayDockAction({
    required this.actionKey,
    required this.icon,
    required this.label,
    required this.onTap,
    this.emphasized = false,
    this.selected = false,
    this.enabled = true,
    this.onDisabledTap,
  });

  final Key actionKey;
  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final bool emphasized;
  final bool selected;
  final bool enabled;
  final VoidCallback? onDisabledTap;

  @override
  Widget build(BuildContext context) {
    final foreground = emphasized
        ? EllaColors.paper
        : selected
            ? EllaColors.tealDeep
            : EllaColors.inkSoft;
    return Semantics(
      button: true,
      enabled: enabled,
      selected: selected,
      label: label,
      excludeSemantics: true,
      child: Material(
        color: emphasized
            ? EllaColors.tealDeep
            : selected
                ? const Color(0xFFD0E4DE)
                : EllaColors.paper,
        borderRadius: BorderRadius.circular(16),
        child: InkWell(
          key: actionKey,
          onTap: enabled ? onTap : onDisabledTap,
          borderRadius: BorderRadius.circular(16),
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 58),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 7),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(icon, size: 22, color: enabled ? foreground : EllaColors.cardEdge),
                  const SizedBox(height: 3),
                  Text(
                    label,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.center,
                    style: EllaTextStyles.caption.copyWith(
                      color: enabled ? foreground : EllaColors.cardEdge,
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
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

String _memoryTitle(ServerConversation conversation, String fallback) {
  var title = conversation.structured.title
      .replaceFirst(RegExp(r'^🪽\s*'), '')
      .replaceFirst(RegExp(r'^(?:\[[^\]]+\]\s*)+'), '')
      .trim();
  title = title.split(RegExp(r'\s*(?:,|\band\b)\s*', caseSensitive: false)).first.trim();
  title = title.replaceAll(
    RegExp(
      r'\b(?:doctor|medical|clinical|monitoring|emergency|alert|tracking|detecting)(?:[- ]\w+)?\b',
      caseSensitive: false,
    ),
    '',
  );
  final words = title.split(RegExp(r'\s+')).where((word) => word.isNotEmpty).take(4).toList();
  return words.isEmpty ? fallback : words.join(' ');
}

String _memoryOverview(ServerConversation conversation) =>
    conversation.structured.overview.replaceFirst(RegExp(r'^\[Ella\]\s*'), '').trim();

class _MemoryJournalCard extends StatelessWidget {
  const _MemoryJournalCard({required this.conversation, required this.onTap, this.compact = false});

  final ServerConversation conversation;
  final VoidCallback onTap;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final overview = _memoryOverview(conversation);
    final date = DateFormat('MMM d, y').format(conversation.startedAt ?? conversation.createdAt);
    return EllaCardSurface(
      borderRadius: 20,
      color: EllaColors.elevatedCard,
      child: InkWell(
        key: Key('memory-journal-card-${conversation.id}'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            AspectRatio(
              aspectRatio: compact ? 16 / 7 : 3 / 2,
              child: _MemoryArtwork(conversation: conversation),
            ),
            Padding(
              padding: EdgeInsets.fromLTRB(16, compact ? 12 : 14, 16, compact ? 14 : 18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    _memoryTitle(conversation, context.l10n.untitledConversation),
                    maxLines: 2,
                    overflow: TextOverflow.fade,
                    style: EllaTextStyles.noteBody.copyWith(fontSize: compact ? 20 : 24, height: 1.12),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    date,
                    style: EllaTextStyles.caption.copyWith(
                      color: EllaColors.inkSoft,
                      fontSize: 14,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  if (!compact && overview.isNotEmpty) ...[
                    const SizedBox(height: 12),
                    Container(width: 28, height: 1, color: EllaColors.teal),
                    const SizedBox(height: 10),
                    Text(
                      overview,
                      maxLines: 3,
                      overflow: TextOverflow.fade,
                      style: EllaTextStyles.secondary.copyWith(fontSize: 16),
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

  @override
  Widget build(BuildContext context) => MemoryArtworkImage(conversation: conversation);
}

class _MemoryFallbackArtwork extends StatelessWidget {
  const _MemoryFallbackArtwork();

  @override
  Widget build(BuildContext context) {
    return ExcludeSemantics(
      child: Image.asset(
        key: const Key('memory-fallback-art'),
        'assets/images/ella-memory-topics/quiet.webp',
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
      color: EllaColors.elevatedCard,
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
                  context.l10n.todayMemoryCanvasEmptyHeadline,
                  style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.15),
                ),
                const SizedBox(height: 8),
                Text(context.l10n.todayMemoryCanvasEmptyBody, style: EllaTextStyles.secondary),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
