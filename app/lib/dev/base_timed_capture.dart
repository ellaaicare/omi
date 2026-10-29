import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:clock/clock.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';

import 'package:omi/app_globals.dart';
import 'package:omi/dev/dev_mic_token.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/utils/logger.dart';

export 'package:omi/dev/dev_mic_token.dart' show kBaseDevTimedCapture;

/// DEV-ONLY Ella Base timed capture test (Blue Heron hardware proof).
///
/// The operator arms one bounded phone-mic recording while iPhone Mirroring is open, closes Mirroring, and
/// a system trigger later runs the dev AudioRecordingIntent (SiriIntents.swift), which calls
/// [runFromSystemTrigger]. iOS gives a suspended app no Dart timers and lets the mic start from the
/// background only inside an AudioRecordingIntent, so the delayed start is that trigger.
///
/// Exact ownership (v4): the trigger obtains a native token grant (bounded TTL), then starts the recording
/// through CaptureController.devStreamRecordingOwned(token). The coordinator refuses that start if any capture is
/// active (never preempts), binds the token to exactly the capture session it creates, and tags only that
/// session's own native starts (first start and its stall restarts, see DevMicToken). Ordinary starts are never
/// tagged. The timed stop is devStopOwned(token), which the coordinator applies only to that same session.
/// PhoneMicController refuses activation of a revoked/expired token at every engine activation step and cuts off
/// only sessions carrying exactly the token. The token is revoked on every terminal path; native revokes it at
/// grant expiry even if Dart is gone.
///
/// Proof of closure is only the native recorder reporting no live session for the token. Anything else
/// escalates to the native token cutoff, then [TimedCapturePhase.hold], which blocks re-arming until the token's
/// session is verified gone and the capture controller no longer holds the owned session.
///
/// Off unless built with --dart-define=BASE_DEV_TIMED_CAPTURE=true AND Swift condition
/// BASE_DEV_TIMED_CAPTURE. Nothing records while armed. No remote trigger, permission request or consent
/// path. Strings are hardcoded on purpose: this surface is never in a release build.
const String kBaseTimedCaptureChannel = 'com.ella.base/dev_timed_capture';

enum TimedCapturePhase { idle, arming, armed, starting, recording, stopping, done, failed, hold, cancelled }

/// Everything the test touches outside itself. Production wiring reuses the CaptureProvider start/stop
/// the Home Record button and Siri use, plus the dev native channel for token grants and mic truth.
class TimedCapturePorts {
  const TimedCapturePorts({
    required this.start,
    required this.stop,
    required this.ownsCurrent,
    required this.busy,
    required this.micGranted,
    required this.speechReadiness,
    required this.uid,
    required this.newToken,
    required this.grantToken,
    required this.revokeToken,
    required this.micOpen,
    required this.nativeCutoff,
    required this.reportTerminal,
  });

  /// Starts a capture session owned by [token] (refused, never preempting, if any capture is active).
  final Future<void> Function(int token) start;

  /// Stops only the session [token] started; false when there is none (nothing else is ever stopped).
  final Future<bool> Function(int token) stop;

  /// Whether the capture controller's current session is the one [token] started.
  final bool Function(int token) ownsCurrent;

  /// Any capture already live or paused (phone or pendant), or a connected device.
  final bool Function() busy;

  /// Status only. The test never requests a permission.
  final Future<bool> Function() micGranted;

  /// 'ready' or a reason. Read-only readiness probe, never prepareSpeech.
  final Future<String> Function() speechReadiness;
  final String? Function() uid;

  /// A fresh 30-bit token (1..2^30-1).
  final int Function() newToken;

  /// Native grant for [token], valid for [ttl]. False if another grant is live or the token was used.
  final Future<bool> Function(int token, Duration ttl) grantToken;
  final Future<void> Function(int token) revokeToken;

  /// Native truth for the token: true a live session carries it, false none does, null unknown.
  final Future<bool?> Function(int token) micOpen;

  /// Native token cutoff: 'idle' | 'not_owned' | 'closed' | 'unverified'.
  final Future<String> Function(int token) nativeCutoff;
  final Future<void> Function(String status) reportTerminal;
}

class TimedCaptureConfig {
  const TimedCaptureConfig({
    this.notBefore = const Duration(seconds: 45),
    this.window = const Duration(minutes: 10),
    this.recordFor = const Duration(seconds: 20),
    this.startBound = const Duration(seconds: 15),
    this.stopBound = const Duration(seconds: 10),
    this.verifyBound = const Duration(seconds: 5),
    this.stopRetryAfter = const Duration(seconds: 2),
    this.lateStartGrace = const Duration(seconds: 60),
  });

  /// Earliest start after arming: time to quit Mirroring and leave the phone.
  final Duration notBefore;

  /// The arm expires this long after arming, used or not.
  final Duration window;
  final Duration recordFor;
  final Duration startBound;
  final Duration stopBound;
  final Duration verifyBound;
  final Duration stopRetryAfter;

  /// After a start timeout, re-arming stays blocked this long in case the start completes late.
  final Duration lateStartGrace;

  static const Duration minRecord = Duration(seconds: 5);
  static const Duration maxRecord = Duration(seconds: 60);
  static const Duration maxWindow = Duration(minutes: 30);

  Duration get boundedRecordFor => recordFor < minRecord ? minRecord : (recordFor > maxRecord ? maxRecord : recordFor);
  Duration get boundedWindow => window > maxWindow ? maxWindow : window;

  /// Native grant TTL: the whole bounded life of one recording.
  Duration get grantTtl => startBound + boundedRecordFor + stopBound;
}

/// Append-only raw evidence. Never rewritten or deleted by the test.
abstract class TimedCaptureEvidence {
  Future<void> record(Map<String, Object?> event);
}

class FileTimedCaptureEvidence implements TimedCaptureEvidence {
  @override
  Future<void> record(Map<String, Object?> event) async {
    try {
      final dir = await getApplicationDocumentsDirectory();
      await File('${dir.path}/base_timed_capture_evidence.jsonl')
          .writeAsString('${jsonEncode(event)}\n', mode: FileMode.append, flush: true);
    } catch (e) {
      Logger.debug('timed capture evidence write failed: $e');
    }
  }
}

class _Arm {
  _Arm(this.id, this.uid, this.notBefore, this.expiresAt, this.recordFor);
  final String id;
  final String uid;
  final DateTime notBefore;
  final DateTime expiresAt;
  final Duration recordFor;
}

class _Bounded<T> {
  _Bounded.value(this.value)
      : error = null,
        timedOut = false;
  _Bounded.error(this.error)
      : value = null,
        timedOut = false;
  _Bounded.timeout()
      : value = null,
        error = null,
        timedOut = true;
  final T? value;
  final Object? error;
  final bool timedOut;
  bool get ok => !timedOut && error == null;
}

Future<_Bounded<T>> _bounded<T>(Future<T> Function() call, Duration limit) async {
  try {
    return _Bounded.value(await call().timeout(limit));
  } on TimeoutException {
    return _Bounded.timeout();
  } catch (e) {
    return _Bounded.error(e);
  }
}

class BaseTimedCapture extends ChangeNotifier {
  BaseTimedCapture({
    required TimedCapturePorts ports,
    required TimedCaptureEvidence evidence,
    this.config = const TimedCaptureConfig(),
    bool enabled = kBaseDevTimedCapture,
  })  : _ports = ports,
        _evidence = evidence,
        _enabled = enabled;

  static BaseTimedCapture? _instance;

  /// The app-wide instance, or null in every build without the dev define.
  static BaseTimedCapture? get instance {
    if (!kBaseDevTimedCapture) return null;
    return _instance ??= BaseTimedCapture(ports: appPorts(), evidence: FileTimedCaptureEvidence());
  }

  final TimedCapturePorts _ports;
  final TimedCaptureEvidence _evidence;
  final TimedCaptureConfig config;
  final bool _enabled;

  TimedCapturePhase _phase = TimedCapturePhase.idle;
  String? _lastReason;
  _Arm? _arm;
  _Arm? _live;

  /// The token of the live recording, or of an unresolved HOLD.
  int? _token;
  DateTime? _stopAt;
  Timer? _expiryTimer;
  Timer? _stopTimer;
  Timer? _hardTimer;
  Timer? _lateStartTimer;
  bool _lateStartPending = false;
  bool _stopInFlight = false;
  bool _disposed = false;

  /// Ownership generation. Every arm, cancel and trigger claim takes a new one synchronously, before
  /// any await; an async step continues only while it still owns the current generation.
  int _gen = 0;

  TimedCapturePhase get phase => _phase;
  String? get lastReason => _lastReason;
  DateTime? get notBefore => _arm?.notBefore;
  DateTime? get expiresAt => _arm?.expiresAt;
  DateTime? get stopAt => _stopAt;
  bool get lateStartPending => _lateStartPending;

  static const Set<TimedCapturePhase> _livePhases = {
    TimedCapturePhase.starting,
    TimedCapturePhase.recording,
    TimedCapturePhase.stopping,
  };

  Future<void> _log(String event, [Map<String, Object?> detail = const {}]) => _evidence.record({
        't': clock.now().toUtc().toIso8601String(),
        'event': event,
        'side': 'dart',
        'gen': _gen,
        'phase': _phase.name,
        if (_token != null) 'token': _token,
        ...detail,
      });

  void _set(TimedCapturePhase phase, [String? reason]) {
    _phase = phase;
    _lastReason = reason;
    if (!_disposed) notifyListeners();
  }

  // ---------------------------------------------------------------- arming

  /// Arms one recording. Records nothing. Returns null on success, else the refusal reason.
  Future<String?> arm() async {
    if (!_enabled) return 'disabled';
    if (_phase == TimedCapturePhase.arming || _phase == TimedCapturePhase.armed || _livePhases.contains(_phase)) {
      return 'already_${_phase.name}';
    }
    if (_phase == TimedCapturePhase.hold) return 'hold_unresolved';
    if (_lateStartPending) return 'late_start_pending';
    final uid = _ports.uid();
    if (uid == null || uid.isEmpty) return _refuseArm(null, 'signed_out');
    final gen = ++_gen;
    _set(TimedCapturePhase.arming);

    /// Rechecked after every await: a cancel/new generation wins; an account switch disarms here even
    /// if no auth event arrived.
    Future<String?> lost() async {
      if (gen != _gen || _phase != TimedCapturePhase.arming) return 'superseded';
      if (_ports.uid() != uid) return _refuseArm(gen, 'account_changed');
      return null;
    }

    if (_ports.busy()) return _refuseArm(gen, 'capture_busy');
    final mic = await _bounded(_ports.micGranted, config.verifyBound);
    if (await lost() case final r?) return r;
    if (mic.value != true) return _refuseArm(gen, 'mic_not_granted');
    final speech = await _bounded(_ports.speechReadiness, config.verifyBound);
    if (await lost() case final r?) return r;
    if (speech.value != 'ready') return _refuseArm(gen, 'speech_${speech.value ?? 'unknown'}');
    if (_ports.busy()) return _refuseArm(gen, 'capture_busy');

    final now = clock.now();
    _arm = _Arm('arm-${now.toUtc().millisecondsSinceEpoch}-$gen', uid, now.add(config.notBefore),
        now.add(config.boundedWindow), config.boundedRecordFor);
    _stopAt = null;
    _expiryTimer?.cancel();
    _expiryTimer = Timer(config.boundedWindow, () => unawaited(cancel('expired')));
    _set(TimedCapturePhase.armed);
    await _log('armed', {
      'arm': _arm!.id,
      'not_before': _arm!.notBefore.toUtc().toIso8601String(),
      'expires': _arm!.expiresAt.toUtc().toIso8601String(),
      'record_s': _arm!.recordFor.inSeconds,
    });
    return null;
  }

  Future<String> _refuseArm(int? gen, String reason) async {
    // A refusal only lands if this attempt still owns the generation (a cancel may have superseded it).
    if (gen == null || gen == _gen) _set(TimedCapturePhase.idle, reason);
    await _log('arm_refused', {'reason': reason});
    return reason;
  }

  /// Disarms (arming or armed). Never touches a live recording: that is the timed stop's job.
  Future<void> cancel(String reason) async {
    if (_phase != TimedCapturePhase.arming && _phase != TimedCapturePhase.armed) return;
    ++_gen;
    _expiryTimer?.cancel();
    _arm = null;
    _set(TimedCapturePhase.cancelled, reason);
    await _log('cancelled', {'reason': reason});
  }

  /// Account changes disarm; during a live recording they revoke the token and stop now.
  Future<void> onAuthChanged(String? uid) async {
    if (_phase == TimedCapturePhase.arming || _phase == TimedCapturePhase.armed) {
      if (uid != _arm?.uid) await cancel('account_changed');
      return;
    }
    if ((_phase == TimedCapturePhase.starting || _phase == TimedCapturePhase.recording) && uid != _live?.uid) {
      await _stopAndVerify(_gen, 'account_changed', revokeFirst: true);
    }
  }

  // --------------------------------------------------------------- trigger

  /// Called by the dev AudioRecordingIntent. Returns 'recording:<seconds>' or a refusal/failure reason.
  Future<String> runFromSystemTrigger() async {
    if (!_enabled) return 'disabled';
    final arm = _arm;
    if (_phase != TimedCapturePhase.armed || arm == null) {
      await _log('trigger_refused', {'reason': 'not_armed'});
      return 'not_armed';
    }
    final now = clock.now();
    if (now.isBefore(arm.notBefore)) {
      // Stays armed: an early trigger does not burn the one shot.
      await _log('trigger_refused', {'reason': 'too_early'});
      return 'too_early';
    }
    String? refusal;
    if (!now.isBefore(arm.expiresAt)) {
      refusal = 'expired';
    } else if (_ports.uid() != arm.uid) {
      refusal = 'account_changed';
    } else if (_ports.busy()) {
      refusal = 'capture_busy';
    }
    if (refusal != null) {
      await cancel(refusal);
      return refusal;
    }

    // Claim synchronously: one shot, single flight. A concurrent trigger now sees not_armed.
    final gen = ++_gen;
    _expiryTimer?.cancel();
    _arm = null;
    _live = arm;
    _set(TimedCapturePhase.starting);
    await _log('trigger_accepted', {'arm': arm.id});

    bool still() => gen == _gen && _phase == TimedCapturePhase.starting;
    String? gate(String? extra) {
      if (!still()) return 'superseded';
      if (_ports.uid() != arm.uid) return 'account_changed';
      if (!clock.now().isBefore(arm.expiresAt)) return 'expired';
      if (_ports.busy()) return 'capture_busy';
      return extra;
    }

    final mic = await _bounded(_ports.micGranted, config.verifyBound);
    final early = gate(mic.value == true ? null : 'mic_not_granted');
    if (early != null) return _refuseClaim(gen, early);

    // Exact ownership: one native grant for this recording's whole bounded life.
    final token = _ports.newToken();
    final ttl = arm.recordFor + config.startBound + config.stopBound;
    final grant = await _bounded(() => _ports.grantToken(token, ttl), config.verifyBound);
    final afterGrant = gate(grant.ok && grant.value == true ? null : 'grant_refused');
    if (afterGrant != null) {
      // The token may have been granted even if the answer was lost: revoke it either way.
      await _bounded(() => _ports.revokeToken(token), config.verifyBound);
      return _refuseClaim(gen, afterGrant);
    }
    _token = token;
    await _log('token_granted', {'ttl_s': ttl.inSeconds});

    // Hard deadline armed BEFORE the mic opens, so a hung start or lost stop timer is still bounded.
    _hardTimer?.cancel();
    _hardTimer = Timer(ttl, () => unawaited(_stopAndVerify(gen, 'hard_deadline', revokeFirst: true)));

    final startCall = _ports.start(token);
    final started = await _bounded(() => startCall, config.startBound);
    if (!started.ok) {
      final why = started.timedOut ? 'start_timeout' : 'start_failed';
      await _log(why, {'error': started.error == null ? null : _describe(started.error!)});
      if (started.timedOut) _watchLateStart(startCall, token);
      // Revoke first: native refuses any late activation of this token from here on.
      await _stopAndVerify(gen, why, revokeFirst: true);
      return why;
    }
    if (!still()) {
      // Aborted (account change, dispose) while the start was in flight, and the mic opened anyway.
      await _log('start_completed_after_abort');
      await _closeLateMic(token, 'start_completed_after_abort');
      return 'superseded';
    }
    _set(TimedCapturePhase.recording);
    _stopAt = clock.now().add(arm.recordFor);
    _stopTimer = Timer(arm.recordFor, () => unawaited(_stopAndVerify(gen, 'timed')));
    await _log('recording', {'stop_at': _stopAt!.toUtc().toIso8601String()});
    return 'recording:${arm.recordFor.inSeconds}';
  }

  /// Refusal after the claim: the mic was never asked to open, and the shot stays consumed.
  Future<String> _refuseClaim(int gen, String reason) async {
    if (gen == _gen && _phase == TimedCapturePhase.starting) {
      _live = null;
      _set(TimedCapturePhase.cancelled, reason);
      await _report('cancelled');
    }
    await _log('trigger_gate_refused', {'reason': reason});
    return reason;
  }

  // ---------------------------------------------------------- stop/verify

  /// Stops and proves the token's session gone. Runs at most once per live recording.
  Future<void> _stopAndVerify(int gen, String why, {bool revokeFirst = false}) async {
    if (gen != _gen || _stopInFlight || !_livePhases.contains(_phase)) return;
    final token = _token;
    if (token == null) return;
    _stopInFlight = true;
    _stopTimer?.cancel();
    _hardTimer?.cancel();
    _set(TimedCapturePhase.stopping, why);
    await _log('stop_begin', {'why': why});
    if (revokeFirst) await _revoke(token);

    bool? stopResult;
    for (var attempt = 1; attempt <= 2; attempt++) {
      final r = await _bounded(() => _ports.stop(token), config.stopBound);
      await _log(r.timedOut ? 'stop_timeout' : (r.ok ? 'stop_returned' : 'stop_error'), {
        'attempt': attempt,
        if (r.ok) 'result': r.value,
        if (r.error != null) 'error': _describe(r.error!),
      });
      if (r.ok) {
        stopResult = r.value;
        break;
      }
      if (attempt == 1) await Future<void>.delayed(config.stopRetryAfter);
    }
    await _revoke(token);
    final closed = await _verifyClosedOrCutoff(token, why);
    _stopInFlight = false;
    _live = null;
    if (closed) {
      final terminal = why == 'timed' ? TimedCapturePhase.done : TimedCapturePhase.failed;
      final note = why == 'timed' && stopResult != true ? 'stop_returned_${stopResult}_mic_verified_closed' : null;
      _set(terminal, terminal == TimedCapturePhase.done ? note : why);
      await _log(terminal.name, {'why': why, 'mic': 'verified_closed', if (note != null) 'note': note});
      await _report(terminal.name);
      _token = null;
    } else {
      await _enterHold(why);
    }
  }

  Future<void> _revoke(int token) async {
    final r = await _bounded(() => _ports.revokeToken(token), config.verifyBound);
    if (!r.ok) await _log('revoke_unconfirmed', {'token': token});
  }

  /// True only when native reports no live session carrying [token], directly or after the token cutoff.
  Future<bool> _verifyClosedOrCutoff(int token, String why) async {
    final open = await _bounded(() => _ports.micOpen(token), config.verifyBound);
    await _log('mic_check', {'why': why, 'open': open.ok ? open.value : 'unknown'});
    if (open.ok && open.value == false) return true;
    final cut = await _bounded(() => _ports.nativeCutoff(token), config.stopBound + config.verifyBound);
    await _log('native_cutoff', {'why': why, 'result': cut.ok ? cut.value : (cut.timedOut ? 'timeout' : 'error')});
    final after = await _bounded(() => _ports.micOpen(token), config.verifyBound);
    await _log('mic_check_after_cutoff', {'open': after.ok ? after.value : 'unknown'});
    return after.ok && after.value == false;
  }

  Future<void> _enterHold(String why) async {
    _set(TimedCapturePhase.hold, why);
    await _log('UNVERIFIED_HOLD', {'why': why});
    await _report('hold');
  }

  /// A start that timed out may still complete; native refuses activation of its revoked token, but if the
  /// mic did open first it is closed and verified like any other stop.
  void _watchLateStart(Future<void> startCall, int token) {
    _lateStartPending = true;
    _lateStartTimer?.cancel();
    _lateStartTimer = Timer(config.lateStartGrace, () {
      if (!_lateStartPending) return;
      _lateStartPending = false;
      unawaited(_log('late_start_grace_elapsed'));
      if (!_disposed) notifyListeners();
    });
    startCall.then((_) async {
      if (!_lateStartPending) return;
      _lateStartPending = false;
      _lateStartTimer?.cancel();
      await _log('late_start_completed', {'token': token});
      await _closeLateMic(token, 'late_start_completed');
    }, onError: (Object e) async {
      _lateStartPending = false;
      _lateStartTimer?.cancel();
      await _log('late_start_failed', {'error': _describe(e)});
    });
  }

  /// The token's session may exist outside a live phase: revoke, stop, verify, escalate, else HOLD.
  Future<void> _closeLateMic(int token, String why) async {
    await _revoke(token);
    await _bounded(() => _ports.stop(token), config.stopBound);
    if (!await _verifyClosedOrCutoff(token, why)) {
      _token = token;
      await _enterHold(why);
    }
  }

  Future<void> _report(String status) async {
    final r = await _bounded(() => _ports.reportTerminal(status), config.verifyBound);
    if (!r.ok) await _log('report_terminal_failed', {'status': status});
  }

  /// Leaves HOLD only when native reports no live session for the held token and the capture controller no
  /// longer holds the session that token started.
  Future<String> resolveHold() async {
    if (_phase != TimedCapturePhase.hold) return 'nothing_to_resolve';
    if (_lateStartPending) return 'busy';
    final token = _token;
    if (token != null) {
      final open = await _bounded(() => _ports.micOpen(token), config.verifyBound);
      if (!open.ok || open.value != false) return 'mic_not_verified_idle';
      if (_ports.ownsCurrent(token)) return 'capture_not_idle';
    }
    _token = null;
    _set(TimedCapturePhase.idle, 'hold_resolved');
    await _log('hold_resolved');
    return 'resolved';
  }

  static String _describe(Object e) => e is PlatformException ? '${e.code}: ${e.message}' : e.runtimeType.toString();

  @override
  void dispose() {
    // Disposing never proves closure: a live recording's token is revoked, it gets a best-effort stop and
    // a native token cutoff, and the phase is HOLD. Native grant expiry stays the independent guarantee.
    final token = _token;
    if (_livePhases.contains(_phase) && token != null) {
      unawaited(_log('disposed_while_live'));
      unawaited(_bounded(() => _ports.revokeToken(token), config.verifyBound));
      unawaited(_bounded(() => _ports.stop(token), config.stopBound));
      unawaited(_bounded(() => _ports.nativeCutoff(token), config.stopBound + config.verifyBound));
      ++_gen;
      _phase = TimedCapturePhase.hold;
      _lastReason = 'disposed_while_live';
      unawaited(_report('hold'));
    }
    _disposed = true;
    _expiryTimer?.cancel();
    _stopTimer?.cancel();
    _hardTimer?.cancel();
    _lateStartTimer?.cancel();
    super.dispose();
  }

  // ------------------------------------------------------------ app wiring

  static const MethodChannel _native = MethodChannel(kBaseTimedCaptureChannel);

  /// Production ports: the CaptureProvider path the Home Record button and Siri use.
  static TimedCapturePorts appPorts() {
    T read<T>() {
      final context = globalNavigatorKey.currentContext;
      if (context == null) throw StateError('Omi is not ready');
      return context.read<T>();
    }

    const speech = MethodChannel('com.omi.ios/speech');
    final random = Random.secure();
    return TimedCapturePorts(
      start: (token) => read<CaptureProvider>().devStreamRecordingOwned(token),
      stop: (token) => read<CaptureProvider>().devStopOwned(token),
      ownsCurrent: (token) => read<CaptureProvider>().devOwnsCurrentSession(token),
      busy: () {
        final capture = read<CaptureProvider>();
        final device = read<DeviceProvider>();
        return capture.liveCaptureSource != null ||
            capture.isPhoneMicBatchRecording ||
            capture.isPendantBatchRecording ||
            device.isConnected;
      },
      micGranted: () async => (await Permission.microphone.status).isGranted,
      speechReadiness: () async {
        final wire = await speech.invokeMethod<Object?>('readiness', {'language': 'en-US'});
        if (wire is Map && wire['status'] is String) return wire['status'] as String;
        return 'unknown';
      },
      uid: () => FirebaseAuth.instance.currentUser?.uid,
      newToken: () => 1 + random.nextInt(DevMicToken.tokenMask - 1),
      grantToken: (token, ttl) async =>
          await _native.invokeMethod<bool>('grantToken', {'token': token, 'ttl_ms': ttl.inMilliseconds}) ?? false,
      revokeToken: (token) => _native.invokeMethod<void>('revokeToken', {'token': token}),
      micOpen: (token) => _native.invokeMethod<bool>('micOpen', {'token': token}),
      nativeCutoff: (token) async => await _native.invokeMethod<String>('cutoff', {'token': token}) ?? 'unverified',
      reportTerminal: (status) => _native.invokeMethod<void>('terminal', {'status': status}),
    );
  }

  /// Native side: the dev AudioRecordingIntent invokes 'run' on this channel.
  void attachChannel() {
    if (!_enabled) return;
    _native.setMethodCallHandler((call) async {
      if (call.method == 'run') return runFromSystemTrigger();
      throw MissingPluginException(call.method);
    });
    FirebaseAuth.instance.authStateChanges().listen((user) => unawaited(onAuthChanged(user?.uid)));
  }
}
