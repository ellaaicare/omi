import 'package:flutter_test/flutter_test.dart';
import 'package:omi/pages/chat/widgets/markdown_message_widget.dart';

void main() {
  test('adds paragraph spacing only when a reply exceeds four sentences', () {
    const reply = 'One. Two. Three. Four. Five.';

    expect(formatEllaReplyForDisplay(reply), 'One. Two. Three.\n\nFour. Five.');
    expect(formatEllaReplyForDisplay('One. Two. Three. Four.'), 'One. Two. Three. Four.');
  });

  test('preserves replies that already contain paragraph spacing', () {
    const reply = 'One. Two. Three.\n\nFour. Five.';
    expect(formatEllaReplyForDisplay(reply), reply);
  });
}
