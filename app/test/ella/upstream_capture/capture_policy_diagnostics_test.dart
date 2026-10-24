import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/upstream_capture/backend/preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  tearDown(() => SharedPreferencesUtil.capturePolicyBridgeForTesting = null);

  test('canonical explicit mute survives initialization and diagnostic reads do not change policy', () async {
    SharedPreferences.setMockInitialValues({'capturePolicy': '{"version":1,"revision":7,"muted":true}'});
    await SharedPreferencesUtil.init();
    final prefs = SharedPreferencesUtil(), before = SharedPreferencesUtil().capturePolicy;
    expect(prefs.capturePolicyDiagnostic,
        {'load_origin': 'canonical', 'last_intent': 'none', 'muted': true, 'revision': 7});
    expect(() => prefs.capturePolicyDiagnostic['muted'] = false, throwsUnsupportedError);
    expect(prefs.capturePolicy, before);
    await SharedPreferencesUtil.init();
    expect(prefs.deviceMuted, isTrue);
    expect(prefs.capturePolicyDiagnostic['load_origin'], 'canonical');
  });
  test('malformed policy remains closed and reports fallback without exporting preference bytes', () async {
    SharedPreferences.setMockInitialValues({'capturePolicy': 'invalid fictional value'});
    await SharedPreferencesUtil.init();
    expect(SharedPreferencesUtil().deviceMuted, isTrue);
    expect(SharedPreferencesUtil().capturePolicyDiagnostic['load_origin'], 'malformed_canonical_fail_closed');
    expect(SharedPreferencesUtil().capturePolicyDiagnostic.toString(), isNot(contains('invalid fictional')));
  });
  test('legacy mute migrates without implicit unmute', () async {
    SharedPreferences.setMockInitialValues({'deviceMuted': true, 'batchMuted': false});
    await SharedPreferencesUtil.init();
    expect(SharedPreferencesUtil().deviceMuted, isTrue);
    expect(SharedPreferencesUtil().capturePolicyDiagnostic['load_origin'], 'legacy_migration');
  });
  test('newer native revision remains closed until explicit durable unmute', () async {
    SharedPreferences.setMockInitialValues({'capturePolicy': '{"version":1,"revision":2,"muted":false}'});
    final effects = <String>[];
    SharedPreferencesUtil.capturePolicyBridgeForTesting = (method, args) async {
      effects.add(method);
      return method == 'getRevision' ? 9 : null;
    };
    await SharedPreferencesUtil.init();
    expect(SharedPreferencesUtil().capturePolicyDiagnostic['load_origin'], 'native_revision_newer_fail_closed');
    expect(SharedPreferencesUtil().deviceMuted, isTrue);
    expect(effects, ['getRevision']);
  });
}
