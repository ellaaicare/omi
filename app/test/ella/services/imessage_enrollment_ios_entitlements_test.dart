import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('every selected Runner entitlement provisions the app Keychain access group', () {
    const entitlementPaths = [
      'ios/Runner/Runner.entitlements',
      'ios/Runner/RunnerDebug-dev.entitlements',
      'ios/Runner/RunnerDebug-prod.entitlements',
      'ios/Runner/RunnerProfile-dev.entitlements',
      'ios/Runner/RunnerProfile-prod.entitlements',
      'ios/Runner/RunnerRelease-dev.entitlements',
      'ios/Runner/RunnerRelease-prod.entitlements',
    ];
    final project = File('ios/Runner.xcodeproj/project.pbxproj').readAsStringSync();

    for (final path in entitlementPaths) {
      final contents = File(path).readAsStringSync();
      expect(contents, contains('<key>keychain-access-groups</key>'), reason: path);
      expect(
        contents,
        contains('<string>\$(AppIdentifierPrefix)\$(CFBundleIdentifier)</string>'),
        reason: path,
      );
      final selectedEntitlement = RegExp(
        'CODE_SIGN_ENTITLEMENTS = "?${RegExp.escape(path.substring(4))}"?;',
      );
      expect(project, matches(selectedEntitlement), reason: path);
    }
  });
}
