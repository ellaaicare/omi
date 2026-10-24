import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/diagnostic_presentation.dart';

void main() {
  setUpAll(() => initializeDateFormatting());
  test('readable timestamp retains exact UTC and explicit local offset', () {
    final time = DateTime.utc(2026, 10, 9, 21, 52, 3, 123, 456);
    final display = diagnosticTimestamp(time, 'en');
    expect(display, contains(RegExp(r'\(UTC[+-]\d{2}:\d{2}\)\nraw_utc=')));
    expect(display, endsWith(time.toIso8601String()));
    expect(display, isNot(contains(r'\n')));
  });
  test('support clipboard excludes owner identifiers and arbitrary payloads', () {
    final fields = supportVoiceFields({
      'http_status': 200,
      'voice_mode': 'off',
      'app_build': '1.0.572+915',
      'account_sha256': 'private-hash',
      'uid': 'private-owner',
      'token': 'private-token',
      'unexpected': 'private-data',
    });
    expect(fields, {'http_status': 200, 'voice_mode': 'off', 'app_build': '1.0.572+915'});
  });
  for (final dark in [false, true]) {
    test('discovery secondary text contrast meets 4.5 dark=$dark', () {
      final palette = EllaPalette(dark);
      for (final background in [palette.bgPrimary, palette.bgSecondary]) {
        final a = palette.textSecondary.computeLuminance();
        final b = background.computeLuminance();
        expect((a > b ? (a + .05) / (b + .05) : (b + .05) / (a + .05)), greaterThanOrEqualTo(4.5));
      }
    });
  }
}
