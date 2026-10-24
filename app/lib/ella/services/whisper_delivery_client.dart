import 'dart:async';

import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/services/whisper_startup_diagnostics.dart';
import 'package:uuid/uuid.dart';

typedef DeliveryTransport = Future<Map<String, dynamic>> Function(
    String method, String endpoint, Map<String, dynamic> fields, ExactAccountAuthorityVerifier authority);
typedef DeliveryStoreRead = Future<Map<String, dynamic>?> Function(String owner);
typedef DeliveryStoreWrite = Future<void> Function(String owner, Map<String, dynamic> state);

enum WhisperPresentation { audio, textOnly }

extension WhisperPresentationWire on WhisperPresentation {
  String get wire => this == WhisperPresentation.audio ? 'audio' : 'text_only';
}

class WhisperDeliveryScope {
  const WhisperDeliveryScope(this.uid, this.device, this.session, this.epoch, this.presentation, this.revision);
  final String uid, device, session, epoch;
  final WhisperPresentation presentation;
  final int revision;
  Map<String, String> get captureQuery => {
        'delivery_protocol': '1',
        'delivery_device_id': device,
        'delivery_session_id': session,
        'delivery_client_epoch': epoch,
        'delivery_revision': '$revision',
        'delivery_tool_protocol': '1',
      };
  Map<String, dynamic> get fields =>
      {'device_id': device, 'session_id': session, 'client_epoch': epoch, 'revision': revision};
  Map<String, dynamic> get stored => {...fields, 'uid': uid, 'presentation': presentation.wire};
}

class WhisperDeliveryEvent {
  const WhisperDeliveryEvent(this.id, this.position, this.url, this.message);
  final String id;
  final int position;
  final String? url;
  final String? message;
}

abstract interface class WhisperDeliveryMedia {
  /// Native output fence is retired before awaiting any server operation.
  Future<void> stop(int generation);
  Future<void> activate(WhisperDeliveryScope scope, int generation);
  Future<void> audio(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation);
  Future<void> text(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation);
}

class WhisperDeliveryFailure extends StateError {
  WhisperDeliveryFailure() : super('Whisper delivery unavailable');
}

/// A Stop conflict hint, never a closure receipt. Exact readback is still required.
class WhisperDeliveryStopRequestStale extends WhisperDeliveryFailure {}

bool deliveryUuid(Object? value) =>
    value is String && RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$').hasMatch(value);

/// Strict protocol1 actor. No legacy transport or direct audio/TTS fallback.
/// Every async boundary checks the captured exact authority and local generation.
class WhisperDeliveryClient {
  WhisperDeliveryClient(
      {required this.transport,
      required this.media,
      required this.read,
      required this.write,
      required this.authorityProvider,
      this.newId = _uuid,
      this.onChanged,
      this.lifetimeIdentity});
  final DeliveryTransport transport;
  final WhisperDeliveryMedia media;
  final DeliveryStoreRead read;
  final DeliveryStoreWrite write;
  final ExactAccountAuthorityVerifier? Function() authorityProvider;
  final String Function() newId;
  final void Function()? onChanged;
  final String Function(ExactAccountAuthorityVerifier)? lifetimeIdentity;
  String? _lifetime;
  Future<void> _storageTail = Future.value();
  static String _uuid() => const Uuid().v4();
  ExactAccountAuthorityVerifier? _authority;
  WhisperDeliveryScope? _scope;
  String? _epoch, _deviceRequest, _sessionRequest;
  Map<String, dynamic>? _pendingStop;
  final Set<String> _seen = {};
  final Map<String, ({WhisperDeliveryScope scope, String claim, int generation, Set<String> reports})> _claims = {};
  int generation = 0;
  int _cursor = 0;
  bool ready = false;
  bool _resumeAllowed = true;
  String? acknowledgedStopRequest;
  int? _pollingGeneration;
  bool _stopping = false;
  bool get stopPending => _pendingStop != null;
  WhisperPresentation? get presentation => _scope?.presentation;
  WhisperDeliveryScope? get scope => ready && _current(generation) ? _scope : null;

  bool hasCurrentClaim(String eventId, {String? status}) {
    final claim = _claims[eventId];
    return claim != null &&
        _current(claim.generation) &&
        scope?.session == claim.scope.session &&
        (status == null || (!claim.reports.contains(status) && !claim.reports.contains('completed')));
  }

  bool _current(int g) => generation == g && _authority?.isExactCurrent() == true;
  void _check(int g) {
    if (!_current(g)) throw WhisperDeliveryFailure();
  }

  void _changed() => onChanged?.call();
  Map<String, dynamic> _state() => {
        'lifetime': _lifetime,
        'epoch': _epoch,
        'device_request_id': _deviceRequest,
        'session_request_id': _sessionRequest,
        'scope': _scope?.stored,
        'seen': _seen.toList(),
        'cursor': _cursor,
        'pending_stop': _pendingStop,
        'resume_allowed': _resumeAllowed
      };
  Future<void> _persist(int g) async {
    _check(g);
    final owner = _authority!.uid, snapshot = _state();
    final raw = _storageTail.then((_) async {
      _check(g);
      await write(owner, snapshot);
      _check(g);
    });
    _storageTail = raw.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    try {
      await raw.timeout(const Duration(seconds: 10));
      _check(g);
    } catch (error) {
      WhisperStartupDiagnostics.failure(WhisperStartupStage.preferencesPersist, error);
      rethrow;
    }
  }

  Future<Map<String, dynamic>> _request(String method, String path, Map<String, dynamic> body, int g) async {
    _check(g);
    final result =
        await transport(method, path, Map.unmodifiable(body), _authority!).timeout(const Duration(seconds: 10));
    _check(g);
    return result;
  }

  Never _invalid() => throw WhisperDeliveryFailure();
  void _base(Map<String, dynamic> m, WhisperDeliveryScope s, {int? revision, bool closed = false}) {
    if (m['contract'] is! int ||
        m['contract'] != 1 ||
        m['uid'] != s.uid ||
        m['device_id'] != s.device ||
        m['session_id'] != s.session ||
        m['client_epoch'] != s.epoch ||
        m['presentation'] != s.presentation.wire ||
        m['revision'] is! int ||
        m['revision'] != (revision ?? s.revision) ||
        m['closed'] != closed ||
        !m.containsKey('event') ||
        !m.containsKey('stop_request_id')) {
      _invalid();
    }
    final c = m['cursor'];
    if (c is! Map ||
        c.length != 2 ||
        c['revision'] is! int ||
        c['revision'] != m['revision'] ||
        c['position'] is! int ||
        c['position'] < 0) {
      _invalid();
    }
    if (!closed && m['stop_request_id'] != null) _invalid();
  }

  WhisperDeliveryScope _fresh(
      Map<String, dynamic> m, String uid, String device, String epoch, WhisperPresentation presentation,
      {String? previousSession}) {
    if (!deliveryUuid(m['session_id']) || m['session_id'] == previousSession) _invalid();
    final s = WhisperDeliveryScope(uid, device, m['session_id'], epoch, presentation, 1);
    _base(m, s);
    if (m['event'] != null || m['cursor']['position'] != 0) _invalid();
    return s;
  }

  Future<void> start(WhisperPresentation presentation, {bool explicitOn = false}) async {
    var phase = WhisperStartupStage.stateReconciliation;
    void enter(WhisperStartupStage stage) {
      phase = stage;
      WhisperStartupDiagnostics.record(stage, WhisperStartupResult.begin);
    }

    enter(phase);
    try {
      if (!_resumeAllowed && !explicitOn && !stopPending && _authority?.isExactCurrent() == true) return;
      if (ready && _current(generation) && _scope?.presentation == presentation) return;
      if (!ready && !stopPending && _scope != null && _authority?.isExactCurrent() == true) {
        await stop(resume: _resumeAllowed);
        if (ready && _scope?.presentation == presentation) return;
        if (!_resumeAllowed && !explicitOn) return;
      }
      if (stopPending) {
        if (_stopping) throw WhisperDeliveryFailure();
        enter(WhisperStartupStage.pendingStop);
        await reconcileStop();
        if (ready && _scope?.presentation == presentation) return;
        if (!_resumeAllowed && !explicitOn) return;
      }
      if (ready && _scope?.presentation != presentation) await stop(resume: false);
      _resumeAllowed = true;
      final authority = authorityProvider();
      if (authority == null || !authority.isExactCurrent()) _invalid();
      final g = ++generation;
      ready = false;
      _changed();
      final sameLifetime = _authority?.isExactCurrent() == true && _authority?.uid == authority.uid;
      _authority = authority;
      if (!sameLifetime) {
        _epoch = newId();
        _deviceRequest = null;
        _scope = null;
        _seen.clear();
        _pendingStop = null;
        _sessionRequest = null;
      }
      enter(WhisperStartupStage.nativeStop);
      await media.stop(g).timeout(const Duration(seconds: 10));
      _check(g);
      try {
        enter(WhisperStartupStage.stateRead);
        final saved = await read(authority.uid);
        _check(g);
        enter(WhisperStartupStage.stateValidation);
        _lifetime = lifetimeIdentity?.call(authority);
        if (!sameLifetime &&
            _lifetime != null &&
            saved?['lifetime'] == _lifetime &&
            saved?['resume_allowed'] == false &&
            saved?['pending_stop'] == null &&
            !explicitOn) {
          _resumeAllowed = false;
          return;
        }
        if (!sameLifetime && _lifetime != null && saved?['lifetime'] == _lifetime && saved?['pending_stop'] != null) {
          final raw = saved!['scope'], pending = saved['pending_stop'];
          if (raw is! Map ||
              pending is! Map<String, dynamic> ||
              raw['uid'] != authority.uid ||
              !deliveryUuid(raw['device_id']) ||
              !deliveryUuid(raw['session_id']) ||
              !deliveryUuid(raw['client_epoch']) ||
              raw['revision'] is! int ||
              raw['revision'] < 1 ||
              !['audio', 'text_only'].contains(raw['presentation']) ||
              !deliveryUuid(pending['request_id']) ||
              pending['resume'] is! bool ||
              pending['client_epoch'] != raw['client_epoch'] ||
              pending['session_id'] != raw['session_id'] ||
              pending['device_id'] != raw['device_id'] ||
              pending['revision'] != raw['revision'] ||
              pending['uid'] != raw['uid'] ||
              pending['presentation'] != raw['presentation'] ||
              saved['cursor'] is! int ||
              saved['cursor'] < 0) {
            _invalid();
          }
          _epoch = raw['client_epoch'];
          _scope = WhisperDeliveryScope(
              authority.uid,
              raw['device_id'],
              raw['session_id'],
              _epoch!,
              raw['presentation'] == 'audio' ? WhisperPresentation.audio : WhisperPresentation.textOnly,
              raw['revision']);
          _cursor = saved['cursor'];
          _resumeAllowed = saved['resume_allowed'] != false;
          _pendingStop = Map.of(pending);
          enter(WhisperStartupStage.pendingStop);
          await _stopAck(g);
          if (ready && _scope?.presentation == presentation) return;
          // Start the separately authorized On only after the original immutable
          // Stop has reconciled, with a new local operation/generation.
          return start(presentation, explicitOn: explicitOn);
        }
        _deviceRequest ??= saved?['device_request_id'] is String && deliveryUuid(saved!['device_request_id'])
            ? saved['device_request_id'] as String
            : newId();
        // Persist registration/session operation IDs before transport; uncertain replies use the same IDs.
        _sessionRequest ??= newId();
        enter(WhisperStartupStage.preferencesPersist);
        await _persist(g);
        enter(WhisperStartupStage.deviceApi);
        final d = await _request('POST', 'devices', {'request_id': _deviceRequest}, g);
        enter(WhisperStartupStage.deviceValidation);
        if (d['contract'] is! int || d['contract'] != 1 || d['uid'] != authority.uid || !deliveryUuid(d['device_id'])) {
          _invalid();
        }
        enter(WhisperStartupStage.sessionApi);
        final response = await _request(
            'POST',
            'sessions',
            {
              'request_id': _sessionRequest,
              'device_id': d['device_id'],
              'client_epoch': _epoch,
              'presentation': presentation.wire
            },
            g);
        enter(WhisperStartupStage.sessionValidation);
        _scope = _fresh(response, authority.uid, d['device_id'], _epoch!, presentation);
        _cursor = 0;
        _seen.clear();
        enter(WhisperStartupStage.preferencesPersist);
        await _persist(g);
        enter(WhisperStartupStage.nativeActivate);
        await media.activate(_scope!, g).timeout(const Duration(seconds: 10));
        _check(g);
        ready = true;
        _changed();
        WhisperStartupDiagnostics.record(WhisperStartupStage.ready, WhisperStartupResult.ok);
      } catch (_) {
        await _fail(g);
        rethrow;
      }
    } catch (error) {
      WhisperStartupDiagnostics.failure(phase, error);
      rethrow;
    }
  }

  Future<void> _fail(int g) async {
    if (generation != g) return;
    ready = false;
    final closed = ++generation;
    _changed();
    try {
      await media.stop(closed).timeout(const Duration(seconds: 10));
    } catch (_) {}
  }

  /// Each authentication/consent lifetime gets a fresh client epoch, even same UID ABA.
  Future<void> retireAuthority() async {
    ready = false;
    final g = ++generation;
    _authority = null;
    _scope = null;
    _epoch = null;
    _deviceRequest = null;
    _sessionRequest = null;
    _pendingStop = null;
    _seen.clear();
    _claims.clear();
    _changed();
    await media.stop(g).timeout(const Duration(seconds: 10));
  }

  Future<void> poll() async {
    if (!ready || _pollingGeneration == generation || _stopping) return;
    final g = generation, s = _scope!;
    _pollingGeneration = g;
    try {
      final m = await _request('GET', 'next', s.fields, g);
      if (m['closed'] == true) {
        acknowledgedStopRequest = null;
        // Server cleanup can finish after playback and before the next Off.
        // Its exact closure is not a playback error or this client's Stop ACK.
        ready = false;
        final close = ++generation;
        _pendingStop = {...s.stored, 'request_id': newId(), 'resume': false};
        _stopping = true;
        _changed();
        try {
          await media.stop(close).timeout(const Duration(seconds: 10));
          _check(close);
          await _persist(close);
          await _commitStopReceipt(close, m, independentlyClosed: true);
        } finally {
          _stopping = false;
        }
        return;
      }
      _base(m, s);
      final position = m['cursor']['position'] as int;
      if (position < _cursor) _invalid();
      _cursor = position;
      final raw = m['event'];
      if (raw == null) {
        await _persist(g);
        return;
      }
      if (raw is! Map || !deliveryUuid(raw['event_id']) || raw['position'] is! int || raw['position'] != _cursor + 1) {
        _invalid();
      }
      final url = raw['url'], message = raw['message'];
      if (s.presentation == WhisperPresentation.audio) {
        if (url is! String || url.trim().isEmpty || Uri.tryParse(url)?.scheme != 'https') _invalid();
      } else if (url != null || message is! String || message.trim().isEmpty) {
        _invalid();
      }
      final event = WhisperDeliveryEvent(raw['event_id'], raw['position'], url as String?, message as String?);
      if (_seen.length >= 512) _invalid();
      if (_seen.contains(event.id)) {
        _invalid(); // Unknown/replayed claim never becomes an automatic playback instruction.
      }
      final claim = newId();
      final fields = {...s.fields, 'event_id': event.id, 'position': event.position, 'claim_request_id': claim};
      final claimed = await _request('POST', 'claim', fields, g);
      _base(claimed, s);
      if (claimed['status'] != 'claimed' ||
          claimed['event_id'] != event.id ||
          claimed['position'] is! int ||
          claimed['position'] != event.position ||
          claimed['claim_request_id'] != claim ||
          claimed['cursor']['position'] != _cursor) {
        _invalid();
      }
      _seen.add(event.id);
      await _persist(g);
      _claims[event.id] = (
        scope: s,
        claim: claim,
        generation: g,
        reports: <String>{}
      ); // Durable seen fence precedes every media and ACK effect.
      _check(g);
      if (s.presentation == WhisperPresentation.audio) {
        await media.audio(s, event, g).timeout(const Duration(seconds: 10));
      } else {
        await media.text(s, event, g).timeout(const Duration(seconds: 10));
      }
      _check(g);
      final ack = newId();
      final acknowledged = await _request('POST', 'ack', {...fields, 'ack_request_id': ack}, g);
      _base(acknowledged, s);
      if (acknowledged['status'] != 'acknowledged' ||
          acknowledged['event_id'] != event.id ||
          acknowledged['ack_request_id'] != ack ||
          acknowledged['cursor']['position'] != event.position) {
        _invalid();
      }
      _cursor = event.position;
      await _persist(g);
    } catch (_) {
      await _fail(g);
      rethrow;
    } finally {
      if (_pollingGeneration == g) _pollingGeneration = null;
    }
  }

  Future<void> stop({bool resume = true}) async {
    acknowledgedStopRequest = null;
    if (!resume) _resumeAllowed = false;
    if (_stopping) {
      if (resume) throw WhisperDeliveryFailure();
      ready = false;
      final close = ++generation;
      _changed();
      await media.stop(close).timeout(const Duration(seconds: 10));
      _check(close);
      await _persist(close);
      return; // Local Off is confirmed, but no server Stop ACK is asserted.
    }
    if (stopPending) {
      await reconcileStop();
      return;
    }
    final s = _scope;
    final authority = _authority;
    if (s == null || authority == null || !authority.isExactCurrent()) {
      await retireAuthority();
      return;
    }
    ready = false;
    final g = ++generation;
    _changed();
    _pendingStop = {...s.stored, 'request_id': newId(), 'resume': resume};
    _stopping = true;
    try {
      await media.stop(g).timeout(const Duration(seconds: 10));
      _check(g);
      await _persist(g); // Original tuple/request survives timeout; retry never targets the resumed scope.
      await _stopAck(g);
    } finally {
      _stopping = false;
    }
  }

  Future<void> reconcileStop() async {
    if (_stopping || _pendingStop == null) return;
    final g = generation;
    _stopping = true;
    try {
      _check(g);
      // A retained pending request may precede native confirmation or storage.
      // Every retry must confirm both before accepting any server closure.
      await media.stop(g).timeout(const Duration(seconds: 10));
      _check(g);
      await _persist(g);
      await _stopAck(g);
    } finally {
      _stopping = false;
    }
  }

  Future<void> _stopAck(int g) async {
    final pending = _pendingStop!;
    final original = _scope!;
    if (pending['uid'] != original.uid ||
        pending['session_id'] != original.session ||
        pending['client_epoch'] != original.epoch) {
      _invalid();
    }
    final request = {...original.fields, 'request_id': pending['request_id'], 'resume': pending['resume']};
    late final Map<String, dynamic> m;
    var independentlyClosed = false;
    try {
      m = await _request('POST', 'stop', request, g);
    } on WhisperDeliveryStopRequestStale {
      // Off can retire an exact already-closed scope, but cannot manufacture a
      // resume session or claim this new Stop request was acknowledged.
      if (pending['resume'] != false) rethrow;
      m = await _request('GET', 'next', original.fields, g);
      independentlyClosed = true;
    }
    await _commitStopReceipt(g, m, independentlyClosed: independentlyClosed);
  }

  Future<void> _commitStopReceipt(int g, Map<String, dynamic> m, {required bool independentlyClosed}) async {
    _check(g);
    final pending = _pendingStop!;
    final original = _scope!;
    _base(m, original, revision: original.revision + 1, closed: true);
    if ((independentlyClosed ? !deliveryUuid(m['stop_request_id']) : m['stop_request_id'] != pending['request_id']) ||
        m['event'] != null ||
        m['cursor']['position'] < _cursor) {
      _invalid();
    }
    WhisperDeliveryScope? fresh;
    if (pending['resume'] == true) {
      if (m['resume'] is! Map<String, dynamic>) _invalid();
      fresh = _fresh(m['resume'], original.uid, original.device, original.epoch, original.presentation,
          previousSession: original.session);
    } else if (independentlyClosed
        ? (m.containsKey('resume') && m['resume'] != null)
        : (!m.containsKey('resume') || m['resume'] != null)) {
      _invalid();
    }
    _check(g);
    // Native activation cannot play by itself. Keep original receipt recoverable until durable commit.
    final oldCursor = _cursor, oldSeen = Set<String>.of(_seen);
    try {
      if (fresh != null && _resumeAllowed) {
        await media.activate(fresh, g).timeout(const Duration(seconds: 10));
        _check(g);
      }
      _scope = fresh;
      _cursor = 0;
      _seen.clear();
      _pendingStop = null;
      _sessionRequest = null;
      await _persist(g);
      ready = fresh != null && _resumeAllowed;
      acknowledgedStopRequest = independentlyClosed ? null : pending['request_id'] as String;
      _claims.clear();
      _changed();
    } catch (_) {
      if (_current(g)) {
        _scope = original;
        _cursor = oldCursor;
        _seen.addAll(oldSeen);
        _pendingStop = pending;
        await _fail(g);
      }
      rethrow;
    }
  }

  /// Existing saved-mode ACK closes every strict owner scope in the server transaction.
  /// Only that exact confirmed Off permits forgetting an uncertain delivery Stop.
  Future<void> savedOffConfirmed(ExactAccountAuthorityVerifier authority) async {
    if (!authority.isExactCurrent() || authority.uid != _authority?.uid) return;
    _resumeAllowed = false;
    ready = false;
    final g = ++generation;
    _changed();
    await media.stop(g).timeout(const Duration(seconds: 10));
    _check(g);
    _scope = null;
    _pendingStop = null;
    _sessionRequest = null;
    // Confirmed saved Off retires the native epoch. A subsequent explicit On
    // must establish a fresh ticket instead of reviving that retired epoch.
    _epoch = newId();
    _claims.clear();
    _seen.clear();
    _cursor = 0;
    acknowledgedStopRequest = null;
    await _persist(g);
  }

  Future<void> report(Object? eventId, Object? status) async {
    final claim = _claims[eventId];
    if (claim == null ||
        !ready ||
        !_current(claim.generation) ||
        _scope?.session != claim.scope.session ||
        !['started', 'completed', 'canceled', 'discarded'].contains(status)) {
      return;
    }
    if (!claim.reports.add(status as String)) return;
    final request = newId(), g = claim.generation;
    try {
      final m = await _request(
          'POST',
          'report',
          {
            ...claim.scope.fields,
            'event_id': eventId,
            'claim_request_id': claim.claim,
            'request_id': request,
            'status': status
          },
          g);
      _base(m, claim.scope);
      if (m['event_id'] != eventId || m['status'] != status || m['request_id'] != request) _invalid();
    } catch (_) {
      await _fail(g);
      rethrow;
    }
  }

  /// Exact current scope only; existing capture remains independent of playback Stop.
  Map<String, String> get captureQuery {
    final s = scope;
    return s?.captureQuery ?? const {};
  }
}
