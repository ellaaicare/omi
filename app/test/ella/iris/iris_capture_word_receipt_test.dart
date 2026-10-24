import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/widgets/capture_word_receipt.dart';

void main() {
  test('cached seed, exact current session and six second expiry fence', () {
    final r = CaptureWordReceipt();
    final now = DateTime(2026, 10, 10);
    void words(String? session, String text) => r.observe(session: session, words: [(id: 'one', text: text)], now: now);
    words('A', 'seed');
    expect(r.recent(now, session: 'A'), false);
    expect(r.age(now, session: 'A'), isNull);
    words('A', 'seed new');
    expect(r.recent(now, session: 'A'), true);
    expect(r.age(now.add(const Duration(seconds: 45)), session: 'A'), const Duration(seconds: 45));
    expect(r.age(now, session: 'B'), isNull);
    expect(r.age(now.subtract(const Duration(seconds: 1)), session: 'A'), isNull);
    expect(r.recent(now, session: 'B'), false); // no provider notification
    expect(r.recent(now, session: null), false);
    expect(r.recent(now.add(const Duration(seconds: 6)), session: 'A'), false);
    expect(r.recent(now.subtract(const Duration(seconds: 1)), session: 'A'), false);
    words('B', 'seed new');
    expect(r.recent(now, session: 'B'), false);
  });
  test('replacement, removal and empty reset never count as arriving words', () {
    final r = CaptureWordReceipt();
    final now = DateTime(2026, 10, 10);
    void words(List<({String id, String text})> w) => r.observe(session: 'A', words: w, now: now);
    words([(id: 'one', text: 'seed')]);
    words([(id: 'one', text: 'seed'), (id: 'two', text: 'new')]);
    expect(r.recent(now, session: 'A'), true);
    words([(id: 'one', text: 'seed')]);
    expect(r.recent(now, session: 'A'), false);
    words([(id: 'one', text: 'replacement more characters')]);
    expect(r.recent(now, session: 'A'), false);
    words([(id: 'one', text: 'replacement more characters'), (id: 'two', text: '   ')]);
    expect(r.recent(now, session: 'A'), false);
    words([(id: 'one', text: 'replacement more characters'), (id: 'two', text: '     ')]);
    expect(r.recent(now, session: 'A'), false);
    words([]);
    expect(r.recent(now, session: 'A'), false);
  });
}
