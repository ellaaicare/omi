import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/services/sockets/transcription_service.dart';

/// Fake transport whose status this test controls directly, so a
/// "connected" socket can exist independently of whether
/// [TranscriptSegmentSocketService.start] has ever run (and therefore
/// independently of whether an active AI consent session lease exists).
class _FakeSocket implements IPureSocket {
  PureSocketStatus _status = PureSocketStatus.notConnected;
  final List<dynamic> sent = [];

  @override
  PureSocketStatus get status => _status;

  set status(PureSocketStatus value) => _status = value;

  @override
  Future<bool> connect() async {
    _status = PureSocketStatus.connected;
    return true;
  }

  @override
  Future disconnect() async => _status = PureSocketStatus.disconnected;

  @override
  Future stop() async => _status = PureSocketStatus.disconnected;

  @override
  void send(dynamic message) => sent.add(message);

  @override
  void setListener(IPureSocketListener listener) {}

  @override
  void onMessage(dynamic message) {}

  @override
  void onConnected() {}

  @override
  void onClosed() {}

  @override
  void onError(Object err, StackTrace trace) {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  void grantDurableCachedConsent(String uid) {
    final preferences = SharedPreferencesUtil()..uid = uid;
    preferences.acceptAiConsent(
      receiptId: 'aicr_$uid',
      uid: uid,
      profileBindingId: 'profile-$uid',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: uid,
      receiptId: 'aicr_$uid',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-$uid',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  }

  test(
    'a connected socket with no active AI consent session lease sends nothing, '
    'even though durable cached consent is accepted',
    () async {
      grantDurableCachedConsent('uid-a');
      expect(SharedPreferencesUtil().aiConsentAccepted, isTrue);

      final fakeSocket = _FakeSocket()..status = PureSocketStatus.connected;
      final service = TranscriptSegmentSocketService.withSocket(
        16000,
        BleAudioCodec.pcm16,
        'en',
        fakeSocket,
        requireCaptureProtocol: false,
      );

      // start() was never called: no AiConsentActiveSessionLease exists, but
      // the socket transport itself already reports connected.
      expect(service.state, SocketServiceState.connected);

      await service.send([1, 2, 3]);
      await service.sendText('hello');

      expect(fakeSocket.sent, isEmpty);
    },
  );

  test('once a valid session lease is established, the same socket sends normally', () async {
    grantDurableCachedConsent('uid-b');

    final fakeSocket = _FakeSocket();
    final service = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.pcm16,
      'en',
      fakeSocket,
      requireCaptureProtocol: false,
    );

    await service.start();
    expect(service.state, SocketServiceState.connected);

    await service.send([1, 2, 3]);

    expect(fakeSocket.sent, [
      [1, 2, 3]
    ]);
  });
}
