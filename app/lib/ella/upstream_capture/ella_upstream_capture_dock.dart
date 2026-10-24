import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'dart:async';
import 'package:omi/ella/widgets/iris_capture_controls.dart';
import 'package:omi/stock_capture/pages/conversations/widgets/live_capture_card.dart';
import 'package:omi/stock_capture/widgets/capture_sources.dart';
import 'package:omi/ella/widgets/iris_whisper_stop_bar.dart';
import 'package:omi/backend/preferences.dart' as ella_preferences;
import 'package:share_plus/share_plus.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_svg/flutter_svg.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/capture_word_receipt.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/upstream_capture/transcript_view_diagnostics.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/guardian_mode_service.dart' as guardian_native;
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/home/today_page.dart'
    show GuardianAvailability, GuardianModeLoader, GuardianModeSetter, GuardianNativeLifecycle;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef EllaCaptureConsentRequester = Future<bool> Function(BuildContext context);
typedef GuardianNativeStateReader = guardian_native.GuardianModeState Function();

enum _DockOperation { idle, booting, searching, connecting, stopping, pausing, resuming, disconnecting, finishing }

enum _WhisperPlaybackState { unknown, ready, unavailable, error }

/// Flag-ON home capture dock: a thin view over upstream's CaptureProvider.
/// Every capture action remains one call into [EllaUpstreamCaptureRuntime].
class EllaUpstreamCaptureDock extends StatefulWidget {
  const EllaUpstreamCaptureDock({
    super.key,
    EllaUpstreamCaptureRuntime? runtime,
    this.authenticatedUid,
    this.consentRequester,
    this.guardianAvailability,
    this.guardianModeLoader,
    this.guardianModeSetter,
    this.guardianNativeStart,
    this.guardianNativeStop,
    this.guardianNativeState,
    this.guardianNativeStates,
    this.guardianAuthorityProvider,
    this.expandControlsForTesting = false,
    this.headerControl = false,
  }) : _runtime = runtime;

  /// Compact control in the main chat header; full controls remain in the sheet.
  final bool headerControl;

  @visibleForTesting
  final bool expandControlsForTesting;

  final EllaUpstreamCaptureRuntime? _runtime;
  final String Function()? authenticatedUid;
  final EllaCaptureConsentRequester? consentRequester;
  final GuardianAvailability? guardianAvailability;
  final GuardianModeLoader? guardianModeLoader;
  final GuardianModeSetter? guardianModeSetter;
  final GuardianNativeLifecycle? guardianNativeStart;
  final GuardianNativeLifecycle? guardianNativeStop;
  final GuardianNativeStateReader? guardianNativeState;
  final Stream<guardian_native.GuardianModeState>? guardianNativeStates;
  final guardian_native.GuardianWhisperAuthorityProvider? guardianAuthorityProvider;

  @override
  State<EllaUpstreamCaptureDock> createState() => _EllaUpstreamCaptureDockState();
}

class _EllaUpstreamCaptureDockState extends State<EllaUpstreamCaptureDock> {
  late final EllaUpstreamCaptureRuntime _runtime = widget._runtime ?? EllaUpstreamCaptureRuntime.instance;
  final ValueNotifier<int> _presentationRevision = ValueNotifier(0);
  final FocusNode _capsuleFocusNode = FocusNode(debugLabel: 'Open capture controls');
  bool _controlsOpen = false;
  Timer? _controlsAuthorityTimer;
  NavigatorState? _controlsNavigator;
  Route<dynamic>? _controlsRoute;

  void _setViewState(VoidCallback change) {
    setState(change);
    _presentationRevision.value++;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) IrisCaptureControls.shared.changed();
    });
  }

  final FocusNode _connectFocusNode = FocusNode(debugLabel: 'Connect necklace');
  final FocusNode _transcriptFocusNode = FocusNode(debugLabel: 'Open transcript');
  CaptureProvider? _provider;
  final CaptureWordReceipt _wordReceipt = CaptureWordReceipt();
  Timer? _wordReceiptTimer;
  _DockOperation _operation = _DockOperation.booting;
  String? _message;
  bool _capturePauseUnconfirmed = false;
  bool _protocolMessageActive = false;
  bool _necklaceRetryAvailable = false;
  bool _transcriptOpen = false;
  bool _whispersVerified = false;
  bool _whispersOn = false;
  bool _whispersBusy = false;
  _WhisperPlaybackState _whisperPlayback = _WhisperPlaybackState.unknown;
  String? _whisperError;
  StreamSubscription<guardian_native.GuardianModeState>? _whisperStateSubscription;
  final _whisperFence = guardian_native.GuardianModeService.whisperStateFence;
  int _whisperSharedRevision = 0;
  int _whisperRefreshRevision = 0;
  guardian_native.GuardianWhisperOperation? _pendingWhisperChoice;

  String get _uid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;
  int _operationRevision = 0;

  bool get _busy => _operation != _DockOperation.idle;
  bool get _canShowNecklaceConnect {
    final provider = _provider;
    return provider != null &&
        _runtime.captureSession == null &&
        !provider.havingRecordingDevice &&
        !provider.isPhoneMicBatchRecording &&
        (provider.recordingState == RecordingState.stop || provider.recordingState == RecordingState.error);
  }

  bool get _guardianAvailable => widget.guardianAvailability?.call() ?? allowsGuardianSurface();

  void _actionFeedback() {
    if (!mounted || _uid.isEmpty) return;
    // Feedback acknowledges the tap, not successful capture or a server write.
    unawaited(HapticFeedback.lightImpact().catchError((_) {}));
  }

  bool Function() _capturePressAdmission() {
    final originUid = _uid;
    final originEpoch = _runtime.authority.bindingEpoch;
    return () =>
        mounted &&
        originUid.isNotEmpty &&
        _uid == originUid &&
        _runtime.authority.bindingEpoch == originEpoch &&
        !_busy;
  }

  @override
  void initState() {
    super.initState();
    IrisCaptureControls.shared.register(
      this,
      status: _openControls,
      whispers: _openWhisperControls,
      necklace: _openHardwareControls,
      necklaceDisconnected: () => mounted && _canShowNecklaceConnect,
      captureSource: () {
        final session = _runtime.captureSession;
        if (!mounted || session == null || session.uid != _uid || !_runtime.isCurrentCaptureSession(session))
          return null;
        if (session.source == 'phone') return IrisCaptureSource.phone;
        return session.source.isNotEmpty ? IrisCaptureSource.necklace : null;
      },
      whispersEnabled: () => _whispersVerified && _whisperFence.snapshot != null ? _whispersOn : null,
      whisperPlayback: () {
        if (!_whispersVerified || _whisperFence.snapshot == null) return IrisWhisperPlayback.unknown;
        return switch (_whisperPlayback) {
          _WhisperPlaybackState.ready => IrisWhisperPlayback.ready,
          _WhisperPlaybackState.unavailable => IrisWhisperPlayback.unavailable,
          _WhisperPlaybackState.error => IrisWhisperPlayback.error,
          _WhisperPlaybackState.unknown => IrisWhisperPlayback.unknown,
        };
      },
      listening: () {
        final session = _runtime.captureSession;
        final provider = _provider;
        final current = mounted && session != null && session.uid == _uid &&
            _runtime.isCurrentCaptureSession(session);
        return projectIrisListening(
          current: current,
          paused: current && _runtime.isCapturePaused(session),
          protocolUnavailable: _runtime.protocolUnavailable.value,
          transcriptReady: provider?.transcriptServiceReady == true,
          captureRunning: provider?.recordingState == RecordingState.record ||
              provider?.recordingState == RecordingState.deviceRecord,
        );
      },
    );
    _runtime.protocolUnavailable.addListener(_onProtocolStatus);
    _whisperSharedRevision = _whisperFence.revision;
    _whisperRefreshRevision = _whisperFence.refreshRevision;
    _whisperFence.addListener(_onSharedWhisperStateChanged);
    _whisperStateSubscription =
        (widget.guardianNativeStates ?? guardian_native.GuardianModeService().stateStream).listen((_) {
      if (mounted && _whispersVerified && !_whispersBusy && _whisperFence.snapshot != null) {
        _setViewState(() => _whisperPlayback = _playbackStateFor(_whispersOn));
      }
    });
    unawaited(_boot());
    if (whisperDeliveryProtocol1) WhisperDeliveryRuntime.shared.addListener(_strictChanged);
    unawaited(_loadWhispersState());
  }

  Future<void> _boot() async {
    if (mounted) {
      _setViewState(() {
        _operation = _DockOperation.booting;
        _message = null;
      });
    }
    try {
      final provider = await _runtime.ensureBooted();
      if (!mounted) return;
      _setViewState(() {
        _provider?.removeListener(_observeWords);
        _provider = provider;
        _observeWords();
        provider.addListener(_observeWords);
        _operation = _DockOperation.idle;
      });
    } catch (_) {
      if (!mounted) return;
      _setViewState(() {
        _operation = _DockOperation.idle;
        _message = context.l10n.upstreamCaptureUnavailable;
      });
    }
  }

  String? _currentWordSession() {
    final session = _runtime.captureSession;
    return session == null || session.uid != _uid
        ? null
        : '${session.uid}:${session.bindingEpoch}:${session.recordingId}:${session.windowId}:${session.source}';
  }

  void _observeWords() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) IrisCaptureControls.shared.changed();
    });
    _wordReceipt.observe(
      session: _currentWordSession(),
      words: _provider?.segments.map((s) => (id: s.id, text: s.text)).toList() ?? [],
      now: DateTime.now(),
    );
    _wordReceiptTimer?.cancel();
    if (_wordReceipt.recent(DateTime.now(), session: _currentWordSession())) {
      _wordReceiptTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
        if (mounted) _presentationRevision.value++;
        if (!mounted || !_wordReceipt.recent(DateTime.now(), session: _currentWordSession())) timer.cancel();
      });
    }
  }

  void _onProtocolStatus() {
    if (!mounted) return;
    final unavailable = _runtime.protocolUnavailable.value;
    _setViewState(() {
      if (unavailable) {
        _protocolMessageActive = true;
        _message = context.l10n.ellaHomeTranscriptionUnconfirmed;
      } else if (_protocolMessageActive) {
        _protocolMessageActive = false;
        _message = null;
      }
    });
  }

  @override
  void dispose() {
    IrisCaptureControls.shared.unregister(this);
    _provider?.removeListener(_observeWords);
    _wordReceiptTimer?.cancel();
    _runtime.protocolUnavailable.removeListener(_onProtocolStatus);
    if (whisperDeliveryProtocol1) WhisperDeliveryRuntime.shared.removeListener(_strictChanged);
    _whisperFence.removeListener(_onSharedWhisperStateChanged);
    final pending = _pendingWhisperChoice;
    if (pending != null) _whisperFence.abandon(pending);
    unawaited(_whisperStateSubscription?.cancel());
    final navigator = _controlsNavigator;
    final route = _controlsRoute;
    if (navigator != null && route != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (navigator.mounted && route.isActive) navigator.removeRoute(route);
      });
    }
    _controlsAuthorityTimer?.cancel();
    _presentationRevision.dispose();
    _capsuleFocusNode.dispose();
    _connectFocusNode.dispose();
    _transcriptFocusNode.dispose();
    super.dispose();
  }

  Future<GuardianModeInfo?> _readWhisperState([ExactAccountAuthorityVerifier? authority]) async {
    final loader = widget.guardianModeLoader;
    if (loader != null) return loader();
    final result = await guardian_api.getGuardianMode(exactAuthority: authority);
    return result.isSuccess ? result.value : null;
  }

  Future<bool> _writeWhisperState(GuardianModeState state, ExactAccountAuthorityVerifier authority) async {
    final setter = widget.guardianModeSetter;
    if (setter != null) return setter(state);
    return (await guardian_api.setGuardianModeTwoTier(state, exactAuthority: authority)).isSuccess;
  }

  Future<void> _startWhisperNative({bool explicitOn = false}) =>
      widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start(explicitOn: explicitOn);

  Future<void> _stopWhisperNative() =>
      widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop();

  guardian_native.GuardianModeState _nativeWhisperState() =>
      widget.guardianNativeState?.call() ?? guardian_native.GuardianModeService().currentState;

  _WhisperPlaybackState _playbackStateFor(bool enabled) {
    if (!enabled) {
      return _nativeWhisperState() == guardian_native.GuardianModeState.idle
          ? _WhisperPlaybackState.unavailable
          : _WhisperPlaybackState.error;
    }
    return switch (_nativeWhisperState()) {
      guardian_native.GuardianModeState.active => _WhisperPlaybackState.ready,
      guardian_native.GuardianModeState.error => _WhisperPlaybackState.error,
      guardian_native.GuardianModeState.idle => _WhisperPlaybackState.unavailable,
    };
  }

  Future<void> _loadWhispersState() async {
    if (!_guardianAvailable) return;
    final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
    if (operation == null) {
      _showWhisperAuthorityUnavailable();
      return;
    }
    try {
      final info = await _readWhisperState(operation.authority);
      if (!_isWhisperOperationCurrent(operation)) return;
      if (info == null) {
        _setViewState(() {
          _whispersVerified = false;
          _whisperError = context.l10n.todayWhispersUnavailable;
        });
        return;
      }
      final enabled = !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);
      _whisperFence.publish(operation, (
        enabled: enabled,
        modeVerified: true,
        nativeReconciled: enabled
            ? _playbackStateFor(enabled) == _WhisperPlaybackState.ready
            : _nativeWhisperState() == guardian_native.GuardianModeState.idle,
      ));
      _setViewState(() {
        _whispersVerified = true;
        _whispersOn = enabled;
        _whisperPlayback = _playbackStateFor(enabled);
        _whisperError = null;
      });
    } catch (_) {
      if (!_isWhisperOperationCurrent(operation)) return;
      _setViewState(() {
        _whispersVerified = false;
        _whisperError = context.l10n.todayWhispersUnavailable;
      });
    }
  }

  bool _isWhisperOperationCurrent(guardian_native.GuardianWhisperOperation operation) =>
      mounted && _guardianAvailable && operation.isCurrent;

  void _showWhisperAuthorityUnavailable() {
    if (!mounted || !_guardianAvailable || _whisperFence.choicePending) return;
    _setViewState(() {
      _whispersVerified = false;
      _whisperError = null;
    });
  }

  void _onSharedWhisperStateChanged() {
    if (!mounted) return;
    final refreshRequired = _whisperRefreshRevision != _whisperFence.refreshRevision;
    _whisperRefreshRevision = _whisperFence.refreshRevision;
    final superseded = _whisperSharedRevision != _whisperFence.revision;
    _whisperSharedRevision = _whisperFence.revision;
    final snapshot = _whisperFence.snapshot;
    _setViewState(() {
      _whispersOn = snapshot?.enabled ?? false;
      _whispersVerified = snapshot?.modeVerified ?? false;
      _whispersBusy = _whisperFence.choicePending;
      _whisperPlayback = snapshot == null || !snapshot.modeVerified
          ? _WhisperPlaybackState.unavailable
          : !snapshot.nativeReconciled
              ? _WhisperPlaybackState.error
              : _playbackStateFor(snapshot.enabled);
      if (superseded) _whisperError = null;
    });
    if (refreshRequired && _guardianAvailable) unawaited(_loadWhispersState());
  }

  Future<void> _setWhispers(bool enabled) async {
    if (_whispersBusy || !_whispersVerified) return;
    final previousEnabled = _whispersOn;
    final operation = _whisperFence.choose(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active, enabled);
    if (operation == null) {
      _showWhisperAuthorityUnavailable();
      return;
    }
    if (_isWhisperOperationCurrent(operation)) _actionFeedback();
    _pendingWhisperChoice = operation;
    try {
      final saveFailedMessage = context.l10n.upstreamCaptureWhispersSaveFailed;
      final playbackFailedMessage = context.l10n.upstreamCaptureWhispersPlaybackFailed;
      final playbackStopFailedMessage = context.l10n.upstreamCaptureWhispersPlaybackStopFailed;
      _setViewState(() {
        _whispersBusy = true;
        _whisperError = null;
      });

      var nativeStopFailed = false;
      var saved = false;
      var resolvedEnabled = enabled;
      var modeVerified = false;
      var playback = _WhisperPlaybackState.unavailable;
      String? error;
      await _whisperFence.serialize<void>(operation, () async {
        if (!_isWhisperOperationCurrent(operation)) return;
        if (!enabled) {
          try {
            await _stopWhisperNative();
          } catch (_) {
            nativeStopFailed = true;
          }
        }
        if (!_isWhisperOperationCurrent(operation)) return;
        try {
          saved = await _writeWhisperState(
            enabled ? const GuardianModeState(features: ['MEMORY_SUPPORT']) : const GuardianModeState(),
            operation.authority,
          );
        } catch (_) {}
        if (!_isWhisperOperationCurrent(operation)) return;
        if (!saved) {
          error = saveFailedMessage;
          // A lost response does not establish whether the server committed.
          // Reconcile only current readback, never a speculative prior ON.
          GuardianModeInfo? authoritative;
          try {
            authoritative = await _readWhisperState(operation.authority);
          } catch (_) {}
          if (!_isWhisperOperationCurrent(operation)) return;
          modeVerified = authoritative != null;
          resolvedEnabled = authoritative == null
              ? previousEnabled
              : !(authoritative.twoTierState?.isOff ?? authoritative.currentMode == GuardianModeKey.off);
          try {
            if (modeVerified && resolvedEnabled) {
              await _startWhisperNative(explicitOn: enabled);
              playback = _WhisperPlaybackState.ready;
            } else {
              await _stopWhisperNative();
            }
          } catch (_) {
            playback = _WhisperPlaybackState.error;
          }
        } else if (enabled) {
          try {
            await _startWhisperNative(explicitOn: enabled);
            playback = _WhisperPlaybackState.ready;
          } catch (_) {
            playback = _WhisperPlaybackState.error;
            error = playbackFailedMessage;
          }
        } else if (nativeStopFailed) {
          playback = _WhisperPlaybackState.error;
          error = playbackStopFailedMessage;
        }
        if (saved) modeVerified = true;
      });
      if (!_isWhisperOperationCurrent(operation)) return;
      _whisperFence.publish(operation, (
        enabled: resolvedEnabled,
        modeVerified: modeVerified,
        nativeReconciled: modeVerified && playback != _WhisperPlaybackState.error,
      ));
      _setViewState(() {
        _whispersBusy = false;
        _whispersOn = resolvedEnabled;
        _whispersVerified = modeVerified;
        _whisperPlayback = playback;
        _whisperError = error;
      });
    } finally {
      if (identical(_pendingWhisperChoice, operation)) _pendingWhisperChoice = null;
      _whisperFence.abandon(operation);
    }
  }

  bool _strictOutputBusy = false;
  void _strictChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _stopStrictOutput() async {
    if (_strictOutputBusy) return;
    final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
    if (operation == null) return;
    setState(() => _strictOutputBusy = true);
    try {
      final runtime = WhisperDeliveryRuntime.shared;
      if (runtime.client.stopPending) {
        await runtime.retryStop();
      } else {
        await runtime.stopOutput();
      }
    } catch (_) {
      if (_isWhisperOperationCurrent(operation))
        setState(() => _whisperError = context.l10n.irisWhisperOutputUnconfirmed);
    } finally {
      if (mounted) setState(() => _strictOutputBusy = false);
    }
  }

  Future<void> _textOnlyStrictOutput() async {
    if (_strictOutputBusy) return;
    final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
    if (operation == null) return;
    setState(() => _strictOutputBusy = true);
    try {
      final runtime = WhisperDeliveryRuntime.shared;
      await runtime.setPresentation(
        runtime.preferredPresentation == WhisperPresentation.audio
            ? WhisperPresentation.textOnly
            : WhisperPresentation.audio,
      );
    } catch (_) {
      if (_isWhisperOperationCurrent(operation))
        setState(() => _whisperError = context.l10n.irisWhisperOutputUnconfirmed);
    } finally {
      if (mounted) setState(() => _strictOutputBusy = false);
    }
  }

  Future<void> _retryWhispers() async {
    if (_whispersBusy) return;
    if (!_whispersVerified) {
      final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
      if (operation == null) {
        _showWhisperAuthorityUnavailable();
        return;
      }
      if (_isWhisperOperationCurrent(operation)) _actionFeedback();
      _setViewState(() => _whispersBusy = true);
      await _loadWhispersState();
      if (_isWhisperOperationCurrent(operation)) _setViewState(() => _whispersBusy = false);
      return;
    }
    final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
    if (operation == null) {
      _showWhisperAuthorityUnavailable();
      return;
    }
    if (_isWhisperOperationCurrent(operation)) _actionFeedback();
    final enabled = _whispersOn;
    final playbackFailedMessage = context.l10n.upstreamCaptureWhispersPlaybackFailed;
    final playbackStopFailedMessage = context.l10n.upstreamCaptureWhispersPlaybackStopFailed;
    _setViewState(() {
      _whispersBusy = true;
      _whisperError = null;
    });
    try {
      await _whisperFence.serialize<void>(operation, () async {
        if (!_isWhisperOperationCurrent(operation)) return;
        if (enabled) {
          await _startWhisperNative(explicitOn: enabled);
        } else {
          await _stopWhisperNative();
        }
      });
      if (!_isWhisperOperationCurrent(operation)) return;
      _whisperFence.publish(operation, (enabled: enabled, modeVerified: true, nativeReconciled: true));
      _setViewState(() {
        _whisperPlayback = enabled ? _WhisperPlaybackState.ready : _WhisperPlaybackState.unavailable;
      });
    } catch (_) {
      if (!_isWhisperOperationCurrent(operation)) return;
      _setViewState(() {
        _whisperPlayback = _WhisperPlaybackState.error;
        _whisperError = enabled ? playbackFailedMessage : playbackStopFailedMessage;
      });
    } finally {
      if (_isWhisperOperationCurrent(operation)) _setViewState(() => _whispersBusy = false);
    }
  }

  Future<void> _run(
    _DockOperation operation,
    Future<void> Function() action, {
    required String failureMessage,
    bool interruptResume = false,
  }) async {
    if (_busy && !(interruptResume && _operation == _DockOperation.resuming)) return;
    if (interruptResume) _runtime.cancelPendingCaptureChange();
    final operationRevision = ++_operationRevision;
    final originUid = _uid;
    final originEpoch = _runtime.authority.bindingEpoch;
    _actionFeedback();
    _setViewState(() {
      _operation = operation;
      _message = null;
      _capturePauseUnconfirmed = false;
    });
    try {
      await action();
    } catch (_) {
      if (mounted &&
          operationRevision == _operationRevision &&
          _uid == originUid &&
          _runtime.authority.bindingEpoch == originEpoch) {
        _setViewState(() {
          _message = failureMessage;
          _capturePauseUnconfirmed = operation == _DockOperation.pausing || operation == _DockOperation.resuming;
        });
      }
    } finally {
      if (mounted && operationRevision == _operationRevision) _setViewState(() => _operation = _DockOperation.idle);
    }
  }

  Future<void> _handleStartOutcome(
    EllaCaptureStartOutcome outcome, {
    required bool necklace,
    required String originUid,
  }) async {
    if (originUid.isEmpty || _uid != originUid) return;
    switch (outcome) {
      case EllaCaptureStartOutcome.started:
        if (mounted) {
          _setViewState(() {
            if (!_runtime.protocolUnavailable.value) _message = null;
            _necklaceRetryAvailable = false;
          });
        }
        return;
      case EllaCaptureStartOutcome.unavailable:
        if (mounted) {
          _setViewState(() {
            _message =
                necklace ? context.l10n.upstreamCaptureConnectionFailed : context.l10n.upstreamCapturePhoneStartFailed;
            _necklaceRetryAvailable = necklace;
          });
        }
        return;
      case EllaCaptureStartOutcome.consentRequired:
        if (!mounted) return;
        _setViewState(() => _message = context.l10n.upstreamCaptureConsentRequired);
        final accepted = await (widget.consentRequester?.call(context) ?? AiConsentCoordinator.ensure(context));
        if (!mounted) return;
        _setViewState(() {
          _message =
              accepted ? context.l10n.upstreamCapturePermissionUpdated : context.l10n.upstreamCaptureConsentRequired;
        });
        return;
    }
  }

  Future<void> _startPhone() => _run(_DockOperation.connecting, () async {
        final originUid = _uid;
        await _handleStartOutcome(await _runtime.startPhoneCapture(originUid), necklace: false, originUid: originUid);
      }, failureMessage: context.l10n.upstreamCapturePhoneStartFailed);

  Future<void> _connectNecklace() async {
    if (_busy) return;
    final originUid = _uid;
    if (originUid.isEmpty) return;
    _actionFeedback();
    _setViewState(() {
      _operation = _DockOperation.searching;
      _message = null;
      _necklaceRetryAvailable = false;
    });
    try {
      final route = MaterialPageRoute<void>(
        builder: (_) => ConnectDevicePage(
          originUid: originUid,
          authenticatedUid: () => _uid,
          consentRequester: widget.consentRequester,
        ),
      );
      await Navigator.of(context).push<void>(route);
      await route.completed;
    } catch (_) {
      if (!mounted) return;
      _setViewState(() {
        _message = context.l10n.upstreamCaptureSearchFailed;
        _necklaceRetryAvailable = true;
      });
    } finally {
      if (mounted) {
        _setViewState(() => _operation = _DockOperation.idle);
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && _uid == originUid) _connectFocusNode.requestFocus();
        });
      }
    }
  }

  Future<void> _openTranscript(CaptureProvider provider, EllaCaptureSessionView session) async {
    bool isCurrent() => mounted && _uid == session.uid && _runtime.isCurrentCaptureSession(session);
    if (!isCurrent() || _transcriptOpen) return;
    _transcriptOpen = true;
    final originUid = _uid;
    final originAuthority = (widget.guardianAuthorityProvider ?? WalOwnerAuthority.active)();
    var closing = false;
    _actionFeedback();
    try {
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        backgroundColor: EllaPalette.of(context).paper,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(EllaSizes.cardRadius)),
        ),
        builder: (sheetContext) => IrisWhisperSheet(
          child: EllaCaptureTranscriptSheet(
            provider: provider,
            presentationChanges: Listenable.merge([_runtime.lastRevocation, _runtime.finishPending]),
            isCurrent: isCurrent,
            sourceLabel: session.source != 'phone'
                ? sheetContext.l10n.todayDockTranscriptNecklace
                : sheetContext.l10n.todayDockTranscriptPhone,
            onClose: () {
              if (closing || !sheetContext.mounted || ModalRoute.of(sheetContext)?.isCurrent != true) return false;
              closing = true;
              if (mounted && _uid == originUid && originAuthority?.isExactCurrent() == true) _actionFeedback();
              return true;
            },
          ),
        ),
      );
    } finally {
      _transcriptOpen = false;
    }
    if (mounted) _transcriptFocusNode.requestFocus();
  }

  String? _operationLabel(BuildContext context) => switch (_operation) {
        _DockOperation.idle => null,
        _DockOperation.booting => context.l10n.upstreamCapturePreparing,
        _DockOperation.searching => context.l10n.upstreamCaptureSearching,
        _DockOperation.connecting => context.l10n.upstreamCaptureConnecting,
        _DockOperation.stopping => context.l10n.upstreamCaptureStopping,
        _DockOperation.pausing => context.l10n.irisPausingCapture,
        _DockOperation.resuming => context.l10n.irisResumingCapture,
        _DockOperation.disconnecting => context.l10n.upstreamCaptureDisconnecting,
        _DockOperation.finishing => _runtime.isFinishingFor(_uid) ? context.l10n.upstreamCaptureFinishing : null,
      };

  Widget _actionLabel(BuildContext context, String label, {required String compactLabel}) => Text(
        MediaQuery.textScalerOf(context).scale(1) >= 2 ? compactLabel : label,
        semanticsLabel: label,
        textAlign: TextAlign.center,
      );

  Widget _secondaryAction(
    BuildContext context, {
    required Key key,
    required FocusNode focusNode,
    required VoidCallback? onPressed,
    required IconData icon,
    required String label,
    required String compactLabel,
    bool compactPresentation = false,
  }) {
    final text = compactPresentation
        ? Text(compactLabel, semanticsLabel: label, textAlign: TextAlign.center)
        : _actionLabel(context, label, compactLabel: compactLabel);
    if (MediaQuery.textScalerOf(context).scale(1) >= 3) {
      return OutlinedButton(
        key: key,
        focusNode: focusNode,
        style: _DockButtonStyles.secondary(context),
        onPressed: onPressed,
        child: Column(mainAxisSize: MainAxisSize.min, children: [Icon(icon), const SizedBox(height: 4), text]),
      );
    }
    return OutlinedButton.icon(
      key: key,
      focusNode: focusNode,
      style: _DockButtonStyles.secondary(context),
      onPressed: onPressed,
      icon: Icon(icon),
      label: text,
    );
  }

  @override
  Widget build(BuildContext context) => _buildPresentation(context, expanded: widget.expandControlsForTesting);

  Widget _statusDetail(String label, String value, {VoidCallback? onTap}) => ListTile(
        dense: true,
        minVerticalPadding: 12,
        title: Text(label, style: TextStyle(fontSize: onTap == null ? 15.5 : 16)),
        subtitle: Text(value, style: TextStyle(fontSize: onTap == null ? 15.5 : 16)),
        onTap: onTap,
      );

  Future<void> _openHardwareControls() async {
    if (!mounted || _uid.isEmpty) return;
    final uid = _uid, epoch = _runtime.authority.bindingEpoch;
    final authority = (widget.guardianAuthorityProvider ?? WalOwnerAuthority.active)();
    bool current() =>
        mounted && uid == _uid && epoch == _runtime.authority.bindingEpoch && authority?.isExactCurrent() == true;
    if (!current()) {
      await _openControls();
      return;
    }
    late final MaterialPageRoute<void> hardwareRoute;
    hardwareRoute = MaterialPageRoute<void>(
      builder: (hardwareContext) => Scaffold(
        appBar: AppBar(title: Text(context.l10n.todayNecklace)),
        body: QuietMemoryAuthorityFence(
          isCurrent: current,
          child: Column(
            children: [
              const IrisWhisperStopBar(),
              Expanded(
                child: SingleChildScrollView(
                  child: Column(children: [
                    Padding(
                        padding: const EdgeInsets.all(20),
                        child: SvgPicture.asset(
                            'assets/images/ella-v7/necklace-${_provider?.havingRecordingDevice == true ? 'connected' : 'not-connected'}.svg',
                            width: 160,
                            height: 160,
                            excludeFromSemantics: true)),
                    _buildPresentation(context, expanded: true, necklaceOnly: true, onConnectNecklace: () async {
                      if (!current() || _busy || !hardwareRoute.isCurrent) return;
                      // Explicit selection can mint a fresh capture lease. Retire
                      // this old authority-bound page before opening the picker;
                      // returning then reveals the current Home controls. Never
                      // promote an old page to a different account/session epoch.
                      Navigator.of(hardwareContext).pop();
                      await hardwareRoute.completed;
                      if (!current()) return;
                      await _connectNecklace();
                    }),
                  ]),
                ),
              ),
            ],
          ),
        ),
      ),
    );
    await Navigator.of(context).push(hardwareRoute);
  }

  Future<void> _openWhisperControls() async {
    if (!mounted || _uid.isEmpty) return;
    final authority = (widget.guardianAuthorityProvider ?? WalOwnerAuthority.active)();
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (sheetContext) => IrisWhisperSheet(
        child: AnimatedBuilder(
          animation: Listenable.merge([
            _presentationRevision,
            if (whisperDeliveryProtocol1) WhisperDeliveryRuntime.shared,
          ]),
          builder: (context, _) => SingleChildScrollView(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    context.l10n.todayWhispersTitle,
                    style: EllaTypography.of(context).noteBody.copyWith(fontSize: 28),
                  ),
                  if (authority?.isExactCurrent() != true)
                    Text(context.l10n.upstreamCaptureUnavailable)
                  else ...[
                    _WhispersRow(
                      verified: _whispersVerified,
                      enabled: _whispersOn,
                      busy: _whispersBusy,
                      playback: _whisperPlayback,
                      error: _whisperError,
                      onChanged: _setWhispers,
                      onRetry: _retryWhispers,
                    ),
                    if (whisperDeliveryProtocol1) ...[
                      if (_whispersVerified && _whispersOn)
                        Text(context.l10n.irisWhisperDeliveryHint, style: EllaTypography.of(context).secondary),
                      const IrisWhisperMessage(showStopControl: false),
                      if (WhisperDeliveryRuntime.shared.failed)
                        Text(context.l10n.irisWhisperOutputUnconfirmed, style: EllaTypography.of(context).body),
                      if (WhisperDeliveryRuntime.shared.client.stopPending)
                        TextButton(
                          onPressed: _strictOutputBusy ? null : _stopStrictOutput,
                          child: Text(context.l10n.tryAgain),
                        ),
                      TextButton.icon(
                        key: const Key('iris-whisper-text-only'),
                        style: _DockButtonStyles.text(context),
                        onPressed:
                            _strictOutputBusy || !_whispersVerified || !_whispersOn ? null : _textOnlyStrictOutput,
                        icon: const Icon(Icons.text_fields_rounded),
                        label: Text(
                          WhisperDeliveryRuntime.shared.preferredPresentation == WhisperPresentation.textOnly
                              ? context.l10n.irisWhisperAudio
                              : context.l10n.irisWhisperTextOnly,
                        ),
                      ),
                    ],
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _openControls() async {
    if (_controlsOpen || !mounted || _uid.isEmpty) return;
    final originUid = _uid;
    final readAuthority = widget.guardianAuthorityProvider ?? WalOwnerAuthority.active;
    final originAuthority = readAuthority();
    final originEpoch = _runtime.authority.bindingEpoch;
    final preferences = ella_preferences.SharedPreferencesUtil();
    final originConsentTerminal = preferences.terminalAccountConsentAuthorityGeneration;
    final originProvisioningTerminal = preferences.ellaProvisioningTerminalAuthorityGeneration;
    // Once observed retired, a sheet must never become eligible again even if
    // an old account or authority subsequently reappears. Reentry is explicit.
    var controlsRetired = false;
    bool sameContext() =>
        !controlsRetired && mounted && _uid == originUid && _runtime.authority.bindingEpoch == originEpoch &&
        preferences.terminalAccountConsentAuthorityGeneration == originConsentTerminal &&
        preferences.ellaProvisioningTerminalAuthorityGeneration == originProvisioningTerminal;
    bool consentReady() => AiConsentActiveSessionLease.authorityForSessionStart(
          preferences: preferences, expectedUid: originUid) != null;
    // A composite WAL authority can be absent while consent is valid and setup
    // verification is pending. That is not a request for a new consent decision.
    bool canReviewConsent() => originAuthority == null && sameContext() &&
        preferences.uid == originUid && !consentReady();
    bool isCurrent() => sameContext() && originAuthority?.uid == originUid &&
        originAuthority?.isExactCurrent() == true;
    _controlsOpen = true;
    // Display-only refresh. Never adopt a newly minted authority into an old
    // sheet's action closures: dismiss and require a fresh explicit entry.
    _controlsAuthorityTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!mounted) {
        _controlsAuthorityTimer?.cancel();
        return;
      }
      final latest = originAuthority == null && sameContext() ? readAuthority() : null;
      if (latest?.uid == originUid && latest?.isExactCurrent() == true) {
        final navigator = _controlsNavigator;
        final route = _controlsRoute;
        _controlsAuthorityTimer?.cancel();
        if (navigator != null && navigator.mounted && route != null && route.isActive) {
          navigator.removeRoute(route);
        }
        return;
      }
      if (!sameContext() || (originAuthority != null && !isCurrent())) {
        controlsRetired = true;
        _presentationRevision.value++;
        _controlsAuthorityTimer?.cancel();
      } else if (originAuthority == null || (_controlsAuthorityTimer?.tick ?? 0) % 4 == 0) {
        _presentationRevision.value++;
      }
    });
    try {
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Colors.transparent,
        showDragHandle: true,
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(38))),
        builder: (sheetContext) {
          _controlsNavigator = Navigator.of(sheetContext);
          _controlsRoute = ModalRoute.of(sheetContext);
          return AnimatedBuilder(
            animation: Listenable.merge([
              _presentationRevision,
              _runtime.lastRevocation,
              if (_provider != null) _provider!,
            ]),
            builder: (context, _) => SafeArea(
              child: ConstrainedBox(
                constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .85),
                child: EllaV7Glass(
                    radius: 38,
                    child: SingleChildScrollView(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        key: const Key('upstream-capture-controls-sheet'),
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          const IrisWhisperStopBar(),
                          Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                            if (_canShowNecklaceConnect)
                              SizedBox.square(
                                  dimension: 150,
                                  child: SvgPicture.asset('assets/images/ella-v7/necklace-not-connected.svg',
                                      width: 150, height: 150, fit: BoxFit.contain, excludeFromSemantics: true)),
                            EllaV7Lume(
                                pose: _canShowNecklaceConnect
                                    ? 'needs'
                                    : IrisCaptureControls.shared.listening == IrisListeningState.active
                                        ? 'listen'
                                        : IrisCaptureControls.shared.listening == IrisListeningState.paused
                                            ? 'paused'
                                            : 'idle',
                                size: 86),
                          ]),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                  context.l10n.irisHearingTitle,
                                  style: EllaTypography.of(
                                    context,
                                  ).noteBody.copyWith(fontSize: 28, fontWeight: FontWeight.w400),
                                ),
                              ),
                              IconButton(
                                key: const Key('upstream-capture-controls-close'),
                                tooltip: context.l10n.close,
                                onPressed: () => Navigator.of(sheetContext).pop(),
                                icon: const Icon(Icons.close),
                              ),
                            ],
                          ),
                          if (isCurrent()) ...[
                            Text(
                              _operation == _DockOperation.pausing || _operation == _DockOperation.resuming
                                  ? _operationLabel(context)!
                                  : _capturePauseUnconfirmed
                                      ? context.l10n.irisCapturePauseFailed
                                      : _runtime.captureSession != null &&
                                              _runtime.isCapturePaused(_runtime.captureSession!)
                                          ? context.l10n.irisPausedAnswer
                                          : _canShowNecklaceConnect
                                              ? context.l10n.irisNecklaceDisconnectedAnswer
                                              : _currentWordSession() == null
                                                  ? context.l10n.irisCaptureOff
                                                  : _runtime.captureSession?.source == 'phone'
                                                      ? context.l10n.irisPhoneFallbackAnswer
                                                      : context.l10n.irisUnverifiedCaptureAnswer,
                              style:
                                  EllaTypography.of(context).body.copyWith(fontSize: 19, fontWeight: FontWeight.w800),
                            ),
                            if (_canShowNecklaceConnect)
                              Padding(
                                  padding: const EdgeInsets.only(top: 8, bottom: 12),
                                  child: Text(context.l10n.irisNecklaceDisconnectedExplanation,
                                      style: EllaTypography.of(context).body.copyWith(fontSize: 16))),
                            if (_runtime.captureSession case final pausedSession?
                                when _runtime.isCapturePaused(pausedSession))
                              Text(context.l10n.irisPausedExplanation, style: EllaTypography.of(context).body),
                            if (_runtime.captureSession case final session?) ...[
                              if (session.source == 'phone' &&
                                  (_runtime.canPauseCapture(session) ||
                                      _runtime.canResumeCapture(session) ||
                                      _operation == _DockOperation.pausing ||
                                      _operation == _DockOperation.resuming))
                                OutlinedButton.icon(
                                  key: const Key('iris-status-pause-resume'),
                                  style: _DockButtonStyles.secondary(
                                    context,
                                  ).copyWith(minimumSize: const WidgetStatePropertyAll(Size(0, 56))),
                                  onPressed: _busy || _runtime.pauseChangePending
                                      ? null
                                      : () {
                                          final paused = _runtime.isCapturePaused(session);
                                          _run(
                                            paused ? _DockOperation.resuming : _DockOperation.pausing,
                                            () => _runtime.setCapturePaused(session,
                                                paused: !paused, isCurrent: isCurrent),
                                            failureMessage: this.context.l10n.irisCapturePauseFailed,
                                          );
                                        },
                                  icon: Icon(
                                    _runtime.isCapturePaused(session) ? Icons.play_arrow_rounded : Icons.pause_rounded,
                                  ),
                                  label: Text(
                                    _operationLabel(context) ??
                                        (_runtime.isCapturePaused(session)
                                            ? context.l10n.irisResumeListening
                                            : context.l10n.irisPauseListening),
                                  ),
                                ),
                            ],
                            if (_message != null && !_capturePauseUnconfirmed)
                              Text(_message!, style: EllaTypography.of(context).secondary),
                            if (_canShowNecklaceConnect)
                              FilledButton.icon(
                                key: const Key('iris-status-connect-necklace'),
                                style: FilledButton.styleFrom(
                                    minimumSize: const Size(double.infinity, 52),
                                    backgroundColor: EllaPalette.of(context).ink,
                                    foregroundColor:
                                        EllaPalette.of(context).dark ? EllaPalette.of(context).paper : Colors.white),
                                onPressed: _busy
                                    ? null
                                    : () async {
                                        if (!isCurrent()) return;
                                        Navigator.of(sheetContext).pop();
                                        await _connectNecklace();
                                      },
                                icon: const Icon(Icons.bluetooth_rounded),
                                label: Text(context.l10n.irisNecklaceSettings),
                              ),
                            // During a pending Resume the disabled primary action is
                            // replaced by the existing Stop escape, preserving recovery.
                            if (_operation == _DockOperation.resuming)
                              TextButton.icon(
                                key: const Key('iris-status-stop-capture'),
                                style: _DockButtonStyles.text(context),
                                onPressed: _busy && _operation != _DockOperation.resuming
                                    ? null
                                    : () => _run(
                                          _DockOperation.stopping,
                                          _runtime.stopCaptureEscape,
                                          interruptResume: true,
                                          failureMessage: context.l10n.upstreamCaptureUnavailable,
                                        ),
                                icon: const Icon(Icons.stop_circle_outlined),
                                label: Text(context.l10n.irisCaptureStop),
                              ),
                            ExpansionTile(
                              key: const Key('iris-status-details'),
                              textColor: EllaPalette.of(context).ink,
                              collapsedTextColor: EllaPalette.of(context).ink,
                              iconColor: EllaPalette.of(context).inkSoft,
                              collapsedIconColor: EllaPalette.of(context).inkSoft,
                              title: Text(context.l10n.irisStatusDetails),
                              children: [
                                _statusDetail(
                                  context.l10n.todayNecklace,
                                  _provider?.havingRecordingDevice == true
                                      ? context.l10n.irisNecklaceConnected
                                      : _canShowNecklaceConnect
                                          ? context.l10n.irisNecklaceNotConnected
                                          : context.l10n.irisNotShownYet,
                                ),
                                _statusDetail(context.l10n.irisSound, context.l10n.irisNotShownYet),
                                _statusDetail(
                                  context.l10n.irisWords,
                                  _provider?.segments.isNotEmpty == true
                                      ? context.l10n.irisNewestWords
                                      : context.l10n.irisNotShownYet,
                                  onTap: _provider != null && _runtime.captureSession != null
                                      ? () => _openTranscript(_provider!, _runtime.captureSession!)
                                      : null,
                                ),
                                _statusDetail(
                                  context.l10n.irisConnectionToElla,
                                  _provider?.transcriptServiceReady == true
                                      ? context.l10n.irisOn
                                      : context.l10n.irisNotShownYet,
                                ),
                                _statusDetail(context.l10n.irisWaitingToSend, context.l10n.irisNotShownYet),
                                TextButton(
                                  style: _DockButtonStyles.text(context),
                                  onPressed: () => Share.share(
                                    'Ella status report\nObserved: ${DateTime.now().toUtc().toIso8601String()}\n'
                                    'Capture: ${IrisCaptureControls.shared.listening.name}\n'
                                    'Sound heartbeat: unavailable\nWaiting to send: unavailable',
                                  ),
                                  child: Text(context.l10n.irisSendSupportReport),
                                ),
                                SwitchListTile(
                                  title: Text(context.l10n.irisShowNewestWords, style: EllaTypography.of(context).body),
                                  value: ella_preferences.SharedPreferencesUtil()
                                      .getBool('irisNewestWordsHome.$originUid'),
                                  onChanged: (value) async {
                                    if (!isCurrent()) return;
                                    await ella_preferences.SharedPreferencesUtil().saveBool(
                                      'irisNewestWordsHome.$originUid',
                                      value,
                                    );
                                    if (isCurrent()) _presentationRevision.value++;
                                  },
                                ),
                              ],
                            ),
                          ] else if (originAuthority == null && sameContext() &&
                              preferences.uid == originUid && consentReady()) ...[
                            Text(context.l10n.ellaSetupUnknownTitle,
                                key: const Key('upstream-capture-setup-unverified')),
                            Text(context.l10n.upstreamCaptureUnavailable),
                          ] else if (canReviewConsent()) ...[
                            Text(context.l10n.upstreamCaptureConsentRequired),
                            TextButton(
                              key: const Key('upstream-capture-review-consent'),
                              style: _DockButtonStyles.text(context),
                              onPressed: _busy
                                  ? null
                                  : () => _run(_DockOperation.connecting, () async {
                                        if (!canReviewConsent()) return;
                                        final accepted = await (widget.consentRequester?.call(this.context) ??
                                            AiConsentCoordinator.review(this.context, canReview: canReviewConsent));
                                        if (!canReviewConsent()) return;
                                        _setViewState(
                                          () => _message = accepted
                                              ? this.context.l10n.upstreamCapturePermissionUpdated
                                              : this.context.l10n.upstreamCaptureConsentRequired,
                                        );
                                        if (accepted && sheetContext.mounted) Navigator.of(sheetContext).pop();
                                      }, failureMessage: context.l10n.upstreamCaptureConsentRequired),
                              child: Text(context.l10n.aiConsentReviewAction),
                            ),
                          ] else
                            Text(context.l10n.upstreamCaptureUnavailable),
                        ],
                      ),
                    )),
              ),
            ),
          );
        },
      );
    } finally {
      _controlsOpen = false;
      _controlsAuthorityTimer?.cancel();
      _controlsAuthorityTimer = null;
      _controlsNavigator = null;
      _controlsRoute = null;
    }
    if (mounted && _uid == originUid) _capsuleFocusNode.requestFocus();
  }

  Widget _capsule(
    BuildContext context, {
    required String label,
    required bool live,
    required bool error,
    bool paused = false,
    String? errorDetail,
    VoidCallback? stop,
  }) {
    final palette = EllaPalette.of(context);
    if (widget.headerControl) {
      return Center(
          child: EllaV7Glass(
              radius: 24,
              child: IconButton(
                key: const Key('upstream-capture-open-controls'),
                focusNode: _capsuleFocusNode,
                tooltip:
                    '${context.l10n.irisStatusDetails}: ${_canShowNecklaceConnect ? context.l10n.irisNecklaceNotConnected : label}',
                onPressed: _openControls,
                color: palette.ink,
                constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                icon: const Icon(Icons.settings_outlined),
              )));
    }
    final statusButton = Tooltip(
      message: context.l10n.upstreamCaptureControlsTitle,
      child: TextButton(
        key: const Key('upstream-capture-open-controls'),
        focusNode: _capsuleFocusNode,
        onPressed: _openControls,
        style: TextButton.styleFrom(
          foregroundColor: palette.ink,
          minimumSize: const Size(48, 48),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        ),
        child: Row(
          children: [
            if (error)
              const Icon(Icons.info_outline, size: 21)
            else
              Container(
                key: const Key('iris-capture-status-dot'),
                width: paused ? 9 : 10,
                height: paused ? 9 : 10,
                decoration: BoxDecoration(
                  color: palette.textSecondary,
                  borderRadius: BorderRadius.circular(paused ? 2 : 5),
                ),
              ),
            const SizedBox(width: 9),
            Expanded(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(label, style: EllaTypography.of(context).body.copyWith(fontWeight: FontWeight.w600)),
                  if (errorDetail != null) Text(errorDetail, style: EllaTypography.of(context).secondary),
                ],
              ),
            ),
            const Icon(Icons.expand_more, size: 20),
          ],
        ),
      ),
    );
    final stopButton = stop == null
        ? null
        : _DockPrimaryAction(
            buttonKey: const Key('upstream-capture-capsule-stop'),
            captureAdmission: _capturePressAdmission,
            onPressed: stop,
            icon: Icons.stop_circle_outlined,
            label: Text(context.l10n.irisCaptureStop, textAlign: TextAlign.center),
          );
    final connectButton = stop == null && !live && _canShowNecklaceConnect
        ? TextButton.icon(
            key: const Key('iris-home-connect-necklace'),
            style: _DockButtonStyles.text(context),
            onPressed: _busy ? null : _connectNecklace,
            icon: const Icon(Icons.bluetooth_rounded),
            label: Text(context.l10n.connect),
          )
        : null;
    return _DockSurface(
      capsule: true,
      child: LayoutBuilder(
        key: const Key('upstream-capture-dock'),
        builder: (context, constraints) {
          if (constraints.hasBoundedHeight) {
            if (connectButton != null) {
              // Idle recovery can scroll as one surface. Reserving a fixed
              // Connect row here clips the chosen-size status glyphs when a
              // keyboard leaves only a short capsule viewport.
              return SingleChildScrollView(
                primary: false,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    statusButton,
                    Tooltip(message: context.l10n.upstreamCaptureConnectNecklace, child: connectButton),
                  ],
                ),
              );
            }
            // A keyboard may leave less height than the full status text.
            // Scroll that text at the selected size and keep Stop outside it.
            return Row(
              children: [
                Expanded(child: SingleChildScrollView(primary: false, child: statusButton)),
                if (stopButton != null)
                  Expanded(
                    child: Tooltip(
                      message: context.l10n.irisCaptureStop,
                      child: _DockPrimaryAction(
                        buttonKey: const Key('upstream-capture-capsule-stop'),
                        captureAdmission: _capturePressAdmission,
                        onPressed: stop,
                        icon: Icons.stop_circle_outlined,
                        label: Text(
                          context.l10n.todayDockStop,
                          semanticsLabel: context.l10n.irisCaptureStop,
                          textAlign: TextAlign.center,
                        ),
                      ),
                    ),
                  ),
              ],
            );
          }
          if (stopButton != null && (constraints.maxWidth < 360 || MediaQuery.textScalerOf(context).scale(1) >= 1.5)) {
            return Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [statusButton, const SizedBox(height: 8), stopButton],
            );
          }
          if (connectButton != null) {
            return Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [statusButton, connectButton],
            );
          }
          return Row(
            children: [
              Expanded(child: statusButton),
              if (stopButton != null) stopButton,
            ],
          );
        },
      ),
    );
  }

  Widget _buildPresentation(BuildContext context,
      {required bool expanded, bool necklaceOnly = false, Future<void> Function()? onConnectNecklace}) {
    final provider = _provider;
    if (provider == null) {
      final bootFailed = _operation == _DockOperation.idle;
      if (!expanded) {
        return _capsule(
          context,
          label: _operationLabel(context) ?? _message ?? context.l10n.upstreamCaptureUnavailable,
          live: false,
          error: bootFailed,
        );
      }
      return _DockSurface(
        child: Column(
          key: const Key('upstream-capture-dock'),
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _DockStatus(
              label: _operationLabel(context) ?? _message ?? context.l10n.upstreamCaptureUnavailable,
              active: !bootFailed,
              error: bootFailed,
            ),
            if (bootFailed) ...[
              const SizedBox(height: 12),
              _DockPrimaryAction(
                buttonKey: const Key('upstream-capture-retry-boot'),
                captureAdmission: _capturePressAdmission,
                onPressed: () {
                  if (_busy) return;
                  _actionFeedback();
                  unawaited(_boot());
                },
                icon: Icons.refresh_rounded,
                label: Text(context.l10n.retry),
              ),
            ],
          ],
        ),
      );
    }
    return AnimatedBuilder(
      animation: Listenable.merge([provider, _runtime.lastRevocation, _runtime.finishPending, _presentationRevision]),
      builder: (context, _) {
        final candidate = _runtime.captureSession;
        final session = candidate?.uid == _uid ? candidate : null;
        final state = provider.recordingState;
        final paused = session != null && _runtime.isCapturePaused(session);
        final phoneActive = state == RecordingState.record ||
            state == RecordingState.interrupted ||
            (paused && session.source == 'phone') ||
            provider.isPhoneMicBatchRecording;
        final necklaceBound = state == RecordingState.deviceRecord || provider.havingRecordingDevice;
        final phoneLive = session != null &&
            ((state == RecordingState.record && provider.transcriptServiceReady) || provider.isPhoneMicBatchRecording);
        final necklaceLive = session != null && state == RecordingState.deviceRecord && provider.transcriptServiceReady;
        final live = session != null && (phoneLive || necklaceLive);
        final transcriptAvailable = session != null && (live || provider.segments.isNotEmpty);
        final finishing = _runtime.isFinishingFor(_uid);
        final canFinish = session != null && !_busy && _runtime.canFinishSession(session);
        final connectionAction = necklaceBound
                    ? _secondaryAction(
                        context,
                        key: const Key('upstream-capture-disconnect-necklace'),
                        focusNode: _connectFocusNode,
                        onPressed: _busy
                            ? null
                            : () => _run(
                                  _DockOperation.disconnecting,
                                  _runtime.disconnectNecklace,
                                  failureMessage: context.l10n.upstreamCaptureConnectionFailed,
                                ),
                        icon: Icons.bluetooth_disabled_rounded,
                        label: context.l10n.upstreamCaptureDisconnectNecklace,
                        compactLabel: context.l10n.disconnect,
                        compactPresentation: true,
                      )
                    : _secondaryAction(
                        context,
                        key: const Key('upstream-capture-connect-necklace'),
                        focusNode: _connectFocusNode,
                        onPressed: _busy ? null : (onConnectNecklace ?? _connectNecklace),
                        icon: _necklaceRetryAvailable ? Icons.refresh_rounded : Icons.bluetooth_rounded,
                        label:
                            _necklaceRetryAvailable ? context.l10n.retry : context.l10n.upstreamCaptureConnectNecklace,
                        compactLabel: _necklaceRetryAvailable ? context.l10n.retry : context.l10n.connect,
                        compactPresentation: true,
                      );
        final transcriptAction = session == null ? const SizedBox.shrink() : _secondaryAction(
                    context,
                    key: const Key('upstream-capture-view-transcript'),
                    focusNode: _transcriptFocusNode,
                    onPressed: () => _openTranscript(provider, session),
                    icon: Icons.subject_rounded,
                    label: session.source != 'phone'
                        ? context.l10n.todayDockTranscriptNecklace
                        : context.l10n.todayDockTranscriptPhone,
                    compactLabel: context.l10n.transcript,
                  );
        final initializing = state == RecordingState.initialising;
        final operationLabel = finishing ? context.l10n.upstreamCaptureFinishing : _operationLabel(context);
        final status = operationLabel ??
            (_protocolMessageActive && _message != null ? _message : null) ??
            (phoneLive
                ? context.l10n.irisPhoneMicrophoneOn
                : necklaceLive
                    ? context.l10n.irisCaptureOn
                    : session != null && state == RecordingState.interrupted
                        ? context.l10n.transcriptionPausedReconnecting
                        : (session != null &&
                                    (state == RecordingState.record || state == RecordingState.deviceRecord)) ||
                                initializing
                            ? context.l10n.upstreamCaptureConnectingTranscription
                            : _message);
        final detail = operationLabel != null
            ? null
            : live && _message != null && !_protocolMessageActive
                ? _message
                : live && provider.segments.isEmpty && !_protocolMessageActive
                    ? context.l10n.upstreamCaptureWaitingForSpeech
                    : null;

        if (!expanded) {
          final uncertain = session == null && (phoneActive || state == RecordingState.deviceRecord);
          final capsule = _capsule(
            context,
            label: operationLabel ??
                (_protocolMessageActive ? _message : null) ??
                (paused
                    ? context.l10n.irisCapturePaused
                    : live
                        ? (necklaceLive ? context.l10n.irisCaptureOn : context.l10n.irisPhoneMicrophoneOn)
                        : status ??
                            (uncertain ? context.l10n.upstreamCaptureUnavailable : context.l10n.upstreamCaptureIdle)),
            live: false,
            paused: paused,
            error: uncertain || (_message != null && !_busy),
            errorDetail: live && !_busy && !_protocolMessageActive ? _message : null,
            stop: _operation == _DockOperation.resuming
                ? () => _run(
                      _DockOperation.stopping,
                      _runtime.stopCaptureEscape,
                      interruptResume: true,
                      failureMessage: context.l10n.upstreamCaptureUnavailable,
                    )
                : null,
          );
          final showWords = ella_preferences.SharedPreferencesUtil().getBool('irisNewestWordsHome.$_uid') &&
              session != null &&
              provider.segments.isNotEmpty;
          if (!showWords) return capsule;
          return LayoutBuilder(
            builder: (context, constraints) => SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  capsule,
                  TextButton(
                    onPressed: () => _openTranscript(provider, session),
                    child: Text(provider.segments.last.text, style: EllaTypography.of(context).body),
                  ),
                ],
              ),
            ),
          );
        }
        return _DockSurface(
          child: Column(
            key: const Key('upstream-capture-dock'),
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (status != null && status.isNotEmpty) ...[
                _DockStatus(
                  label: status,
                  visualLabel: MediaQuery.textScalerOf(context).scale(1) >= 3 &&
                          !_protocolMessageActive &&
                          operationLabel == null
                      ? (necklaceLive
                          ? context.l10n.todayNecklace
                          : phoneLive
                              ? context.l10n.phone
                              : null)
                      : null,
                  detail: detail,
                  active: false,
                  live: false,
                  error: !_busy && _message != null && (_protocolMessageActive || !live),
                ),
                const SizedBox(height: 12),
              ],
              if (necklaceOnly && session != null && session.source != 'phone') ...[
                LiveCaptureCard(
                  source: session.source,
                  status: CaptureSources.label(context, session.source),
                  compact: true,
                  paused: paused,
                  onPauseToggle: LiveCaptureCard.canPause(provider.recordingDevice, source: session.source)
                      ? () async {
                          try {
                            await _runtime.setCapturePaused(session,
                                paused: !provider.isPaused,
                                isCurrent: () => mounted && _uid == session.uid &&
                                    identical(provider, _runtime.provider));
                          } catch (_) {
                            if (mounted) _setViewState(() => _message = context.l10n.upstreamCaptureUnavailable);
                          }
                        }
                      : null,
                ),
                const SizedBox(height: 8),
              ],
              if (necklaceOnly) connectionAction else _AdaptiveActionPair(
                first: phoneActive
                    ? _DockPrimaryAction(
                        buttonKey: const Key('upstream-capture-stop-phone'),
                        captureAdmission: _capturePressAdmission,
                        onPressed: _busy
                            ? null
                            : () => _run(
                                  _DockOperation.stopping,
                                  _runtime.stopPhoneCapture,
                                  failureMessage: context.l10n.upstreamCaptureUnavailable,
                                ),
                        icon: Icons.stop_circle_outlined,
                        label: Text(
                          context.l10n.todayDockStop,
                          semanticsLabel: context.l10n.upstreamCaptureStop,
                          textAlign: TextAlign.center,
                        ),
                      )
                    : _DockPrimaryAction(
                        buttonKey: const Key('upstream-capture-record-phone'),
                        captureAdmission: _capturePressAdmission,
                        onPressed: _busy ? null : _startPhone,
                        icon: Icons.mic_none_rounded,
                        label: Text(
                          context.l10n.todayDockRecord,
                          semanticsLabel: context.l10n.upstreamCaptureRecordPhone,
                          textAlign: TextAlign.center,
                        ),
                      ),
                second: connectionAction,
              ),
              if (transcriptAvailable) ...[
                const SizedBox(height: 8),
                if (necklaceOnly) transcriptAction else _AdaptiveActionPair(
                  first: transcriptAction,
                  second: _DockPrimaryAction(
                    buttonKey: const Key('upstream-capture-finish'),
                    captureAdmission: _capturePressAdmission,
                    onPressed: !canFinish
                        ? null
                        : () => _run(
                              _DockOperation.finishing,
                              () => _runtime.finishCaptureSession(session),
                              failureMessage: context.l10n.upstreamCaptureUnavailable,
                            ),
                    icon: Icons.check_circle_outline_rounded,
                    label: Text(
                      context.l10n.todayDockFinish,
                      semanticsLabel: context.l10n.upstreamCaptureFinish,
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
              ],
              if (_guardianAvailable && widget.expandControlsForTesting) ...[
                const SizedBox(height: 10),
                Divider(height: 1, color: EllaPalette.of(context).cardDeep),
                const SizedBox(height: 8),
                _WhispersRow(
                  verified: _whispersVerified,
                  enabled: _whispersOn,
                  busy: _whispersBusy,
                  playback: _whisperPlayback,
                  error: _whisperError,
                  onChanged: _setWhispers,
                  onRetry: _retryWhispers,
                ),
                if (whisperDeliveryProtocol1) ...[
                  if (WhisperDeliveryRuntime.shared.message != null)
                    Text(WhisperDeliveryRuntime.shared.message!, style: EllaTypography.of(context).body),
                  if (WhisperDeliveryRuntime.shared.playing)
                    Text(context.l10n.irisWhisperPlaying, style: EllaTypography.of(context).secondary),
                  if (WhisperDeliveryRuntime.shared.failed)
                    Text(context.l10n.irisWhisperOutputUnconfirmed, style: EllaTypography.of(context).secondary),
                  Wrap(
                    spacing: 8,
                    children: [
                      TextButton.icon(
                        key: const Key('iris-whisper-output-stop'),
                        style: _DockButtonStyles.text(context),
                        onPressed: _strictOutputBusy ||
                                (!WhisperDeliveryRuntime.shared.client.ready &&
                                    !WhisperDeliveryRuntime.shared.client.stopPending)
                            ? null
                            : _stopStrictOutput,
                        icon: const Icon(Icons.stop_rounded),
                        label: Text(
                          WhisperDeliveryRuntime.shared.client.stopPending
                              ? context.l10n.tryAgain
                              : context.l10n.irisWhisperStop,
                        ),
                      ),
                      TextButton.icon(
                        key: const Key('iris-whisper-text-only'),
                        style: _DockButtonStyles.text(context),
                        onPressed: _strictOutputBusy || !_whispersOn ? null : _textOnlyStrictOutput,
                        icon: Icon(
                          WhisperDeliveryRuntime.shared.preferredPresentation == WhisperPresentation.textOnly
                              ? Icons.volume_up_outlined
                              : Icons.text_fields_rounded,
                        ),
                        label: Text(
                          WhisperDeliveryRuntime.shared.preferredPresentation == WhisperPresentation.textOnly
                              ? context.l10n.irisWhisperAudio
                              : context.l10n.irisWhisperTextOnly,
                        ),
                      ),
                    ],
                  ),
                ],
              ],
            ],
          ),
        );
      },
    );
  }
}

class _DockStatus extends StatelessWidget {
  const _DockStatus({
    required this.label,
    required this.active,
    required this.error,
    this.detail,
    this.visualLabel,
    this.live = false,
  });

  final String label;
  final String? visualLabel;
  final String? detail;
  final bool active;
  final bool live;
  final bool error;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      liveRegion: true,
      excludeSemantics: true,
      label: detail == null ? label : '$label. $detail',
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 7),
            child: error
                ? Icon(Icons.error_outline_rounded, size: 18, color: EllaPalette.of(context).error)
                : EllaBreathingDot(
                    key: const Key('upstream-capture-status-indicator'),
                    active: active,
                    live: live,
                    size: 12,
                  ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  visualLabel ?? label,
                  key: const Key('upstream-capture-status'),
                  semanticsLabel: label,
                  style: EllaTypography.of(context).body.copyWith(
                        color: error ? EllaPalette.of(context).error : EllaPalette.of(context).ink,
                        fontWeight: FontWeight.w700,
                      ),
                ),
                if (detail != null) ...[
                  const SizedBox(height: 2),
                  Text(
                    detail!,
                    style: EllaTypography.of(context).caption.copyWith(color: EllaPalette.of(context).inkSoft),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _AdaptiveActionPair extends StatelessWidget {
  const _AdaptiveActionPair({required this.first, required this.second});

  final Widget first;
  final Widget second;

  @override
  Widget build(BuildContext context) {
    final textScale = MediaQuery.textScalerOf(context).scale(1);
    return LayoutBuilder(
      builder: (context, constraints) {
        if (constraints.maxWidth < 300 || textScale >= 2) {
          return Column(
            key: const Key('upstream-capture-actions-stacked'),
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [first, const SizedBox(height: 8), second],
          );
        }
        return Row(
          key: const Key('upstream-capture-actions-inline'),
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Expanded(child: first),
            const SizedBox(width: 10),
            Expanded(child: second),
          ],
        );
      },
    );
  }
}

class EllaCaptureTranscriptSheet extends StatefulWidget {
  const EllaCaptureTranscriptSheet({
    required this.provider,
    required this.sourceLabel,
    required this.onClose,
    required this.isCurrent,
    required this.presentationChanges,
    this.detailsBuilder,
    this.actionsBuilder,
    this.diagnosticReporter,
  });

  @visibleForTesting
  final Future<void> Function(String report)? diagnosticReporter;
  final WidgetBuilder? detailsBuilder;
  final WidgetBuilder? actionsBuilder;
  final CaptureProvider provider;
  final bool Function() isCurrent;
  final Listenable presentationChanges;
  final String sourceLabel;
  final bool Function() onClose;

  @override
  State<EllaCaptureTranscriptSheet> createState() => _EllaCaptureTranscriptSheetState();
}

class _EllaCaptureTranscriptSheetState extends State<EllaCaptureTranscriptSheet> {
  final TranscriptViewDiagnostics _viewDiagnostics = TranscriptViewDiagnostics();

  bool _observeView() {
    final current = widget.isCurrent();
    // A retired viewer never reads provider data for presentation or support.
    final state = current ? widget.provider.recordingState : null;
    final ready = current ? widget.provider.transcriptServiceReady : null;
    final count = current ? widget.provider.segments.length : null;
    final branch = !current ? TranscriptViewBranch.retired
        : count != 0 ? TranscriptViewBranch.words
        : state == RecordingState.pause ? TranscriptViewBranch.paused
        : (state == RecordingState.record || state == RecordingState.deviceRecord)
            ? ready == true ? TranscriptViewBranch.listening
                : TranscriptViewBranch.transcriptionUnconfirmed
            : TranscriptViewBranch.empty;
    _viewDiagnostics.observe(viewerCurrent: current, branch: branch,
      recordingState: state?.name, transcriptionReady: ready, segmentCount: count);
    return current;
  }

  Future<void> _shareStatus() async {
    // Refresh the owner/window predicate at the tap, including retirement since
    // the last paint; this clears prior-state history before exporting.
    _observeView();
    final report = _viewDiagnostics.supportReport();
    try {
      if (widget.diagnosticReporter != null) {
        await widget.diagnosticReporter!(report);
      } else {
        await Share.share(report);
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.maybeOf(context)?.showSnackBar(
          SnackBar(content: Text(context.l10n.somethingWentWrong)));
      }
    }
  }

  @override
  void dispose() {
    _viewDiagnostics.dispose();
    super.dispose();
  }

  Widget _transcriptHeaderTitle(BuildContext context) => Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Semantics(
                          header: true,
                          child: Text(
                            context.l10n.irisNewestWords,
                            key: const Key('upstream-capture-transcript-title'),
                            style: Theme.of(context).textTheme.titleLarge,
                          ),
                        ),
                        Text(
                          widget.sourceLabel,
                          style: EllaTypography.of(context).caption.copyWith(color: EllaPalette.of(context).inkSoft),
                        ),
                      ],
                    );

  List<Widget> _transcriptHeaderActions(BuildContext context) => [
                  IconButton(
                    key: const Key('upstream-capture-transcript-support'),
                    tooltip: context.l10n.irisSendSupportReport,
                    constraints: const BoxConstraints.tightFor(
                      width: EllaSizes.minTouchTarget, height: EllaSizes.minTouchTarget),
                    onPressed: _shareStatus,
                    icon: Icon(Icons.ios_share_rounded, color: EllaPalette.of(context).tealDeep),
                  ),
                  IconButton(
                    key: const Key('upstream-capture-transcript-close'),
                    tooltip: context.l10n.close,
                    constraints: const BoxConstraints.tightFor(
                      width: EllaSizes.minTouchTarget,
                      height: EllaSizes.minTouchTarget,
                    ),
                    onPressed: () {
                      if (widget.onClose()) Navigator.of(context).pop();
                    },
                    icon: Icon(Icons.close_rounded, color: EllaPalette.of(context).tealDeep),
                  ),
  ];

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: 0.72,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 18, 12, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              LayoutBuilder(builder: (context, constraints) {
                final stacked = constraints.maxWidth < 360 || MediaQuery.textScalerOf(context).scale(1) >= 2;
                if (stacked) {
                  return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    Row(mainAxisAlignment: MainAxisAlignment.end,
                      children: _transcriptHeaderActions(context)),
                    _transcriptHeaderTitle(context),
                  ]);
                }
                return Row(children: [
                  Expanded(child: _transcriptHeaderTitle(context)),
                  ..._transcriptHeaderActions(context),
                ]);
              }),
              const SizedBox(height: 12),
              Expanded(
                child: AnimatedBuilder(
                  animation: Listenable.merge([widget.provider, widget.presentationChanges]),
                  builder: (context, _) {
                    if (!_observeView()) {
                      return Center(child: Text(context.l10n.upstreamCaptureUnavailable));
                    }
                    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                      if (widget.detailsBuilder != null)
                        ConstrainedBox(
                          constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .2),
                          child: SingleChildScrollView(child: widget.detailsBuilder!(context)),
                        ),
                      Expanded(child: Builder(builder: (context) {
                        final segments = widget.provider.segments;
                        if (segments.isEmpty) {
                          final listening = (widget.provider.recordingState == RecordingState.record ||
                                  widget.provider.recordingState == RecordingState.deviceRecord) &&
                              widget.provider.transcriptServiceReady;
                          return Semantics(
                            liveRegion: true,
                            child: Center(
                              child: Text(
                                listening
                                    ? context.l10n.upstreamCaptureTranscriptEmpty
                                    : context.l10n.upstreamCaptureUnavailable,
                                key: const Key('upstream-capture-transcript-empty'),
                                textAlign: TextAlign.center,
                                style: EllaTypography.of(context).body.copyWith(color: EllaPalette.of(context).inkSoft),
                              ),
                            ),
                          );
                        }
                        return ListView.separated(
                          key: const Key('upstream-capture-transcript-list'),
                          padding: const EdgeInsets.only(right: 8, bottom: 20),
                          itemCount: segments.length,
                          separatorBuilder: (_, __) => const SizedBox(height: 8),
                          itemBuilder: (context, index) => _TranscriptSegmentCard(segment: segments[index]),
                        );
                      })),
                      if (widget.actionsBuilder != null) widget.actionsBuilder!(context),
                    ]);
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _TranscriptSegmentCard extends StatelessWidget {
  const _TranscriptSegmentCard({required this.segment});

  final TranscriptSegment segment;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(color: EllaPalette.of(context).card, borderRadius: BorderRadius.circular(14)),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Text(segment.text, style: EllaTypography.of(context).body.copyWith(color: EllaPalette.of(context).ink)),
      ),
    );
  }
}

class _WhispersRow extends StatelessWidget {
  const _WhispersRow({
    required this.verified,
    required this.enabled,
    required this.busy,
    required this.playback,
    required this.error,
    required this.onChanged,
    required this.onRetry,
  });

  final bool verified;
  final bool enabled;
  final bool busy;
  final _WhisperPlaybackState playback;
  final String? error;
  final ValueChanged<bool> onChanged;
  final VoidCallback onRetry;

  String _description(BuildContext context) {
    if (busy) return context.l10n.upstreamCaptureSavingWhispers;
    if (!verified) return context.l10n.todayWhispersUnavailable;
    if (!enabled) {
      return playback == _WhisperPlaybackState.error
          ? context.l10n.upstreamCaptureWhispersPlaybackStopFailed
          : error != null
              ? context.l10n.upstreamCaptureWhispersSaveFailed
              : context.l10n.upstreamCaptureWhispersOffDescription;
    }
    return playback == _WhisperPlaybackState.ready
        ? error == null
            ? context.l10n.todayWhispersOnDescription
            : context.l10n.upstreamCaptureWhispersSaveFailed
        : playback == _WhisperPlaybackState.error
            ? context.l10n.upstreamCaptureWhispersPlaybackFailed
            : context.l10n.upstreamCaptureWhispersPlaybackUnavailable;
  }

  @override
  Widget build(BuildContext context) {
    final canRetry = !verified ||
        playback == _WhisperPlaybackState.error ||
        (enabled && playback == _WhisperPlaybackState.unavailable);
    final textScale = MediaQuery.textScalerOf(context).scale(1);
    final title = Text(
      context.l10n.todayWhispersTitle,
      key: const Key('upstream-capture-whispers-status'),
      style: EllaTypography.of(
        context,
      ).secondary.copyWith(fontWeight: FontWeight.w700, color: EllaPalette.of(context).ink),
    );
    final control = busy
        ? Semantics(
            label: context.l10n.upstreamCaptureSavingWhispers,
            child: SizedBox(
              width: EllaSizes.minTouchTarget,
              height: EllaSizes.minTouchTarget,
              child: Center(
                child: SizedBox(
                  width: 22,
                  height: 22,
                  child: CircularProgressIndicator(strokeWidth: 2, color: EllaPalette.of(context).tealDeep),
                ),
              ),
            ),
          )
        : verified
            ? Semantics(
                label: context.l10n.todayWhispersTitle,
                toggled: enabled,
                child: Switch(
                  key: const Key('upstream-capture-whispers-switch'),
                  value: enabled,
                  onChanged: onChanged,
                  activeTrackColor: EllaPalette.of(context).ink,
                  activeThumbColor: EllaPalette.of(context).paper,
                ),
              )
            : const SizedBox.shrink();
    return Semantics(
      container: true,
      liveRegion: true,
      child: Column(
        key: textScale >= 2
            ? const Key('upstream-capture-whispers-stacked')
            : const Key('upstream-capture-whispers-row'),
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (textScale >= 2) ...[
            title,
            Align(alignment: AlignmentDirectional.centerEnd, child: control),
          ] else
            Row(
              children: [
                Expanded(child: title),
                control,
              ],
            ),
          const SizedBox(height: 2),
          Text(_description(context), style: EllaTypography.of(context).secondary),
          if (canRetry && !busy)
            Align(
              alignment: AlignmentDirectional.centerStart,
              child: TextButton.icon(
                key: const Key('upstream-capture-whispers-retry'),
                style: _DockButtonStyles.text(context),
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: Text(context.l10n.tryAgain),
              ),
            ),
        ],
      ),
    );
  }
}

class _DockPrimaryAction extends StatefulWidget {
  _DockPrimaryAction({
    required this.buttonKey,
    required this.captureAdmission,
    required this.onPressed,
    required this.icon,
    required this.label,
  }) : super(key: ValueKey<Key>(buttonKey));

  final Key buttonKey;
  final bool Function() Function() captureAdmission;
  final VoidCallback? onPressed;
  final IconData icon;
  final Widget label;

  @override
  State<_DockPrimaryAction> createState() => _DockPrimaryActionState();
}

class _DockPrimaryActionState extends State<_DockPrimaryAction> {
  final _states = WidgetStatesController();
  bool _pressed = false;
  bool Function()? _pressAdmission;
  int _pressGeneration = 0;

  @override
  void initState() {
    super.initState();
    _states.addListener(_onStatesChanged);
  }

  void _onStatesChanged() {
    final pressed = widget.onPressed != null && _states.value.contains(WidgetState.pressed);
    if (pressed == _pressed) return;
    if (pressed) {
      _pressGeneration++;
      _pressAdmission = widget.captureAdmission();
    } else {
      final generation = _pressGeneration;
      // Native button activation follows pressed=false synchronously. A cancelled
      // gesture has no activation, so its admission retires in this microtask.
      scheduleMicrotask(() {
        if (mounted && generation == _pressGeneration && !_pressed) _pressAdmission = null;
      });
    }
    setState(() => _pressed = pressed);
  }

  void _activate() {
    if (widget.onPressed == null) return;
    final admitted = (_pressAdmission ?? widget.captureAdmission())();
    _pressGeneration++;
    _pressAdmission = null;
    if (admitted) widget.onPressed!();
  }

  @override
  void didUpdateWidget(covariant _DockPrimaryAction oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.onPressed == null || widget.buttonKey != oldWidget.buttonKey) {
      _pressGeneration++;
      _pressAdmission = () => false;
      _pressed = false;
      final generation = _pressGeneration;
      scheduleMicrotask(() {
        if (mounted && generation == _pressGeneration && !_pressed) _pressAdmission = null;
      });
    }
  }

  @override
  void dispose() {
    _pressGeneration++;
    _states.removeListener(_onStatesChanged);
    _states.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.disableAnimationsOf(context) || MediaQuery.accessibleNavigationOf(context);
    final duration = reduceMotion ? Duration.zero : Duration(milliseconds: _pressed ? 90 : 140);
    return AnimatedScale(
      scale: !reduceMotion && _pressed ? 0.98 : 1,
      duration: duration,
      child: FilledButton.icon(
        key: widget.buttonKey,
        statesController: _states,
        style: _DockButtonStyles.primary(context).copyWith(animationDuration: duration),
        onPressed: widget.onPressed == null ? null : _activate,
        icon: Icon(widget.icon),
        label: widget.label,
      ),
    );
  }
}

class _DockButtonStyles {
  const _DockButtonStyles._();

  static ButtonStyle primary(BuildContext context) => ButtonStyle(
        minimumSize: const WidgetStatePropertyAll(Size(0, 56)),
        iconSize: const WidgetStatePropertyAll(20),
        enableFeedback: false,
        padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 14, vertical: 10)),
        foregroundColor: WidgetStateProperty.resolveWith(
          (states) =>
              states.contains(WidgetState.disabled) ? EllaPalette.of(context).inkSoft : EllaPalette.of(context).paper,
        ),
        backgroundColor: WidgetStateProperty.resolveWith(
          (states) =>
              states.contains(WidgetState.disabled) ? EllaPalette.of(context).cardDeep : EllaPalette.of(context).ink,
        ),
        overlayColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.pressed)) return EllaPalette.of(context).ink.withValues(alpha: 0.08);
          if (states.contains(WidgetState.focused)) return EllaPalette.of(context).ink.withValues(alpha: 0.12);
          return null;
        }),
        textStyle: WidgetStatePropertyAll(
          Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 17, fontWeight: FontWeight.w700),
        ),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(borderRadius: BorderRadius.circular(28))),
      );

  static ButtonStyle secondary(BuildContext context) => ButtonStyle(
        minimumSize: const WidgetStatePropertyAll(Size(0, 48)),
        iconSize: const WidgetStatePropertyAll(20),
        enableFeedback: false,
        padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 14, vertical: 10)),
        foregroundColor: WidgetStateProperty.resolveWith(
          (states) =>
              states.contains(WidgetState.disabled) ? EllaPalette.of(context).inkSoft : EllaPalette.of(context).ink,
        ),
        backgroundColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.disabled)) return EllaPalette.of(context).card;
          if (states.contains(WidgetState.pressed) || states.contains(WidgetState.focused)) {
            return EllaPalette.of(context).cardDeep;
          }
          return EllaPalette.of(context).elevatedCard;
        }),
        side: WidgetStateProperty.resolveWith(
          (states) => BorderSide(
            color: states.contains(WidgetState.disabled)
                ? EllaPalette.of(context).cardEdge.withValues(alpha: 0.55)
                : EllaPalette.of(context).ink,
            width: states.contains(WidgetState.focused) ? 2.5 : 1.5,
          ),
        ),
        textStyle: WidgetStatePropertyAll(
          Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
        ),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(borderRadius: BorderRadius.circular(28))),
      );

  static ButtonStyle text(BuildContext context) => TextButton.styleFrom(
        foregroundColor: EllaPalette.of(context).ink,
        minimumSize: const Size(0, EllaSizes.minTouchTarget),
        textStyle: Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
      );
}

class _DockSurface extends StatelessWidget {
  const _DockSurface({required this.child, this.capsule = false});

  final bool capsule;

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final surface = Material(
      color: Colors.transparent,
      borderRadius: capsule ? BorderRadius.circular(24) : null,
      clipBehavior: capsule ? Clip.antiAlias : Clip.none,
      elevation: 0,
      child: Padding(
        padding: EdgeInsets.symmetric(horizontal: capsule ? 8 : 12, vertical: capsule ? 0 : 16),
        child: child,
      ),
    );
    return capsule
        ? Align(
            alignment: Alignment.center,
            heightFactor: 1,
            child: ConstrainedBox(constraints: const BoxConstraints(maxWidth: 366), child: surface),
          )
        : EllaV7Glass(radius: 38, child: surface);
  }
}
