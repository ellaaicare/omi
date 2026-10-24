import 'package:omi/ella/services/app_output_admission.dart';
import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_startup_diagnostics.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

/// Explicit candidate opt-in. Installed921 and ordinary builds keep their existing mode API.
/// No probing, negotiation or legacy fallback is performed by this strict client.
const whisperDeliveryProtocol1 = bool.fromEnvironment('ELLA_WHISPER_DELIVERY_PROTOCOL1');

/// Decode the existing protocol error hint; the actor validates closure separately.
Map<String, dynamic> decodeWhisperDeliveryTransportResponse(
    String method, String endpoint, int statusCode, String body) {
  Object? decoded;
  try {
    decoded = jsonDecode(body);
  } on FormatException {
    throw WhisperDeliveryFailure();
  }
  final detail = decoded is Map<String, dynamic> ? decoded['detail'] : null;
  if (method == 'POST' &&
      endpoint == 'stop' &&
      statusCode == 409 &&
      detail is Map<String, dynamic> &&
      detail.length == 1 &&
      detail['code'] == 'delivery_stop_request_stale') {
    throw WhisperDeliveryStopRequestStale();
  }
  if (statusCode != 200 || decoded is! Map<String, dynamic>) throw WhisperDeliveryFailure();
  return decoded;
}

class WhisperDeliveryRuntime extends ChangeNotifier implements WhisperDeliveryMedia {
  WhisperDeliveryRuntime._()
      : this._create(_transportDefault, _readDefault, _writeDefault, WalOwnerAuthority.active,
            const MethodChannel('com.ellaaicare.ella/guardian_mode'), true);

  @visibleForTesting
  WhisperDeliveryRuntime.testing(
      {required DeliveryTransport transport,
      required DeliveryStoreRead read,
      required DeliveryStoreWrite write,
      required ExactAccountAuthorityVerifier? Function() authorityProvider,
      required MethodChannel channel,
      FirebaseAuth? authentication})
      : this._create(transport, read, write, authorityProvider, channel, false, authentication: authentication);

  WhisperDeliveryRuntime._create(DeliveryTransport transport, DeliveryStoreRead read, DeliveryStoreWrite write,
      this._authorityProvider, this._channel, bool observeAuthority,
      {FirebaseAuth? authentication})
      : _authentication = authentication {
    client = WhisperDeliveryClient(
        transport: transport,
        media: this,
        read: read,
        write: write,
        authorityProvider: _authorityProvider,
        onChanged: _notify,
        lifetimeIdentity: (_) => _authenticationLifetime);
    if (observeAuthority) SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(_authorityChanged);
    _channel.setMethodCallHandler(_nativeFact);
  }
  static final shared = WhisperDeliveryRuntime._();
  final MethodChannel _channel;
  final FirebaseAuth? _authentication;
  FirebaseAuth get _auth => _authentication ?? FirebaseAuth.instance;
  final ExactAccountAuthorityVerifier? Function() _authorityProvider;
  int _nativeOperation = 0;
  late final WhisperDeliveryClient client;
  WhisperDeliveryScope? _nativeScope;
  Timer? _timer;
  int _pollSchedule = 0;
  int _permissionHolds = 0;
  String _authenticationLifetime = "";
  bool failed = false;
  AppOutputPermit? _outputPermit;
  bool _playing = false;
  bool get playing => _playing && client.scope != null;
  String? _message;
  WhisperDeliveryScope? _messageScope;
  int? _messageGeneration;
  String? get message => client.scope == _messageScope && client.generation == _messageGeneration ? _message : null;
  WhisperPresentation preferredPresentation = WhisperPresentation.audio;
  void _notify() {
    if (!client.ready) _playing = false;
    notifyListeners();
  }

  void _authorityChanged() {
    _cancelPolling();
    _message = null;
    _playing = false;
    unawaited(client.retireAuthority().catchError((_) {}));
  }

  static String _key(String uid) => 'ella.whisper.delivery1.${sha256.convert(utf8.encode(uid))}';
  static Future<Map<String, dynamic>?> _readDefault(String uid) async {
    final raw = SharedPreferencesUtil().getString(_key(uid));
    if (raw.isEmpty) return null;
    return jsonDecode(raw) as Map<String, dynamic>;
  }

  static Future<void> _writeDefault(String uid, Map<String, dynamic> state) async {
    if (!await SharedPreferencesUtil().saveString(_key(uid), jsonEncode(state))) throw WhisperDeliveryFailure();
  }

  static Future<Map<String, dynamic>> _transportDefault(
      String method, String endpoint, Map<String, dynamic> fields, ExactAccountAuthorityVerifier authority) async {
    final phase = switch (endpoint) {
      'devices' => WhisperStartupStage.deviceApi,
      'sessions' => WhisperStartupStage.sessionApi,
      'stop' => WhisperStartupStage.pendingStop,
      _ => null,
    };
    if (!whisperDeliveryProtocol1 || !authority.isExactCurrent()) {
      if (phase != null)
        WhisperStartupDiagnostics.record(phase, WhisperStartupResult.failed,
            code:
                !whisperDeliveryProtocol1 ? WhisperStartupCode.protocolDisabled : WhisperStartupCode.authorityChanged);
      throw WhisperDeliveryFailure();
    }
    final base = Env.apiBaseUrl;
    if (base == null || base.isEmpty) {
      if (phase != null)
        WhisperStartupDiagnostics.record(phase, WhisperStartupResult.failed, code: WhisperStartupCode.apiUnconfigured);
      throw WhisperDeliveryFailure();
    }
    var uri = Uri.parse('${base}v1/ella/guardian/delivery/$endpoint');
    if (method == 'GET') uri = uri.replace(queryParameters: fields.map((k, v) => MapEntry(k, '$v')));
    final response = await makeApiCall(
        url: uri.toString(),
        method: method,
        body: method == 'GET' ? '' : jsonEncode(fields),
        headers: const {'Content-Type': 'application/json'},
        timeout: const Duration(seconds: 10),
        retries: 0,
        requireAuthCheck: true,
        expectedAuthenticatedUid: authority.uid,
        exactAuthority: authority);
    if (phase != null)
      WhisperStartupDiagnostics.record(
          phase, response?.statusCode == 200 ? WhisperStartupResult.ok : WhisperStartupResult.failed,
          code: response == null
              ? WhisperStartupCode.noResponse
              : response.statusCode == 200
                  ? WhisperStartupCode.none
                  : WhisperStartupCode.httpFailure,
          httpStatus: response?.statusCode);
    if (!authority.isExactCurrent() || response == null) throw WhisperDeliveryFailure();
    return decodeWhisperDeliveryTransportResponse(method, endpoint, response.statusCode, response.body);
  }

  Future<void> startOutput({bool explicitOn = false}) async {
    _outputPermit ??= AppOutputAdmission.shared.reserveOutput('native-whisper');
    failed = false;
    WhisperStartupDiagnostics.record(WhisperStartupStage.authority, WhisperStartupResult.begin);
    final ExactAccountAuthorityVerifier? authority;
    final User? user;
    try {
      authority = _authorityProvider();
      user = _auth.currentUser;
    } catch (error) {
      WhisperStartupDiagnostics.failure(WhisperStartupStage.authority, error);
      rethrow;
    }
    if (authority == null || user == null || user.uid != authority.uid) {
      WhisperStartupDiagnostics.record(WhisperStartupStage.authority, WhisperStartupResult.failed,
          code: authority == null ? WhisperStartupCode.missingAuthority : WhisperStartupCode.userChanged);
      throw WhisperDeliveryFailure();
    }
    WhisperStartupDiagnostics.record(WhisperStartupStage.authenticationToken, WhisperStartupResult.begin);
    final IdTokenResult token;
    try {
      token = await user.getIdTokenResult();
    } catch (error) {
      WhisperStartupDiagnostics.failure(WhisperStartupStage.authenticationToken, error);
      rethrow;
    }
    // Firebase creates a fresh User wrapper on every read. Identity belongs to
    // the authenticated UID; the captured exact authority still fences lifetime changes.
    final exactCurrent = authority.isExactCurrent();
    if (!exactCurrent || _auth.currentUser?.uid != user.uid || token.authTime == null) {
      WhisperStartupDiagnostics.record(WhisperStartupStage.authenticationReceipt, WhisperStartupResult.failed,
          code: !exactCurrent
              ? WhisperStartupCode.authorityChanged
              : token.authTime == null
                  ? WhisperStartupCode.authTimeMissing
                  : WhisperStartupCode.userChanged);
      throw WhisperDeliveryFailure();
    }
    WhisperStartupDiagnostics.record(WhisperStartupStage.authenticationReceipt, WhisperStartupResult.ok);
    WhisperStartupDiagnostics.record(WhisperStartupStage.presentationPreference, WhisperStartupResult.begin);
    try {
      final prefs = SharedPreferencesUtil();
      _authenticationLifetime = '${authority.uid}:${token.authTime!.millisecondsSinceEpoch}:'
          '${prefs.aiConsentAuthorityGeneration}:${prefs.terminalAccountConsentAuthorityGeneration}:'
          '${prefs.ellaProvisioningTerminalAuthorityGeneration}';
      preferredPresentation = SharedPreferencesUtil().getString('${_key(authority.uid)}.presentation') == 'text_only'
          ? WhisperPresentation.textOnly
          : WhisperPresentation.audio;
    } catch (error) {
      WhisperStartupDiagnostics.failure(WhisperStartupStage.presentationPreference, error);
      rethrow;
    }
    try {
      await client.start(preferredPresentation, explicitOn: explicitOn);
      _schedule();
    } catch (_) {
      failed = true;
      notifyListeners();
      rethrow;
    }
  }

  void _cancelPolling() {
    _pollSchedule++;
    _timer?.cancel();
    _timer = null;
  }

  void _schedule() {
    _cancelPolling();
    if (_permissionHolds != 0) return;
    final schedule = _pollSchedule;
    _timer = Timer.periodic(const Duration(seconds: 3), (_) {
      if (!client.ready) return;
      unawaited(client.poll().catchError((_) {
        // Off, authority retirement or a replacement schedule owns subsequent UI.
        // A current poll can advance client generation before failing, so fence
        // the schedule rather than suppressing all generation changes.
        if (schedule != _pollSchedule) return;
        failed = true;
        _playing = false;
        notifyListeners();
      }));
    });
  }

  Future<void> stopOutput({bool resume = true}) async {
    _cancelPolling();
    _message = null;
    _playing = false;
    failed = false;
    try {
      await client.stop(resume: resume);
      if (client.ready) _schedule();
    } catch (_) {
      failed = true;
      notifyListeners();
      rethrow;
    }
  }

  /// A system permission alert covers the normal Stop controls. Fence current
  /// output before presenting it, and discard its hold-window backlog before
  /// restoring polling. Both Stops preserve capture and configured Whispers.
  Future<T> withoutWhisperOutput<T>(Future<T> Function() permissionRequest) async {
    final authority = _authorityProvider();
    if (authority == null || !authority.isExactCurrent() || (!client.ready && !client.stopPending)) {
      return permissionRequest();
    }
    _permissionHolds++;
    _cancelPolling();
    int? heldGeneration;
    try {
      await stopOutput();
      if (!authority.isExactCurrent()) throw WhisperDeliveryFailure();
      heldGeneration = client.generation;
      return await permissionRequest();
    } finally {
      try {
        if (heldGeneration != null &&
            authority.isExactCurrent() &&
            client.generation == heldGeneration &&
            client.ready) {
          await stopOutput();
        }
      } finally {
        _permissionHolds--;
        if (_permissionHolds == 0 && client.ready) _schedule();
      }
    }
  }

  Future<void> retryStop() async {
    failed = false;
    try {
      await client.reconcileStop();
      if (client.ready) _schedule();
    } catch (_) {
      failed = true;
      notifyListeners();
      rethrow;
    }
  }

  Future<void> setPresentation(WhisperPresentation value) async {
    if (preferredPresentation == value) return;
    final authority = _authorityProvider();
    if (authority == null || !authority.isExactCurrent()) throw WhisperDeliveryFailure();
    final wasReady = client.ready;
    if (wasReady || client.stopPending) await stopOutput(resume: false);
    if (!authority.isExactCurrent()) throw WhisperDeliveryFailure();
    if (!await SharedPreferencesUtil().saveString('${_key(authority.uid)}.presentation', value.wire) ||
        !authority.isExactCurrent()) throw WhisperDeliveryFailure();
    preferredPresentation = value;
    // Choosing the presentation while Off changes a preference, not saved mode.
    if (wasReady) await startOutput(explicitOn: true);
  }

  Map<String, dynamic> _ticket(WhisperDeliveryScope s, int generation) => {
        'uid': s.uid,
        'client_epoch': s.epoch,
        'session_id': s.session,
        'presentation': s.presentation.wire,
        'generation': generation
      };
  bool _nativeCurrent(int operation, int generation) =>
      operation == _nativeOperation && client.generation == generation;

  @override
  Future<void> stop(int generation) async {
    final operation = ++_nativeOperation;
    final s = _nativeScope;
    _playing = false;
    _message = null;
    final result = await _channel.invokeMapMethod<String, dynamic>('strictStop',
        {'uid': s?.uid ?? _authorityProvider()?.uid, if (s != null) 'client_epoch': s.epoch, 'generation': generation});
    if (!_nativeCurrent(operation, generation) || result?['stopped'] != true) throw WhisperDeliveryFailure();
    _nativeScope = null;
  }

  @override
  Future<void> activate(WhisperDeliveryScope scope, int generation) async {
    _outputPermit ??= AppOutputAdmission.shared.reserveOutput('native-whisper');
    final operation = ++_nativeOperation;
    final authority = _authorityProvider();
    if (client.generation != generation || authority?.uid != scope.uid || authority?.isExactCurrent() != true) {
      throw WhisperDeliveryFailure();
    }
    // Reserve the exact ticket before the native receipt, so Stop can close a
    // pending activation. A delayed receipt never writes replacement metadata.
    _nativeScope = scope;
    final r = await _channel.invokeMapMethod<String, dynamic>('strictActivate', _ticket(scope, generation));
    if (!_nativeCurrent(operation, generation) || !authority!.isExactCurrent() || r?['accepted'] != true) {
      throw WhisperDeliveryFailure();
    }
  }

  @override
  Future<void> audio(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation) async {
    _outputPermit ??= AppOutputAdmission.shared.reserveOutput('native-whisper');
    final operation = _nativeOperation;
    final authority = _authorityProvider();
    if (!_nativeCurrent(operation, generation) ||
        _nativeScope != scope ||
        authority?.isExactCurrent() != true ||
        authority?.uid != scope.uid) {
      throw WhisperDeliveryFailure();
    }
    final r = await _channel.invokeMapMethod<String, dynamic>(
        'strictAudio', {..._ticket(scope, generation), 'event_id': event.id, 'url': event.url});
    if (!_nativeCurrent(operation, generation) || !authority!.isExactCurrent() || r?['accepted'] != true) {
      throw WhisperDeliveryFailure();
    }
    _message = event.message;
    _messageScope = scope;
    _messageGeneration = generation;
    notifyListeners();
  }

  @override
  Future<void> text(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation) async {
    if (client.generation != generation || client.scope?.session != scope.session) throw WhisperDeliveryFailure();
    _message = event.message;
    _messageScope = scope;
    _messageGeneration = generation;
    _playing = false;
    notifyListeners();
  }

  Future<void> _nativeFact(MethodCall call) async {
    if (call.method != 'strictPlaybackFact') return;
    final fact = call.arguments;
    if (fact is! Map ||
        !client.ready ||
        fact['generation'] != client.generation ||
        fact['uid'] != client.scope?.uid ||
        fact['client_epoch'] != client.scope?.epoch ||
        fact['session_id'] != client.scope?.session ||
        (fact['status'] != 'started' && fact['status'] != 'completed') ||
        fact['event_id'] is! String ||
        !client.hasCurrentClaim(fact['event_id'], status: fact['status'])) {
      return;
    }
    if (fact['status'] == 'started') {
      _playing = true;
    } else if (fact['status'] == 'completed')
      _playing = false;
    else
      return;
    notifyListeners();
    try {
      await client.report(fact['event_id'], fact['status']);
    } catch (_) {
      failed = true;
      _playing = false;
      notifyListeners();
    }
  }
}
