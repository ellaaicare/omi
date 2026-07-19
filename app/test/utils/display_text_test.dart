import 'package:flutter_test/flutter_test.dart';
import 'package:omi/utils/display_text.dart';

void main() {
  test('strips Ella display prefixes without removing the wing', () {
    expect(stripEllaDisplayPrefix('[Ella] A good afternoon'), 'A good afternoon');
    expect(stripEllaDisplayPrefix('[ella]: Remember the appointment'), 'Remember the appointment');
    expect(stripEllaDisplayPrefix('🪽 [Ella] Family lunch'), '🪽 Family lunch');
  });

  test('leaves unprefixed content unchanged', () {
    expect(stripEllaDisplayPrefix('🪽 Family lunch'), '🪽 Family lunch');
    expect(stripEllaDisplayPrefix('A good afternoon'), 'A good afternoon');
  });
}
