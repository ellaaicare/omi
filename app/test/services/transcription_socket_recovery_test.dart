import 'package:flutter_test/flutter_test.dart';

import 'package:omi/services/sockets/transcription_socket_recovery.dart';

void main() {
  test('a connected socket waiting on consent does not open another listen session', () {
    expect(
      shouldOpenReplacementListenSocket(socketConnected: true, hasSessionAuthority: false),
      isFalse,
    );
  });

  test('a disconnected socket may be replaced', () {
    expect(
      shouldOpenReplacementListenSocket(socketConnected: false, hasSessionAuthority: false),
      isTrue,
    );
    expect(
      shouldOpenReplacementListenSocket(socketConnected: true, hasSessionAuthority: true),
      isTrue,
    );
  });
}
