import 'dart:async';
import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';

typedef NativeCaptureAuthorizationInvoke = Future<Object?> Function(String method, Map<String, Object?> arguments);

/// Account/consent admission for native sinks, independent of durable user mute.
/// No audio, token, user preference, timer, capture start or retry is owned here.
class EllaNativeCaptureAuthorization {
  EllaNativeCaptureAuthorization({NativeCaptureAuthorizationInvoke? invoke})
      : _invoke = invoke ?? ((method, arguments) => _channel.invokeMethod<Object?>(method, arguments));
  static const _channel = MethodChannel('com.ella/capture_authorization');
  final NativeCaptureAuthorizationInvoke _invoke;
  Future<void> _queue = Future<void>.value();
  int _intent = 0;
  (String, int)? _owner;
  String? _operation;

  Future<T> _serialize<T>(Future<T> Function() operation) {
    final result = _queue.then((_) => operation());
    _queue = result.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return result;
  }

  Future<bool> _write(bool allowed, String operation, {String? uid, bool Function()? isCurrent}) async {
    final revision = await _invoke('getRevision', const {});
    if (revision is! int || revision < 0 || revision >= 0x7fffffffffffffff) {
      throw StateError('native authorization revision unavailable');
    }
    if (allowed && !(isCurrent?.call() ?? false)) return false;
    final next = revision + 1;
    final reply = await _invoke('setAuthorization', {
      'allowed': allowed,
      'revision': next,
      'operation': operation,
      if (allowed) 'uid': uid,
    });
    if (reply is! Map || reply['operation'] != operation || reply['allowed'] != allowed) {
      throw StateError('native authorization not confirmed');
    }
    final returnedRevision = reply['revision'];
    if (returnedRevision is! int ||
        returnedRevision < revision ||
        (allowed ? returnedRevision != next : reply['ownedDenied'] != true)) {
      throw StateError('native authorization receipt invalid');
    }
    return true;
  }

  Future<bool> bind(String uid, int epoch, {required bool Function() isCurrent}) {
    final intent = _intent;
    bool current() => _intent == intent && isCurrent();
    return _serialize(() async {
      if (!current()) return false;
      if (_owner == (uid, epoch)) return true;
      final operation = const Uuid().v4();
      _operation = operation;
      try {
        if (!await _write(true, operation, uid: uid, isCurrent: current)) return false;
      } catch (_) {
        _owner = null;
        // A failed allow can have taken effect. Retire only its exact operation;
        // a stale completion must never deny a replacement runtime's owner.
        await _write(false, operation);
        if (_operation == operation) _operation = null;
        rethrow;
      }
      if (!current()) {
        _owner = null;
        await _write(false, operation);
        if (_operation == operation) _operation = null;
        return false;
      }
      _owner = (uid, epoch);
      return true;
    });
  }

  Future<void> deny() {
    _intent++;
    _owner = null;
    final operation = _operation;
    return _serialize(() async {
      if (operation == null) return;
      await _write(false, operation);
      if (_operation == operation) _operation = null;
    });
  }
}
