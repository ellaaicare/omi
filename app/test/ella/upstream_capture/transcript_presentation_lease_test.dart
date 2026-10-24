import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/transcript_presentation_lease.dart';

final _provider = Object();
TranscriptPresentationScope scope({String uid='fixture-owner', int epoch=4,
    Object? provider, String producer='pendant-8', String source='omi',
    String mode='live', int device=2}) => TranscriptPresentationScope(
  uid: uid, bindingEpoch: epoch, providerIdentity: provider ?? _provider,
  producerSessionKey: producer, mode: mode, source: source, deviceRevision: device);

void main() {
  test('new conversation projections keep the same authorized producer viewer', () {
    final lease=TranscriptPresentationLease(scope());
    // Each notification reprojects a new scope object. Conversation/window IDs
    // are intentionally outside this presentation-only scope.
    expect(lease.observe(scope()), isTrue);
    expect(lease.observe(scope()), isTrue);
    expect(lease.retired, isFalse);
  });
  test('Stop then reopen cannot revive the old sheet', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(null), isFalse);
    expect(lease.observe(scope()), isFalse);
  });
  test('coalesced Stop/new capture is rejected without observing idle', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(scope(producer:'pendant-9')), isFalse);
    expect(lease.observe(scope()), isFalse);
  });
  test('switching owner retires before any future presentation', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(scope(uid:'different-fixture-owner')), isFalse);
    expect(lease.observe(scope()), isFalse);
  });
  test('same owner authority retirement cannot revive on a new epoch', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(scope(epoch:5)), isFalse);
    expect(lease.observe(scope()), isFalse);
  });
  test('provider replacement cannot adopt the old viewer', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(scope(provider:Object())), isFalse);
  });
  test('source and transport transitions each retire the viewer', () {
    final sourceLease=TranscriptPresentationLease(scope());
    expect(sourceLease.observe(scope(source:'phone')), isFalse);
    final modeLease=TranscriptPresentationLease(scope());
    expect(modeLease.observe(scope(mode:'batch')), isFalse);
  });
  test('device identity revision retires even with unchanged logical key', () {
    final lease=TranscriptPresentationLease(scope());
    expect(lease.observe(scope(device:3)), isFalse);
    expect(lease.observe(scope()), isFalse);
  });
}
