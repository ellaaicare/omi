import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';

class _Listener implements IPureSocketListener {
  final closed = Completer<void>();
  @override
  void onClosed([int? code]) {
    if (!closed.isCompleted) closed.complete();
  }

  @override
  void onConnected() {}
  @override
  void onMessage(dynamic value) {}
  @override
  void onError(Object error, StackTrace stack) {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final local in [false, true]) {
    test('actual loopback stream attachment and ${local ? "local cleanup" : "remote onDone"} distinct', () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final accepted = Completer<WebSocket>();
      final subscription = server.listen((request) async {
        accepted.complete(await WebSocketTransformer.upgrade(request));
      });
      final events = <({PureSocketDiagnosticEvent event, int generation, int? code})>[];
      final socket = PureSocket('ws://127.0.0.1:${server.port}',
          headersProvider: () async => {},
          diagnosticObserver: (event, generation, code) =>
              events.add((event: event, generation: generation, code: code)));
      final listener = _Listener();
      socket.setListener(listener);
      try {
        expect(await socket.connect(), isTrue);
        final peer = await accepted.future;
        expect(events.map((e) => e.event),
            [PureSocketDiagnosticEvent.listenerAttached, PureSocketDiagnosticEvent.transportConnected]);
        if (local) {
          await socket.stop();
        } else {
          await peer.close(1011, 'SECRET remote reason');
        }
        await listener.closed.future;
        await Future<void>.delayed(const Duration(milliseconds: 10));
        final remotes = events.where((e) => e.event == PureSocketDiagnosticEvent.remoteClosed).toList();
        if (local) {
          expect(remotes, isEmpty, reason: 'disconnect calls onClosed locally; it is not remote onDone');
          expect(events.last.event, PureSocketDiagnosticEvent.cleanupCloseRequested);
          expect(events.last.code, 1000, reason: 'requested cleanup code, not fabricated received code');
        } else {
          expect(remotes, hasLength(1));
          expect(remotes.single.code, 1011);
          expect(events.last.event, PureSocketDiagnosticEvent.cleanupCloseRequested);
        }
        expect(events.last.generation, events.first.generation,
            reason: 'cleanup identifies the retired channel, not the next invalidation generation');
        expect(events.toString(), isNot(contains('SECRET')));
        await peer.close();
      } finally {
        await socket.stop();
        await subscription.cancel();
        await server.close(force: true);
      }
    });
  }
  test('throwing diagnostic observer cannot alter actual transport connect or close', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final accepted = Completer<WebSocket>();
    final subscription = server.listen((r) async => accepted.complete(await WebSocketTransformer.upgrade(r)));
    final socket = PureSocket('ws://127.0.0.1:${server.port}',
        headersProvider: () async => {}, diagnosticObserver: (_, __, ___) => throw StateError('diagnostic-only'));
    try {
      expect(await socket.connect(), isTrue);
      await socket.stop();
      await (await accepted.future).close();
    } finally {
      await subscription.cancel();
      await server.close(force: true);
    }
  });
}
