import 'package:flutter_test/flutter_test.dart';

import 'package:omi/dev/dev_mic_token.dart';

void main() {
  tearDown(DevMicToken.disarm);

  test('unarmed: ordinary ids from the caller, never the test bit', () {
    var seq = 0;
    expect(DevMicToken.mint(() => ++seq), 1);
    expect(DevMicToken.mint(() => ++seq), 2);
    expect(DevMicToken.isTest(2), isFalse);
    expect(DevMicToken.tokenOf(2), isNull);
  });

  test('armed: exactly ONE mint is tagged; the next mint is ordinary again', () {
    var seq = 0;
    DevMicToken.armNext(0x2A5A5);
    final tagged = DevMicToken.mint(() => ++seq);
    final next = DevMicToken.mint(() => ++seq);
    expect(DevMicToken.tokenOf(tagged), 0x2A5A5);
    expect(DevMicToken.armed, isNull, reason: 'consumed by the first mint');
    expect(next, 1, reason: 'a concurrent/ordinary start is never tagged');
    expect(seq, 1);
  });

  test('same token re-armed for a restart keeps the token with the next seq', () {
    DevMicToken.armNext(5);
    final a = DevMicToken.mint(() => 0);
    DevMicToken.armNext(5);
    final b = DevMicToken.mint(() => 0);
    expect(DevMicToken.tokenOf(a), 5);
    expect(DevMicToken.tokenOf(b), 5);
    expect(a & 0xFFFF, 1);
    expect(b & 0xFFFF, 2);
  });

  test('disarm without a mint leaves nothing armed (non-consuming recorder)', () {
    DevMicToken.armNext(6);
    DevMicToken.disarm();
    expect(DevMicToken.mint(() => 7), 7);
  });

  test('encoding matches PhoneMicDevTokenPolicy (Swift): testBit 1<<62, token<<16, 30-bit token', () {
    DevMicToken.armNext(DevMicToken.tokenMask);
    final id = DevMicToken.mint(() => 0);
    expect(id, (1 << 62) | (((1 << 30) - 1) << 16) | (id & 0xFFFF));
    expect(id > 0, isTrue, reason: 'stays a positive Int64 on the native side');
    expect(DevMicToken.tokenOf(id), (1 << 30) - 1);
  });

  test('invalid tokens are rejected', () {
    expect(() => DevMicToken.armNext(0), throwsArgumentError);
    expect(() => DevMicToken.armNext(1 << 30), throwsArgumentError);
  });
}
