import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_invite_link_controller.dart';

void main() {
  test('extracts the canonical first-party fragment without accepting legacy URL shapes', () {
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite#c=ella-7k9q')), 'ELLA7K9Q');
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite?code=ella-7k9q')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite/ABCD-2345')), isEmpty);
  });

  test('accepts only the registered Omi custom scheme and rejects foreign invite hosts', () {
    expect(extractEllaInviteCode(Uri.parse('omi://invite/QR-77-z')), 'QR77Z');
    expect(extractEllaInviteCode(Uri.parse('ella://invite/QR-77-z')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('https://attacker.example/invite#c=SECRET-22')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/privacy')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('omi://auth/callback?code=private-oauth-code')), isEmpty);
  });

  test('prefilled invite remains pending until explicit dismissal', () {
    final controller = EllaInviteLinkController.instance;
    controller.clear();
    addTearDown(controller.clear);

    expect(controller.accept(Uri.parse('https://ella-ai-care.com/invite#c=ABCD-2345')), isTrue);
    expect(controller.pendingCode, 'ABCD2345');
    expect(controller.pendingCode, 'ABCD2345');

    controller.clear();
    expect(controller.pendingCode, isEmpty);
  });

  test('malformed fragments never produce a code', () {
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite#not-a-query')), isEmpty);
    expect(extractEllaInviteCode(Uri.parse('https://ella-ai-care.com/invite#c=%')), isEmpty);
  });
}
