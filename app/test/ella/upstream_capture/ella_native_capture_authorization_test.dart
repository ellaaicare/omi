import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/ella_native_capture_authorization.dart';

class _Native {
  int revision = 0;
  bool allowed = false;
  String? operation;
  final writes = <Map<String, Object?>>[];
  Completer<void>? heldRead, heldAllow, readEntered, allowEntered;
  bool badReply = false;
  late final gate = EllaNativeCaptureAuthorization(invoke: invoke);
  Future<Object?> invoke(String method, Map<String, Object?> arguments) async {
    if (method == 'getRevision') {
      final hold = heldRead;
      heldRead = null;
      readEntered?.complete();
      readEntered = null;
      await hold?.future;
      return revision;
    }
    final next = Map<String, Object?>.from(arguments);
    writes.add(next);
    if (next['allowed'] == true) {
      final hold = heldAllow;
      heldAllow = null;
      allowEntered?.complete();
      allowEntered = null;
      await hold?.future;
    }
    final op = next['operation'] as String;
    if (next['allowed'] == false && operation != op) {
      return badReply ? null : {'revision': revision, 'allowed': false, 'operation': op, 'ownedDenied': true};
    }
    final proposed = next['revision'] as int;
    if (proposed <= revision) throw StateError('stale native command');
    revision = proposed;
    allowed = next['allowed'] as bool;
    operation = allowed ? op : null;
    return badReply ? null : {'revision': revision, 'allowed': allowed, 'operation': op, 'ownedDenied': !allowed};
  }
}

void main() {
  test('same-owner bind is stable; denial retires native work without a mute preference', () async {
    final n = _Native();
    expect(await n.gate.bind('fictional-a', 3, isCurrent: () => true), true);
    expect(await n.gate.bind('fictional-a', 3, isCurrent: () => true), true);
    expect(n.writes.length, 1);
    await n.gate.deny();
    expect(n.allowed, false);
    expect(n.writes.last['allowed'], false);
    expect(n.writes.last['revision'], 2);
    expect(n.writes.last['operation'], n.writes.first['operation']);
    expect(await n.gate.bind('fictional-a', 4, isCurrent: () => true), true);
    expect(n.revision, 3);
    expect(n.writes.any((e) => e.containsKey('muted') || e.containsKey('capturePolicy')), false);
  });

  test('retired account before dispatch has zero native effects', () async {
    final n = _Native();
    expect(await n.gate.bind('fictional-a', 1, isCurrent: () => false), false);
    expect(n.writes, isEmpty);
  });

  test('retirement while native revision is held refuses the later allow effect', () async {
    final n = _Native();
    var current = true;
    final held = Completer<void>();
    final entered = Completer<void>();
    n.heldRead = held;
    n.readEntered = entered;
    final bind = n.gate.bind('fictional-a', 1, isCurrent: () => current);
    await entered.future;
    current = false;
    held.complete();
    expect(await bind, false);
    expect(n.writes, isEmpty);
  });

  test('revocation during held allow compensates and cannot publish an admitted owner', () async {
    final n = _Native();
    final held = Completer<void>();
    final entered = Completer<void>();
    n.heldAllow = held;
    n.allowEntered = entered;
    final bind = n.gate.bind('fictional-a', 1, isCurrent: () => true);
    await entered.future;
    final denied = n.gate.deny();
    held.complete();
    expect(await bind, false);
    await denied;
    expect(n.allowed, false);
    expect(n.writes.where((e) => e['allowed'] == true).length, 1);
    expect(n.writes.last['allowed'], false);
  });

  test('uncertain allow compensates without leaving a cached admitted owner', () async {
    final n = _Native()..badReply = true;
    await expectLater(n.gate.bind('fictional-a', 1, isCurrent: () => true), throwsStateError);
    expect(n.allowed, false);
    expect(n.writes.first['allowed'], true);
    expect(n.writes.last['allowed'], false);
  });

  test('old runtime failed allow cannot deny a replacement runtime operation', () async {
    final n = _Native();
    final held = Completer<void>();
    final entered = Completer<void>();
    n.heldAllow = held;
    n.allowEntered = entered;
    final old = n.gate.bind('fictional-a', 1, isCurrent: () => true);
    final rejected = expectLater(old, throwsStateError);
    await entered.future;
    final replacement = EllaNativeCaptureAuthorization(invoke: n.invoke);
    expect(await replacement.bind('fictional-b', 2, isCurrent: () => true), true);
    final replacementOperation = n.operation;
    held.complete();
    await rejected;
    expect(n.allowed, true);
    expect(n.operation, replacementOperation);
  });

  test('malformed native denial remains a failure; a fresh denial can recover', () async {
    final n = _Native();
    expect(await n.gate.bind('fictional-a', 1, isCurrent: () => true), true);
    n.badReply = true;
    await expectLater(n.gate.deny(), throwsStateError);
    n.badReply = false;
    await n.gate.deny();
    expect(n.allowed, false);
  });
}
