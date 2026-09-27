// Wires the promoted upstream device-connection + capture layer (BLE
// connect/reconnect, necklace raw audio, phone mic) to
// [EllaUpstreamCaptureAdapter]'s account-binding + consent gate, and to
// Ella's socket layer. This is what a Home entry point calls — an
// `ensureConnection`-shaped action, `streamRecording`-shaped actions
// (startNecklace/startPhoneMic), and liveCaptureSource — when
// ELLA_UPSTREAM_CAPTURE is on (see EllaUpstreamCaptureRegistration.swift for
// the matching Swift flag).
//
// This file is not upstream-owned: it only calls upstream-owned code
// (package:omi/upstream_capture/...) and Ella's own preferences/schema. It
// never edits an upstream-owned file's behavior.
import 'dart:async';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_adapter.dart';
import 'package:omi/upstream_capture/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/devices/connectors/omi_connection.dart';
import 'package:omi/upstream_capture/devices/discovery/device_discoverer.dart';
import 'package:omi/upstream_capture/devices/discovery/native_bluetooth_discoverer.dart';
import 'package:omi/upstream_capture/mic/mic_recorder_interface.dart';

/// Default [EllaUpstreamCaptureAdapter.mayEmitAudio] wiring: the persisted
/// consent flag Ella already falls back to when no active per-session
/// consent lease exists (see `TranscriptSegmentSocketService`'s
/// `_aiConsentLease?.hasCurrentAuthority ?? SharedPreferencesUtil().aiConsentAccepted`
/// in services/sockets/transcription_service.dart). Fails closed: a missing
/// or unset preference reads false, never true.
///
/// KNOWN GAP: this does not yet consult the same per-session
/// `AiConsentActiveSessionLease` the transcription socket uses once a
/// session is live (that lease is scoped to a socket session's lifecycle,
/// not to device connect/reconnect). Upgrading this default to the lease is
/// tracked as follow-up; the seam is injectable specifically so that upgrade
/// does not touch call sites.
bool defaultEllaMayEmitAudio() => SharedPreferencesUtil().aiConsentAccepted;

/// Where a gated audio frame goes next. The codec is fixed by the caller per
/// source (opus for the necklace, pcm16 for the phone) per the /v4/listen
/// contract — this pass does not negotiate it dynamically. Real routing is
/// injected so contract tests use a fake instead of a live socket.
typedef EllaAudioRouter = void Function({required bool isPhone, required List<int> bytes});

class EllaUpstreamCaptureRuntime {
  EllaUpstreamCaptureRuntime({
    required this.adapter,
    required EllaAudioRouter routeAudio,
    IMicRecorderService? micRecorder,
    DeviceDiscoverer? discoverer,
    DeviceConnection? Function(BtDevice device)? connectionFactory,
  })  : _routeAudio = routeAudio,
        _micRecorder = micRecorder,
        _discoverer = discoverer ?? NativeBluetoothDiscoverer(),
        _connectionFactory = connectionFactory ?? DeviceConnectionFactory.create;

  final EllaUpstreamCaptureAdapter adapter;
  final EllaAudioRouter _routeAudio;
  final IMicRecorderService? _micRecorder;
  final DeviceDiscoverer _discoverer;
  final DeviceConnection? Function(BtDevice device) _connectionFactory;

  DeviceConnection? _connection;
  StreamSubscription? _necklaceAudioSubscription;

  UpstreamLiveSource get liveCaptureSource => adapter.liveSource;

  /// Discovers and connects the paired Omi necklace. Manual on first call;
  /// `force: true` is the only way back in after a manual disconnect (rule 2:
  /// no ambient/periodic auto-reconnect — the caller decides when to retry,
  /// upstream's own manual-disconnect/stale-bond rules still apply via
  /// [EllaUpstreamCaptureAdapter.connectBle]).
  Future<bool> ensureConnection({bool force = false}) async {
    if (!adapter.connectBle(force: force)) return false;
    try {
      final result = await _discoverer.discover();
      final device = result.devices.isEmpty ? null : result.devices.first;
      if (device == null) {
        adapter.bleConnected = false;
        return false;
      }
      final connection = _connectionFactory(device);
      if (connection == null) {
        adapter.bleConnected = false;
        return false;
      }
      await connection.connect();
      _connection = connection;
      return true;
    } catch (_) {
      adapter.bleConnected = false;
      return false;
    }
  }

  void disconnect() {
    adapter.manualDisconnect();
    unawaited(_necklaceAudioSubscription?.cancel());
    _necklaceAudioSubscription = null;
    final connection = _connection;
    _connection = null;
    if (connection != null) unawaited(connection.disconnect());
  }

  /// Starts necklace live capture. Every frame re-checks
  /// [EllaUpstreamCaptureAdapter.mayEmitAudio] and the bound uid/generation
  /// before it reaches the socket — BLE may already be connected and
  /// streaming raw bytes off the wire, but nothing is emitted until consent
  /// and account binding both hold on that exact frame.
  Future<bool> startNecklace() async {
    final connection = _connection;
    if (connection is! OmiDeviceConnection) return false;
    if (!adapter.startNecklace()) return false;
    final generation = adapter.generation;
    final uid = adapter.uid;
    final subscription = await connection.performGetBleAudioBytesListener(
      onAudioBytesReceived: (bytes) {
        if (adapter.onDeviceAudio(generation: generation, ownerUid: uid, bytes: bytes)) {
          _routeAudio(isPhone: false, bytes: bytes);
        }
      },
    );
    if (subscription == null) {
      // Rare native failure to subscribe — the adapter had already flipped
      // liveSource to necklace expecting frames to follow; nothing does, so
      // put it back rather than leave the app believing it is live.
      adapter.liveSource = UpstreamLiveSource.none;
      return false;
    }
    _necklaceAudioSubscription = subscription;
    return true;
  }

  /// Starts phone-mic capture. Pendant live capture hands off to the phone —
  /// they are never both live (enforced by the adapter).
  Future<bool> startPhoneMic() async {
    final micRecorder = _micRecorder;
    if (micRecorder == null) return false;
    if (!adapter.startPhoneMic()) return false;
    final generation = adapter.generation;
    final uid = adapter.uid;
    try {
      await micRecorder.start(
        onByteReceived: (bytes) {
          if (adapter.onPhoneAudio(generation: generation, ownerUid: uid, bytes: bytes)) {
            _routeAudio(isPhone: true, bytes: bytes);
          }
        },
      );
      return true;
    } catch (_) {
      adapter.liveSource = UpstreamLiveSource.none;
      return false;
    }
  }

  /// Stops phone-mic capture. After an explicit "finish conversation /
  /// process now" this leaves the adapter free to start a new session
  /// (necklace or phone) — no latch survives a stop, unlike the legacy path's
  /// known regression here.
  void stopPhoneMic({bool resumeNecklace = false}) {
    _micRecorder?.stop();
    adapter.stopPhoneMic(resumeNecklace: resumeNecklace);
  }

  /// Stops necklace live capture without disconnecting BLE — the device
  /// stays connected (upstream's own model), just no longer streaming into
  /// the socket. Safe to call whether or not necklace capture is live.
  void stopNecklace() {
    unawaited(_necklaceAudioSubscription?.cancel());
    _necklaceAudioSubscription = null;
    if (adapter.liveSource == UpstreamLiveSource.necklace) {
      adapter.liveSource = UpstreamLiveSource.none;
    }
  }
}
