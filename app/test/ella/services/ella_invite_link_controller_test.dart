import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_invite_link_controller.dart';

void main() {
  test('extracts and normalizes universal-link, QR path, and custom-scheme invite codes', () {
    expect(
      extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite?code=ella-7k9q')),
      'ELLA7K9Q',
    );
    expect(
      extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite/ABCD-2345')),
      'ABCD2345',
    );
    expect(
      extractEllaInviteCode(Uri.parse('ella://invite/QR-77-z')),
      'QR77Z',
    );
  });

  test('does not consume unrelated deep links', () {
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/privacy')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('omi://auth/callback?code=private-oauth-code')), isEmpty);
  });
}
