// PREPARED, NOT RUN. Uses only the actual retained public policy, no receipt,
// UID, profile binding, grant, authority synthesis, or network transport.
import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';

void main() {
  test('retained LAB server policy matches shipped Main962 bundled policy', () {
    final json = jsonDecode(File('test/fixtures/retained-server-policy.json')
        .readAsStringSync()) as Map<String, dynamic>;
    final policy = AiConsentPolicy.fromJson(json);
    expect(policy.isBundledCurrent, isTrue);
    expect(policy.isSupportedOperational, isTrue);
    expect(policy.processors.length, 19);
  });
}
