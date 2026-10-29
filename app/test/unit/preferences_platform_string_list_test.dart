import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';

class _Documents extends PathProviderPlatform {
  _Documents(this.path);
  final String path;
  @override
  Future<String> getApplicationDocumentsPath() async => path;
}

/// On a device, a stored string list comes back from the platform store as List<Object?>.
/// Those lists must survive a relaunch (manual disconnects, paired devices, pending uploads).
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late Directory directory;
  late PathProviderPlatform previousProvider;
  setUp(() async {
    directory = await Directory.systemTemp.createTemp('prefs-string-list-');
    previousProvider = PathProviderPlatform.instance;
    PathProviderPlatform.instance = _Documents(directory.path);
  });
  tearDown(() async {
    PathProviderPlatform.instance = previousProvider;
    await directory.delete(recursive: true);
  });

  test('platform-shaped string lists are read, not quarantined', () async {
    SharedPreferences.setMockInitialValues({
      'manualBleDisconnects': <Object?>['E564A1C1'],
      'pairedDeviceIds:uid-1': <Object?>['E564A1C1'],
      'flash_page_pending_uploads': <Object?>['upload-1'],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().getStringList('manualBleDisconnects'), ['E564A1C1']);
    expect(SharedPreferencesUtil().getStringList('pairedDeviceIds:uid-1'), ['E564A1C1']);
    expect(SharedPreferencesUtil().getStringList('flash_page_pending_uploads'), ['upload-1']);
    await Future<void>.delayed(Duration.zero);

    final stored = await SharedPreferences.getInstance();
    expect(stored.getKeys().where((key) => key.contains('.corrupt-')), isEmpty);
    expect(stored.getStringList('manualBleDisconnects'), ['E564A1C1']);
  });

  test('a list with a non-string element is still quarantined', () async {
    SharedPreferences.setMockInitialValues({
      'manualBleDisconnects': <Object?>['E564A1C1', 7],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().getStringList('manualBleDisconnects'), isEmpty);
    await Future<void>.delayed(Duration.zero);
    final stored = await SharedPreferences.getInstance();
    expect(stored.containsKey('manualBleDisconnects'), isFalse);
    expect(stored.getKeys().any((key) => key.startsWith('manualBleDisconnects.corrupt-')), isTrue);
  });
}
