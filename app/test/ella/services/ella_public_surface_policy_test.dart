import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_public_surface_policy.dart';

void main() {
  test('public builds reject inherited Omi surfaces', () {
    expect(allowsInheritedOmiSurface(isPublicBuild: true), isFalse);
  });

  test('non-public builds retain inherited Omi surfaces', () {
    expect(allowsInheritedOmiSurface(isPublicBuild: false), isTrue);
  });
}
