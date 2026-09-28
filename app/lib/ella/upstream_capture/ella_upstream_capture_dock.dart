import 'dart:async';

import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Flag-ON home capture dock: a thin view over upstream's CaptureProvider.
/// Every button is one call into [EllaUpstreamCaptureRuntime], which forwards
/// to upstream's public capture/device API after the consent bind.
class EllaUpstreamCaptureDock extends StatefulWidget {
  const EllaUpstreamCaptureDock({super.key, EllaUpstreamCaptureRuntime? runtime, this.authenticatedUid})
      : _runtime = runtime;

  final EllaUpstreamCaptureRuntime? _runtime;
  final String Function()? authenticatedUid;

  @override
  State<EllaUpstreamCaptureDock> createState() => _EllaUpstreamCaptureDockState();
}

class _EllaUpstreamCaptureDockState extends State<EllaUpstreamCaptureDock> {
  late final EllaUpstreamCaptureRuntime _runtime = widget._runtime ?? EllaUpstreamCaptureRuntime.instance;
  CaptureProvider? _provider;
  bool _busy = false;
  String? _message;

  String get _uid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;

  @override
  void initState() {
    super.initState();
    unawaited(
      _runtime.ensureBooted().then((provider) {
        if (!mounted) return;
        setState(() => _provider = provider);
      }).catchError((Object error) {
        if (!mounted) return;
        setState(() => _message = context.l10n.upstreamCaptureUnavailable);
      }),
    );
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
              if (phoneLive || necklaceLive)
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    key: const Key('upstream-capture-finish'),
                    onPressed: starting ? null : () => _run(_runtime.finishConversation),
                    child: Text(context.l10n.upstreamCaptureFinish),
                  ),
                ),
            ],
          ),
        );
      },
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
