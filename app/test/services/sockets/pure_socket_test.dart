import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'package:omi/services/sockets/pure_socket.dart';

class _BlockingWebSocketSink extends Fake implements WebSocketSink {
  final Completer<void> closeBarrier = Completer<void>();
  int closeCalls = 0;

  @override
  Future<void> close([int? closeCode, String? closeReason]) {
    closeCalls++;
    return closeBarrier.future;
  }
}

class _BlockingWebSocketChannel extends Fake implements WebSocketChannel {
  _BlockingWebSocketChannel(this.blockingSink);

  final _BlockingWebSocketSink blockingSink;

  @override
  WebSocketSink get sink => blockingSink;

  @override
  int? get closeCode => 1000;
}

class _CloseListener implements IPureSocketListener {
  int closeCalls = 0;

  @override
  void onClosed([int? closeCode]) => closeCalls++;

  @override
  void onConnected() {}

  @override
  void onError(Object err, StackTrace trace) {}

  @override
  void onMessage(dynamic message) {}
}

void main() {
  test('disconnect waits for the underlying channel close and notifies its listener once', () async {
    final sink = _BlockingWebSocketSink();
    final socket = PureSocket('wss://example.invalid', connectedChannel: _BlockingWebSocketChannel(sink));
    final listener = _CloseListener();
    socket.setListener(listener);
    var disconnectCompleted = false;
    var overlappingDisconnectCompleted = false;

    final disconnect = socket.disconnect().then((_) => disconnectCompleted = true);
    final overlappingDisconnect = socket.disconnect().then((_) => overlappingDisconnectCompleted = true);
    await pumpEventQueue();

    expect(sink.closeCalls, 1);
    expect(disconnectCompleted, isFalse);
    expect(overlappingDisconnectCompleted, isFalse);
    expect(listener.closeCalls, 0);

    sink.closeBarrier.complete();
    await Future.wait([disconnect, overlappingDisconnect]);
    expect(disconnectCompleted, isTrue);
    expect(overlappingDisconnectCompleted, isTrue);
    expect(listener.closeCalls, 1);

    socket.onClosed(1000);
    expect(listener.closeCalls, 1, reason: 'stream onDone after local close must not release a second retry');
  });

  test('disconnect timeout fails closed instead of releasing a replacement connection', () async {
    final sink = _BlockingWebSocketSink();
    final socket = PureSocket(
      'wss://example.invalid',
      connectedChannel: _BlockingWebSocketChannel(sink),
      disconnectCloseTimeout: const Duration(milliseconds: 1),
    );
    final listener = _CloseListener();
    socket.setListener(listener);

    await expectLater(socket.disconnect(), throwsA(isA<TimeoutException>()));
    await expectLater(socket.disconnect(), throwsA(isA<TimeoutException>()));

    expect(sink.closeCalls, 1, reason: 'a subsequent stop must remain behind the original close fence');
    expect(listener.closeCalls, 0, reason: 'a timed-out old socket cannot authorize a replacement retry');

    sink.closeBarrier.complete();
    await socket.disconnect();
    expect(listener.closeCalls, 1);
  });
}
