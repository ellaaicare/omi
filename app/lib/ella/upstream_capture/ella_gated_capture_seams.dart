import 'dart:async';
import 'dart:typed_data';

import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/geolocation.dart';
import 'package:omi/upstream_capture/models/custom_stt_config.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';

// Ella adapters over the UPSTREAM capture seams (ellaaicare/ella-ai#1280).
//
// Upstream's CaptureController (lib/upstream_capture/services/capture/) accepts
// its external boundaries through constructor seams (capture_seams.dart,
// CaptureDependencies in capture_composition.dart). Ella composes the vendored
// controller with the decorators below instead of editing any upstream file:
//
//   * phone mic  -> [EllaGatedMicRecorderService]   (IMicRecorderService seam)
//   * necklace   -> [EllaGatedDeviceConnection]     (deviceConnectionLoader seam)
//   * socket     -> [EllaGatedTranscriptSocket]     (openConversationSocket seam)
//
// Each one asks [EllaCaptureAuthority.admitsFrame] (== mayEmitAudio + bound
// account check) for EVERY audio frame, so a frame is dropped the instant the
// consent lease stops being current, changes generation, or the account
// switches. The source-level gates stop frames before upstream's WAL copy and
// socket send; the socket gate is the last line before bytes leave the device.

/// Decorates the socket opener: refuses to open without current authority and
/// wraps every opened socket in [EllaGatedTranscriptSocket].
CaptureConversationSocketOpen ellaGatedConversationSocketOpen(
  CaptureConversationSocketOpen inner,
  EllaCaptureAuthority authority,
) {
  return ({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    CustomSttConfig? customSttConfig,
    Geolocation? geolocation,
  }) async {
    if (!authority.hasCurrentAuthority) return null;
    final socket = await inner(
      codec: codec,
      sampleRate: sampleRate,
      language: language,
      force: force,
      source: source,
      clientConversationId: clientConversationId,
      customSttConfig: customSttConfig,
      geolocation: geolocation,
    );
    if (socket == null) return null;
    if (!authority.hasCurrentAuthority) {
      await socket.stop(reason: 'ella_consent_authority_not_current');
      return null;
    }
    return EllaGatedTranscriptSocket(socket, authority);
  };
}

/// [TranscriptSegmentSocketService] decorator whose [send] drops binary audio
/// frames unless [EllaCaptureAuthority.admitsFrame] admits each one. Control
/// text frames and every other member forward unchanged to the upstream socket.
class EllaGatedTranscriptSocket implements TranscriptSegmentSocketService {
  EllaGatedTranscriptSocket(this.inner, this._authority);

  final TranscriptSegmentSocketService inner;
  final EllaCaptureAuthority _authority;
  int _droppedAudioFrames = 0;

  int get droppedAudioFrames => _droppedAudioFrames;

  @override
  Future send(dynamic message) async {
    if (message is List<int> && !_authority.admitsFrame()) {
      _droppedAudioFrames++;
      return;
    }
    return inner.send(message);
  }

  @override
  Future sendText(String message) => inner.sendText(message);

  @override
  Future requestFirstOnboardingQuestion() => inner.requestFirstOnboardingQuestion();

  @override
  IPureSocket get socket => inner.socket;

  @override
  SocketServiceState get state => inner.state;

  @override
  int get binaryAudioBytesSent => inner.binaryAudioBytesSent;

  @override
  bool get stoppedIntentionally => inner.stoppedIntentionally;

  @override
  void subscribe(Object context, ITransctiptSegmentSocketServiceListener listener) =>
      inner.subscribe(context, listener);

  @override
  void unsubscribe(Object context) => inner.unsubscribe(context);

  @override
  Future start() => inner.start();

  @override
  Future stop({String? reason}) => inner.stop(reason: reason);

  @override
  void onClosed([int? closeCode]) => inner.onClosed(closeCode);

  @override
  void onConnected() => inner.onConnected();

  @override
  void onError(Object err, StackTrace trace) => inner.onError(err, trace);

  @override
  void onMessage(dynamic event) => inner.onMessage(event);

  @override
  int get sampleRate => inner.sampleRate;

  @override
  set sampleRate(int value) => inner.sampleRate = value;

  @override
  BleAudioCodec get codec => inner.codec;

  @override
  set codec(BleAudioCodec value) => inner.codec = value;

  @override
  String get language => inner.language;

  @override
  set language(String value) => inner.language = value;

  @override
  bool get includeSpeechProfile => inner.includeSpeechProfile;

  @override
  set includeSpeechProfile(bool value) => inner.includeSpeechProfile = value;

  @override
  String? get source => inner.source;

  @override
  set source(String? value) => inner.source = value;

  @override
  bool get customSttMode => inner.customSttMode;

  @override
  set customSttMode(bool value) => inner.customSttMode = value;

  @override
  String? get sttConfigId => inner.sttConfigId;

  @override
  set sttConfigId(String? value) => inner.sttConfigId = value;

  @override
  String? get clientConversationId => inner.clientConversationId;

  @override
  set clientConversationId(String? value) => inner.clientConversationId = value;

  @override
  bool get onboardingMode => inner.onboardingMode;

  @override
  set onboardingMode(bool value) => inner.onboardingMode = value;

  @override
  bool get speechProfileRedo => inner.speechProfileRedo;

  @override
  set speechProfileRedo(bool value) => inner.speechProfileRedo = value;

  @override
  Geolocation? get geolocation => inner.geolocation;

  @override
  set geolocation(Geolocation? value) => inner.geolocation = value;
}

/// The production phone-mic [IMicRecorderService] for the flag-ON graph:
/// upstream's own native recorder (NativeMicRecorderService over the vendored
/// PhoneMic Pigeon host, arbitrated by upstream's MicArbiter via
/// ServiceManager.phoneMic) decorated with the Ella per-frame gate.
///
/// * start()/startBatch() refuse (throw) without current authority, so the
///   upstream controller reports a visible start failure instead of recording.
/// * Every PCM frame from native is checked before upstream's callback sees it,
///   so neither upstream's WAL copy nor its socket receives a refused frame.
/// * Batch mode writes files natively (no Dart frames); it is only admitted at
///   start, and the runtime mutes upstream's CapturePolicy on revocation so the
///   native writer's own admission latch drops subsequent packets.
class EllaGatedMicRecorderService implements IMicRecorderService {
  EllaGatedMicRecorderService(this.inner, this._authority);

  final IMicRecorderService inner;
  final EllaCaptureAuthority _authority;
  int _droppedFrames = 0;

  int get droppedFrames => _droppedFrames;

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    Function(bool began)? onInterruption,
  }) {
    if (!_authority.hasCurrentAuthority) {
      throw StateError('ella_consent_authority_not_current');
    }
    return inner.start(
      onByteReceived: (bytes) {
        if (!_authority.admitsFrame()) {
          _droppedFrames++;
          return;
        }
        onByteReceived(bytes);
      },
      onRecording: onRecording,
      onStop: onStop,
      onInitializing: onInitializing,
      onStalled: onStalled,
      onInterruption: onInterruption,
    );
  }

  @override
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  }) {
    if (!_authority.hasCurrentAuthority) {
      throw StateError('ella_consent_authority_not_current');
    }
    return inner.startBatch(
      onStop: onStop,
      onInterruption: onInterruption,
      onBatchStalled: onBatchStalled,
      onError: onError,
    );
  }

  @override
  void stop() => inner.stop();

  @override
  void probeStallAfterForeground() => inner.probeStallAfterForeground();
}
