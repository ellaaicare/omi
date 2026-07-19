import 'package:flutter_test/flutter_test.dart';
import 'package:omi/utils/streaming_text_coalescer.dart';

void main() {
  test('coalesces cumulative partials into one evolving utterance', () {
    final coalescer = StreamingTextCoalescer();

    expect(coalescer.addPartial('utterance-1', 'Hello'), 'Hello');
    expect(coalescer.addPartial('utterance-1', 'Hello there'), 'Hello there');
    expect(coalescer.addPartial('utterance-1', 'Hello there.'), 'Hello there.');
  });

  test('coalesces delta partials without repeating overlaps', () {
    final coalescer = StreamingTextCoalescer();

    expect(coalescer.addPartial('utterance-1', 'Remember to '), 'Remember to ');
    expect(coalescer.addPartial('utterance-1', 'to call Greg.'), 'Remember to call Greg.');
    expect(coalescer.addPartial('utterance-1', 'Greg.'), 'Remember to call Greg.');
  });

  test('keeps simultaneous utterance ids independent', () {
    final coalescer = StreamingTextCoalescer();

    expect(coalescer.addPartial('a', 'First'), 'First');
    expect(coalescer.addPartial('b', 'Second'), 'Second');
    expect(coalescer.addPartial('a', 'First message'), 'First message');
  });
}
