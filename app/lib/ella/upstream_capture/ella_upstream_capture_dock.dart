import 'dart:async';

import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/guardian_mode_service.dart' as guardian_native;
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
// Reuses today_page's own Whispers status text and testing seams instead of
// reimplementing them — see UPSTREAM_PATCHES.md patch Six.
import 'package:omi/pages/home/today_page.dart'
    show
        whisperStatusLead,
        whisperStatusDetail,
        GuardianAvailability,
        GuardianModeLoader,
        GuardianModeSetter,
        GuardianNativeLifecycle;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Flag-ON home capture dock: a thin view over upstream's CaptureProvider.
/// Every button is one call into [EllaUpstreamCaptureRuntime], which forwards
/// to upstream's public capture/device API after the consent bind.
class EllaUpstreamCaptureDock extends StatefulWidget {
  const EllaUpstreamCaptureDock({
    super.key,
    EllaUpstreamCaptureRuntime? runtime,
    this.authenticatedUid,
    this.guardianAvailability,
    this.guardianModeLoader,
    this.guardianModeSetter,
    this.guardianNativeStart,
    this.guardianNativeStop,
  }) : _runtime = runtime;

  final EllaUpstreamCaptureRuntime? _runtime;
  final String Function()? authenticatedUid;

  // Same injectable seams `today_page.dart`'s dock uses for Whispers, reused
  // here rather than reimplemented — see UPSTREAM_PATCHES.md patch Six.
  final GuardianAvailability? guardianAvailability;
  final GuardianModeLoader? guardianModeLoader;
  final GuardianModeSetter? guardianModeSetter;
  final GuardianNativeLifecycle? guardianNativeStart;
  final GuardianNativeLifecycle? guardianNativeStop;

  @override
  State<EllaUpstreamCaptureDock> createState() => _EllaUpstreamCaptureDockState();
}

class _EllaUpstreamCaptureDockState extends State<EllaUpstreamCaptureDock> {
  late final EllaUpstreamCaptureRuntime _runtime = widget._runtime ?? EllaUpstreamCaptureRuntime.instance;
  CaptureProvider? _provider;
  bool _busy = false;
  String? _message;
  bool _showTranscript = false;
  bool _whispersAvailable = false;
  bool _whispersOn = false;
  bool _whispersBusy = false;

  String get _uid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;

  @override
  void initState() {
    super.initState();
    unawaited(
      _runtime
          .ensureBooted()
          .then((provider) {
            if (!mounted) return;
            setState(() => _provider = provider);
          })
          .catchError((Object error) {
            if (!mounted) return;
            setState(() => _message = context.l10n.upstreamCaptureUnavailable);
          }),
    );
    unawaited(_loadWhispersState());
  }

  bool get _guardianAvailable => widget.guardianAvailability?.call() ?? allowsGuardianSurface();

  Future<GuardianModeInfo?> _readWhisperState() async {
    final loader = widget.guardianModeLoader;
    if (loader != null) return loader();
    final result = await guardian_api.getGuardianMode();
    return result.isSuccess ? result.value : null;
  }

  Future<bool> _writeWhisperState(GuardianModeState state) async {
    final setter = widget.guardianModeSetter;
    if (setter != null) return setter(state);
    return (await guardian_api.setGuardianModeTwoTier(state)).isSuccess;
  }

  Future<void> _startWhisperNative() =>
      widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start();

  Future<void> _stopWhisperNative() =>
      widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop();

  Future<void> _loadWhispersState() async {
    if (!_guardianAvailable) return;
    try {
      final info = await _readWhisperState();
      if (!mounted || info == null) return;
      setState(() {
        _whispersAvailable = true;
        _whispersOn = !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);
      });
    } catch (_) {
      // Leave whispers hidden; _whispersAvailable stays false.
    }
  }

  Future<void> _setWhispers(bool enabled) async {
    if (_whispersBusy || !_whispersAvailable) return;
    setState(() {
      _whispersOn = enabled;
      _whispersBusy = true;
    });
    final state = enabled ? const GuardianModeState(features: ['ACTIVE_SUPPORT']) : const GuardianModeState();
    if (!enabled) {
      try {
        await _stopWhisperNative();
      } catch (_) {}
    }
    var success = false;
    try {
      success = await _writeWhisperState(state);
    } catch (_) {
      success = false;
    }
    if (enabled) {
      try {
        if (success) {
          await _startWhisperNative();
        } else {
          await _stopWhisperNative();
          await _writeWhisperState(const GuardianModeState());
        }
      } catch (_) {}
    }
    if (!mounted) return;
    setState(() {
      _whispersBusy = false;
      if (!success) _whispersOn = !enabled;
    });
  }

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      await action();
    } catch (_) {
      if (mounted) setState(() => _message = context.l10n.upstreamCaptureUnavailable);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _startPhone() => _run(() async {
    final started = await _runtime.startPhoneCapture(_uid);
    if (!started && mounted) await _consentRequired();
  });

  Future<void> _connectNecklace() => _run(() async {
    setState(() => _message = context.l10n.upstreamCaptureSearching);
    final devices = await _runtime.discoverNecklaces();
    if (!mounted) return;
    if (devices.isEmpty) {
      setState(() => _message = context.l10n.upstreamCaptureNoNecklaceFound);
      return;
    }
    final device = devices.length == 1 ? devices.first : await _pickDevice(devices);
    if (device == null || !mounted) return;
    setState(() => _message = null);
    final connected = await _runtime.connectNecklace(_uid, device);
    if (!connected && mounted) await _consentRequired();
  });

  Future<void> _consentRequired() async {
    setState(() => _message = context.l10n.upstreamCaptureConsentRequired);
    await AiConsentCoordinator.ensure(context);
  }

  Future<BtDevice?> _pickDevice(List<BtDevice> devices) {
    return showModalBottomSheet<BtDevice>(
      context: context,
      builder: (context) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final device in devices)
              ListTile(
                key: Key('upstream-capture-device-${device.id}'),
                title: Text(device.name),
                onTap: () => Navigator.of(context).pop(device),
              ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final provider = _provider;
    if (provider == null) {
      return _DockSurface(child: Text(_message ?? context.l10n.upstreamCaptureStarting));
    }
    return AnimatedBuilder(
      animation: provider,
      builder: (context, _) {
        final state = provider.recordingState;
        final phoneLive = state == RecordingState.record || provider.isPhoneMicBatchRecording;
        final necklaceLive = state == RecordingState.deviceRecord || provider.havingRecordingDevice;
        final starting = _busy || state == RecordingState.initialising;
        final status = starting
            ? context.l10n.upstreamCaptureStarting
            : phoneLive
            ? context.l10n.upstreamCaptureRecordingPhone
            : necklaceLive
            ? context.l10n.upstreamCaptureRecordingNecklace
            : (_message ?? '');
        return _DockSurface(
          child: Column(
            key: const Key('upstream-capture-dock'),
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (status.isNotEmpty) Text(status, style: const TextStyle(fontSize: 18, color: EllaColors.ink)),
              if (_message != null && (phoneLive || necklaceLive || starting))
                Text(_message!, style: const TextStyle(fontSize: 16, color: EllaColors.inkSoft)),
              if (status.isNotEmpty) const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: phoneLive
                        ? FilledButton(
                            key: const Key('upstream-capture-stop-phone'),
                            onPressed: starting ? null : () => _run(_runtime.stopPhoneCapture),
                            child: Text(context.l10n.upstreamCaptureStop),
                          )
                        : FilledButton(
                            key: const Key('upstream-capture-record-phone'),
                            onPressed: starting ? null : _startPhone,
                            child: Text(context.l10n.upstreamCaptureRecordPhone),
                          ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: necklaceLive
                        ? OutlinedButton(
                            key: const Key('upstream-capture-disconnect-necklace'),
                            onPressed: starting ? null : () => _run(_runtime.disconnectNecklace),
                            child: Text(context.l10n.upstreamCaptureDisconnectNecklace),
                          )
                        : OutlinedButton(
                            key: const Key('upstream-capture-connect-necklace'),
                            onPressed: starting ? null : _connectNecklace,
                            child: Text(context.l10n.upstreamCaptureConnectNecklace),
                          ),
                  ),
                ],
              ),
              if (phoneLive || necklaceLive) ...[
                const SizedBox(height: 6),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    key: const Key('upstream-capture-view-transcript'),
                    onPressed: () => setState(() => _showTranscript = !_showTranscript),
                    child: Text(
                      necklaceLive ? context.l10n.todayDockTranscriptNecklace : context.l10n.todayDockTranscriptPhone,
                    ),
                  ),
                ),
                if (_showTranscript) _TranscriptPanel(segments: provider.segments),
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    key: const Key('upstream-capture-finish'),
                    onPressed: starting ? null : () => _run(_runtime.finishConversation),
                    child: Text(context.l10n.upstreamCaptureFinish),
                  ),
                ),
              ],
              if (_whispersAvailable) ...[
                const SizedBox(height: 10),
                _WhispersRow(enabled: _whispersOn, busy: _whispersBusy, onChanged: _setWhispers),
              ],
            ],
          ),
        );
      },
    );
  }
}

class _TranscriptPanel extends StatelessWidget {
  const _TranscriptPanel({required this.segments});

  final List<TranscriptSegment> segments;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('upstream-capture-transcript-panel'),
      constraints: const BoxConstraints(maxHeight: 220),
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: EllaColors.cardDeep, borderRadius: BorderRadius.circular(16)),
      child: segments.isEmpty
          ? Text(context.l10n.upstreamCaptureStarting, style: const TextStyle(color: EllaColors.inkSoft))
          : ListView.builder(
              shrinkWrap: true,
              itemCount: segments.length,
              itemBuilder: (context, index) {
                final segment = segments[index];
                return Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Text(segment.text, style: const TextStyle(color: EllaColors.ink)),
                );
              },
            ),
    );
  }
}

class _WhispersRow extends StatelessWidget {
  const _WhispersRow({required this.enabled, required this.busy, required this.onChanged});

  final bool enabled;
  final bool busy;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return Row(
      key: const Key('upstream-capture-whispers-row'),
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                whisperStatusLead(enabled),
                key: const Key('upstream-capture-whispers-status'),
                style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600, color: EllaColors.ink),
              ),
              Text(whisperStatusDetail(enabled), style: const TextStyle(fontSize: 12, color: EllaColors.inkSoft)),
            ],
          ),
        ),
        if (busy)
          const SizedBox(
            width: 20,
            height: 20,
            child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
          )
        else
          Switch(key: const Key('upstream-capture-whispers-switch'), value: enabled, onChanged: onChanged),
      ],
    );
  }
}

class _DockSurface extends StatelessWidget {
  const _DockSurface({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: EllaColors.elevatedCard,
      elevation: 6,
      borderRadius: BorderRadius.circular(24),
      child: Padding(padding: const EdgeInsets.all(16), child: child),
    );
  }
}
