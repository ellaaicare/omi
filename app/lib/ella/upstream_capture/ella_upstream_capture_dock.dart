import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
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

enum _DockOperation {
  idle,
  booting,
  searching,
  connecting,
  stopping,
  disconnecting,
  finishing,
}

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
  }) : _runtime = runtime;

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
  final FocusNode _connectFocusNode = FocusNode(debugLabel: 'Connect necklace');
  final FocusNode _transcriptFocusNode = FocusNode(debugLabel: 'Open transcript');
  CaptureProvider? _provider;
  _DockOperation _operation = _DockOperation.booting;
  String? _message;
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
  bool get _busy => _operation != _DockOperation.idle;
  bool get _guardianAvailable => widget.guardianAvailability?.call() ?? allowsGuardianSurface();

  void _actionFeedback() {
    if (!mounted || _uid.isEmpty) return;
    // Feedback acknowledges the tap, not successful capture or a server write.
    unawaited(HapticFeedback.lightImpact().catchError((_) {}));
  }

  @override
  void initState() {
    super.initState();
    _runtime.protocolUnavailable.addListener(_onProtocolStatus);
    _whisperSharedRevision = _whisperFence.revision;
    _whisperRefreshRevision = _whisperFence.refreshRevision;
    _whisperFence.addListener(_onSharedWhisperStateChanged);
    _whisperStateSubscription =
        (widget.guardianNativeStates ?? guardian_native.GuardianModeService().stateStream).listen((_) {
      if (mounted && _whispersVerified && !_whispersBusy && _whisperFence.snapshot != null) {
        setState(() => _whisperPlayback = _playbackStateFor(_whispersOn));
      }
    });
    unawaited(_boot());
    unawaited(_loadWhispersState());
  }

  Future<void> _boot() async {
    if (mounted) {
      setState(() {
        _operation = _DockOperation.booting;
        _message = null;
      });
    }
    try {
      final provider = await _runtime.ensureBooted();
      if (!mounted) return;
      setState(() {
        _provider = provider;
        _operation = _DockOperation.idle;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _operation = _DockOperation.idle;
        _message = context.l10n.upstreamCaptureUnavailable;
      });
    }
  }

  void _onProtocolStatus() {
    if (!mounted) return;
    final unavailable = _runtime.protocolUnavailable.value;
    setState(() {
      if (unavailable) {
        _protocolMessageActive = true;
        _message = context.l10n.todayTranscriptionUnavailable;
      } else if (_protocolMessageActive) {
        _protocolMessageActive = false;
        _message = null;
      }
    });
  }

  @override
  void dispose() {
    _runtime.protocolUnavailable.removeListener(_onProtocolStatus);
    _whisperFence.removeListener(_onSharedWhisperStateChanged);
    final pending = _pendingWhisperChoice;
    if (pending != null) _whisperFence.abandon(pending);
    unawaited(_whisperStateSubscription?.cancel());
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

  Future<void> _startWhisperNative() =>
      widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start();

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
        setState(() {
          _whispersVerified = false;
          _whisperError = context.l10n.todayWhispersUnavailable;
        });
        return;
      }
      final enabled = !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);
      _whisperFence.publish(
        operation,
        (
          enabled: enabled,
          modeVerified: true,
          nativeReconciled: enabled
              ? _playbackStateFor(enabled) == _WhisperPlaybackState.ready
              : _nativeWhisperState() == guardian_native.GuardianModeState.idle
        ),
      );
      setState(() {
        _whispersVerified = true;
        _whispersOn = enabled;
        _whisperPlayback = _playbackStateFor(enabled);
        _whisperError = null;
      });
    } catch (_) {
      if (!_isWhisperOperationCurrent(operation)) return;
      setState(() {
        _whispersVerified = false;
        _whisperError = context.l10n.todayWhispersUnavailable;
      });
    }
  }

  bool _isWhisperOperationCurrent(guardian_native.GuardianWhisperOperation operation) =>
      mounted && _guardianAvailable && operation.isCurrent;

  void _showWhisperAuthorityUnavailable() {
    if (!mounted || !_guardianAvailable || _whisperFence.choicePending) return;
    setState(() {
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
    setState(() {
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
      setState(() {
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
              await _startWhisperNative();
              playback = _WhisperPlaybackState.ready;
            } else {
              await _stopWhisperNative();
            }
          } catch (_) {
            playback = _WhisperPlaybackState.error;
          }
        } else if (enabled) {
          try {
            await _startWhisperNative();
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
      _whisperFence.publish(
        operation,
        (
          enabled: resolvedEnabled,
          modeVerified: modeVerified,
          nativeReconciled: modeVerified && playback != _WhisperPlaybackState.error
        ),
      );
      setState(() {
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

  Future<void> _retryWhispers() async {
    if (_whispersBusy) return;
    if (!_whispersVerified) {
      final operation = _whisperFence.observe(widget.guardianAuthorityProvider ?? WalOwnerAuthority.active);
      if (operation == null) {
        _showWhisperAuthorityUnavailable();
        return;
      }
      if (_isWhisperOperationCurrent(operation)) _actionFeedback();
      setState(() => _whispersBusy = true);
      await _loadWhispersState();
      if (_isWhisperOperationCurrent(operation)) setState(() => _whispersBusy = false);
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
    setState(() {
      _whispersBusy = true;
      _whisperError = null;
    });
    try {
      await _whisperFence.serialize<void>(operation, () async {
        if (!_isWhisperOperationCurrent(operation)) return;
        if (enabled) {
          await _startWhisperNative();
        } else {
          await _stopWhisperNative();
        }
      });
      if (!_isWhisperOperationCurrent(operation)) return;
      _whisperFence.publish(operation, (enabled: enabled, modeVerified: true, nativeReconciled: true));
      setState(() {
        _whisperPlayback = enabled ? _WhisperPlaybackState.ready : _WhisperPlaybackState.unavailable;
      });
    } catch (_) {
      if (!_isWhisperOperationCurrent(operation)) return;
      setState(() {
        _whisperPlayback = _WhisperPlaybackState.error;
        _whisperError = enabled ? playbackFailedMessage : playbackStopFailedMessage;
      });
    } finally {
      if (_isWhisperOperationCurrent(operation)) setState(() => _whispersBusy = false);
    }
  }

  Future<void> _run(
    _DockOperation operation,
    Future<void> Function() action, {
    required String failureMessage,
  }) async {
    if (_busy) return;
    _actionFeedback();
    setState(() {
      _operation = operation;
      _message = null;
    });
    try {
      await action();
    } catch (_) {
      if (mounted) setState(() => _message = failureMessage);
    } finally {
      if (mounted) setState(() => _operation = _DockOperation.idle);
    }
  }

  Future<void> _handleStartOutcome(EllaCaptureStartOutcome outcome,
      {required bool necklace, required String originUid}) async {
    if (originUid.isEmpty || _uid != originUid) return;
    switch (outcome) {
      case EllaCaptureStartOutcome.started:
        if (mounted) {
          setState(() {
            if (!_runtime.protocolUnavailable.value) _message = null;
            _necklaceRetryAvailable = false;
          });
        }
        return;
      case EllaCaptureStartOutcome.unavailable:
        if (mounted) {
          setState(() {
            _message =
                necklace ? context.l10n.upstreamCaptureConnectionFailed : context.l10n.upstreamCapturePhoneStartFailed;
            _necklaceRetryAvailable = necklace;
          });
        }
        return;
      case EllaCaptureStartOutcome.consentRequired:
        if (!mounted) return;
        setState(() => _message = context.l10n.upstreamCaptureConsentRequired);
        final accepted = await (widget.consentRequester?.call(context) ?? AiConsentCoordinator.ensure(context));
        if (!mounted) return;
        setState(() {
          _message =
              accepted ? context.l10n.upstreamCapturePermissionUpdated : context.l10n.upstreamCaptureConsentRequired;
        });
        return;
    }
  }

  Future<void> _startPhone() => _run(
        _DockOperation.connecting,
        () async {
          final originUid = _uid;
          await _handleStartOutcome(await _runtime.startPhoneCapture(originUid), necklace: false, originUid: originUid);
        },
        failureMessage: context.l10n.upstreamCapturePhoneStartFailed,
      );

  Future<void> _connectNecklace() async {
    if (_busy) return;
    final originUid = _uid;
    if (originUid.isEmpty) return;
    _actionFeedback();
    setState(() {
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
      setState(() {
        _message = context.l10n.upstreamCaptureSearchFailed;
        _necklaceRetryAvailable = true;
      });
    } finally {
      if (mounted) {
        setState(() => _operation = _DockOperation.idle);
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && _uid == originUid) _connectFocusNode.requestFocus();
        });
      }
    }
  }

  Future<void> _openTranscript(CaptureProvider provider, {required bool necklace}) async {
    if (!mounted || _transcriptOpen) return;
    _transcriptOpen = true;
    final originUid = _uid;
    final originAuthority = (widget.guardianAuthorityProvider ?? WalOwnerAuthority.active)();
    var closing = false;
    _actionFeedback();
    try {
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        backgroundColor: EllaColors.paper,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(EllaSizes.cardRadius)),
        ),
        builder: (sheetContext) => _TranscriptSheet(
          provider: provider,
          sourceLabel:
              necklace ? sheetContext.l10n.todayDockTranscriptNecklace : sheetContext.l10n.todayDockTranscriptPhone,
          onClose: () {
            if (closing || !sheetContext.mounted || ModalRoute.of(sheetContext)?.isCurrent != true) return false;
            closing = true;
            if (mounted && _uid == originUid && originAuthority?.isExactCurrent() == true) _actionFeedback();
            return true;
          },
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
        _DockOperation.disconnecting => context.l10n.upstreamCaptureDisconnecting,
        _DockOperation.finishing => context.l10n.upstreamCaptureFinishing,
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
  }) {
    final text = _actionLabel(context, label, compactLabel: compactLabel);
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
  Widget build(BuildContext context) {
    final provider = _provider;
    if (provider == null) {
      final bootFailed = _operation == _DockOperation.idle;
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
              FilledButton.icon(
                key: const Key('upstream-capture-retry-boot'),
                style: _DockButtonStyles.primary(context),
                onPressed: () {
                  if (_busy) return;
                  _actionFeedback();
                  unawaited(_boot());
                },
                icon: const Icon(Icons.refresh_rounded),
                label: Text(context.l10n.retry),
              ),
            ],
          ],
        ),
      );
    }
    return AnimatedBuilder(
      animation: provider,
      builder: (context, _) {
        final state = provider.recordingState;
        final phoneActive = state == RecordingState.record || provider.isPhoneMicBatchRecording;
        final necklaceBound = state == RecordingState.deviceRecord || provider.havingRecordingDevice;
        final phoneLive =
            (state == RecordingState.record && provider.transcriptServiceReady) || provider.isPhoneMicBatchRecording;
        final necklaceLive = state == RecordingState.deviceRecord && provider.transcriptServiceReady;
        final live = phoneLive || necklaceLive;
        final initializing = state == RecordingState.initialising;
        final operationLabel = _operationLabel(context);
        final status = operationLabel ??
            (_protocolMessageActive && _message != null ? _message : null) ??
            (phoneLive
                ? context.l10n.upstreamCaptureRecordingPhone
                : necklaceLive
                    ? context.l10n.upstreamCaptureRecordingNecklace
                    : (state == RecordingState.record || state == RecordingState.deviceRecord || initializing)
                        ? context.l10n.upstreamCaptureConnectingTranscription
                        : _message);
        final detail = operationLabel != null
            ? null
            : live && provider.segments.isEmpty && !_protocolMessageActive
                ? context.l10n.upstreamCaptureWaitingForSpeech
                : live && _message != null && !_protocolMessageActive
                    ? _message
                    : null;

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
                  active: live || _busy || initializing,
                  live: live,
                  error: !_busy && _message != null && (_protocolMessageActive || !live),
                ),
                const SizedBox(height: 12),
              ],
              _AdaptiveActionPair(
                first: phoneActive
                    ? FilledButton.icon(
                        key: const Key('upstream-capture-stop-phone'),
                        style: _DockButtonStyles.primary(context),
                        onPressed: _busy
                            ? null
                            : () => _run(
                                  _DockOperation.stopping,
                                  _runtime.stopPhoneCapture,
                                  failureMessage: context.l10n.upstreamCaptureUnavailable,
                                ),
                        icon: const Icon(Icons.stop_circle_outlined),
                        label: _actionLabel(
                          context,
                          context.l10n.upstreamCaptureStop,
                          compactLabel: context.l10n.todayDockStop,
                        ),
                      )
                    : FilledButton.icon(
                        key: const Key('upstream-capture-record-phone'),
                        style: _DockButtonStyles.primary(context),
                        onPressed: _busy ? null : _startPhone,
                        icon: const Icon(Icons.mic_none_rounded),
                        label: _actionLabel(
                          context,
                          context.l10n.upstreamCaptureRecordPhone,
                          compactLabel: context.l10n.phone,
                        ),
                      ),
                second: necklaceBound
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
                      )
                    : _secondaryAction(
                        context,
                        key: const Key('upstream-capture-connect-necklace'),
                        focusNode: _connectFocusNode,
                        onPressed: _busy ? null : _connectNecklace,
                        icon: _necklaceRetryAvailable ? Icons.refresh_rounded : Icons.bluetooth_rounded,
                        label:
                            _necklaceRetryAvailable ? context.l10n.retry : context.l10n.upstreamCaptureConnectNecklace,
                        compactLabel: _necklaceRetryAvailable ? context.l10n.retry : context.l10n.connect,
                      ),
              ),
              if (live) ...[
                const SizedBox(height: 8),
                _AdaptiveActionPair(
                  first: _secondaryAction(
                    context,
                    key: const Key('upstream-capture-view-transcript'),
                    focusNode: _transcriptFocusNode,
                    onPressed: () => _openTranscript(provider, necklace: necklaceLive),
                    icon: Icons.subject_rounded,
                    label:
                        necklaceLive ? context.l10n.todayDockTranscriptNecklace : context.l10n.todayDockTranscriptPhone,
                    compactLabel: context.l10n.transcript,
                  ),
                  second: FilledButton.icon(
                    key: const Key('upstream-capture-finish'),
                    style: _DockButtonStyles.primary(context),
                    onPressed: _busy
                        ? null
                        : () => _run(
                              _DockOperation.finishing,
                              _runtime.finishConversation,
                              failureMessage: context.l10n.upstreamCaptureUnavailable,
                            ),
                    icon: const Icon(Icons.check_circle_outline_rounded),
                    label: _actionLabel(
                      context,
                      context.l10n.upstreamCaptureFinish,
                      compactLabel: context.l10n.todayDockFinish,
                    ),
                  ),
                ),
              ],
              if (_guardianAvailable) ...[
                const SizedBox(height: 10),
                const Divider(height: 1, color: EllaColors.cardDeep),
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
              ],
            ],
          ),
        );
      },
    );
  }
}

class _DockStatus extends StatelessWidget {
  const _DockStatus(
      {required this.label,
      required this.active,
      required this.error,
      this.detail,
      this.visualLabel,
      this.live = false});

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
                ? const Icon(Icons.error_outline_rounded, size: 18, color: EllaColors.error)
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
                  style: EllaTextStyles.body.copyWith(
                    color: error ? EllaColors.error : EllaColors.ink,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                if (detail != null) ...[
                  const SizedBox(height: 2),
                  Text(detail!, style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft)),
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

class _TranscriptSheet extends StatelessWidget {
  const _TranscriptSheet({required this.provider, required this.sourceLabel, required this.onClose});

  final CaptureProvider provider;
  final String sourceLabel;
  final bool Function() onClose;

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
              Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Semantics(
                          header: true,
                          child: Text(
                            context.l10n.upstreamCaptureTranscriptTitle,
                            key: const Key('upstream-capture-transcript-title'),
                            style: Theme.of(context).textTheme.titleLarge,
                          ),
                        ),
                        Text(sourceLabel, style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft)),
                      ],
                    ),
                  ),
                  IconButton(
                    key: const Key('upstream-capture-transcript-close'),
                    tooltip: context.l10n.close,
                    constraints: const BoxConstraints.tightFor(
                      width: EllaSizes.minTouchTarget,
                      height: EllaSizes.minTouchTarget,
                    ),
                    onPressed: () {
                      if (onClose()) Navigator.of(context).pop();
                    },
                    icon: const Icon(Icons.close_rounded, color: EllaColors.tealDeep),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Expanded(
                child: AnimatedBuilder(
                  animation: provider,
                  builder: (context, _) {
                    final segments = provider.segments;
                    if (segments.isEmpty) {
                      final listening = (provider.recordingState == RecordingState.record ||
                              provider.recordingState == RecordingState.deviceRecord) &&
                          provider.transcriptServiceReady;
                      return Semantics(
                        liveRegion: true,
                        child: Center(
                          child: Text(
                            listening
                                ? context.l10n.upstreamCaptureTranscriptEmpty
                                : context.l10n.upstreamCaptureUnavailable,
                            key: const Key('upstream-capture-transcript-empty'),
                            textAlign: TextAlign.center,
                            style: EllaTextStyles.body.copyWith(color: EllaColors.inkSoft),
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
      decoration: BoxDecoration(color: EllaColors.card, borderRadius: BorderRadius.circular(14)),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Text(segment.text, style: EllaTextStyles.body.copyWith(color: EllaColors.ink)),
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
      style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w700, color: EllaColors.ink),
    );
    final control = busy
        ? Semantics(
            label: context.l10n.upstreamCaptureSavingWhispers,
            child: const SizedBox(
              width: EllaSizes.minTouchTarget,
              height: EllaSizes.minTouchTarget,
              child: Center(
                child: SizedBox(
                  width: 22,
                  height: 22,
                  child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
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
                  activeTrackColor: EllaColors.tealDeep,
                  activeThumbColor: EllaColors.paper,
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
          Text(_description(context), style: EllaTextStyles.secondary),
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

class _DockButtonStyles {
  const _DockButtonStyles._();

  static ButtonStyle primary(BuildContext context) => ButtonStyle(
        minimumSize: const WidgetStatePropertyAll(Size(0, 52)),
        padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 14, vertical: 10)),
        foregroundColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.disabled) ? EllaColors.inkSoft : EllaColors.paper,
        ),
        backgroundColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.disabled) ? EllaColors.cardDeep : EllaColors.tealDeep,
        ),
        overlayColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.pressed)) return EllaColors.ink.withValues(alpha: 0.18);
          if (states.contains(WidgetState.focused)) return EllaColors.ink.withValues(alpha: 0.12);
          return null;
        }),
        textStyle: WidgetStatePropertyAll(
          Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
        ),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
      );

  static ButtonStyle secondary(BuildContext context) => ButtonStyle(
        minimumSize: const WidgetStatePropertyAll(Size(0, 52)),
        padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 14, vertical: 10)),
        foregroundColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.disabled) ? EllaColors.inkSoft : EllaColors.tealDeep,
        ),
        backgroundColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.disabled)) return EllaColors.card;
          if (states.contains(WidgetState.pressed) || states.contains(WidgetState.focused)) {
            return EllaColors.cardDeep;
          }
          return EllaColors.elevatedCard;
        }),
        side: WidgetStateProperty.resolveWith(
          (states) => BorderSide(
            color: states.contains(WidgetState.disabled)
                ? EllaColors.cardEdge.withValues(alpha: 0.55)
                : EllaColors.tealDeep,
            width: states.contains(WidgetState.focused) ? 2.5 : 1.5,
          ),
        ),
        textStyle: WidgetStatePropertyAll(
          Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
        ),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
      );

  static ButtonStyle text(BuildContext context) => TextButton.styleFrom(
        foregroundColor: EllaColors.tealDeep,
        minimumSize: const Size(0, EllaSizes.minTouchTarget),
        textStyle: Theme.of(context).textTheme.labelLarge?.copyWith(fontSize: 15, fontWeight: FontWeight.w700),
      );
}

class _DockSurface extends StatelessWidget {
  const _DockSurface({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: EllaColors.elevatedCard,
      elevation: 0,
      child: Padding(
        padding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 16,
        ),
        child: child,
      ),
    );
  }
}
