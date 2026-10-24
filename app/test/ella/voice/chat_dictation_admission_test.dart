import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/chat_dictation_admission.dart';

void main() {
  test('new Chat dictation never advertises or admits an unsupported session', () {
    const admission = ChatDictationAdmission();
    expect(admission.isAvailable, isFalse);
    for (var attempt = 0; attempt < 10; attempt++) {
      expect(admission.requestStart(), ChatDictationAdmissionResult.exclusiveOwnershipUnavailable);
    }
  });
}
