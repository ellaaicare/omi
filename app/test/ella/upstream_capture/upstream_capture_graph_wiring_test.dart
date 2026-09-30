// ellaaicare/ella-ai#1280 items 2 & 3: one activation setting drives both the
// native (Swift) and Dart graphs.
//
// Dart side (real import-graph walk over lib/):
//   * OFF: no file under lib/upstream_capture/ or lib/ella/upstream_capture/ is
//     reachable from any existing entry point (lib/main.dart);
//   * ON: lib/main_upstream_capture.dart reaches the vendored controller, the
//     provider, the native mic recorder, the Pigeon bindings, the Ella gates.
//
// Native side (text-level; Xcode cannot run here — see app/lib/upstream_capture/README.md
// for the local Xcode verification that must accompany this):
//   * EllaUpstreamCapture.xcconfig holds the single value (default NO) and derives
//     EXCLUDED_SOURCE_FILE_NAMES / SWIFT_ACTIVE_COMPILATION_CONDITIONS / GCC defines;
//   * every Runner build configuration chain includes it; every override keeps $(inherited);
//   * every vendored native source is a Runner Compile Sources member and is excluded when NO;
//   * AppDelegate registers the Pigeon hosts only under #if ELLA_UPSTREAM_CAPTURE_ENABLED_YES;
//   * the build script derives the Dart define JSON + entry point from the same value.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';

final _app = Directory.current.path; // flutter test runs in app/
final _import = RegExp(r'''^\s*(?:import|export|part)\s+['"]([^'"]+)['"]([^;]*);''', multiLine: true);
final _conditional = RegExp(r'''if\s*\([^)]*\)\s*['"]([^'"]+)['"]''');

String? _resolve(String fromFile, String uri) {
  if (uri.startsWith('dart:')) return null;
  if (uri.startsWith('package:omi/')) return '$_app/lib/${uri.substring('package:omi/'.length)}';
  if (uri.startsWith('package:')) return null;
  return File('${File(fromFile).parent.path}/$uri').absolute.uri.normalizePath().toFilePath();
}

Set<String> _reachable(String entry) {
  final seen = <String>{};
  final queue = <String>[File('$_app/$entry').absolute.path];
  while (queue.isNotEmpty) {
    final path = queue.removeLast();
    if (!seen.add(path)) continue;
    final file = File(path);
    if (!file.existsSync()) continue;
    final source = file.readAsStringSync();
    for (final match in _import.allMatches(source)) {
      final uris = [match.group(1)!, ..._conditional.allMatches(match.group(2)!).map((m) => m.group(1)!)];
      for (final uri in uris) {
        final target = _resolve(path, uri);
        if (target != null && !seen.contains(target)) queue.add(target);
      }
    }
  }
  return seen.map((p) => p.substring(_app.length + 1)).toSet();
}

bool _isUpstreamGraph(String rel) =>
    rel.startsWith('lib/upstream_capture/') ||
    rel.startsWith('lib/ella/upstream_capture/') ||
    rel == 'lib/main_upstream_capture.dart';

String _read(String rel) => File('$_app/$rel').readAsStringSync();

List<String> _vendoredNativeSources() => _read('lib/upstream_capture/UPSTREAM_OWNED.txt')
    .split('\n')
    .where((l) => l.startsWith('verbatim\t') || l.startsWith('patched\t'))
    .map((l) => l.split('\t')[3])
    .where((p) => p.startsWith('app/ios/Runner/'))
    .toList();

void main() {
  group('production service bootstrap retry', () {
    test('recovers a partial singleton initialization after connectivity failure', () async {
      var singletonInstalled = false;
      var managerAttempts = 0;
      var connectivityAttempts = 0;
      final bootstrap = EllaUpstreamServicesBootstrap(
        initializeManager: () async {
          managerAttempts++;
          if (!singletonInstalled) {
            singletonInstalled = true;
            throw StateError('connectivity initialization failed after singleton install');
          }
          throw StateError('Service manager is initiated');
        },
        managerExists: () => singletonInstalled,
        initializeConnectivity: () async {
          connectivityAttempts++;
          if (connectivityAttempts == 1) throw StateError('connectivity still unavailable');
        },
      );

      await expectLater(bootstrap.ensureInitialized(), throwsStateError);
      await bootstrap.ensureInitialized();
      await bootstrap.ensureInitialized();
      expect(managerAttempts, 2);
      expect(connectivityAttempts, 2);
    });

    test('does not mask initialization failure before the singleton exists', () async {
      var connectivityAttempts = 0;
      final bootstrap = EllaUpstreamServicesBootstrap(
        initializeManager: () async => throw StateError('manager construction failed'),
        managerExists: () => false,
        initializeConnectivity: () async {
          connectivityAttempts++;
        },
      );
      await expectLater(bootstrap.ensureInitialized(), throwsStateError);
      expect(connectivityAttempts, 0);
    });
  });

  group('Dart graph (flag OFF / ON)', () {
    test('lib/main.dart (flag OFF entry point) reaches no upstream-capture code', () {
      final reachable = _reachable('lib/main.dart');
      expect(reachable, contains('lib/pages/home/today_page.dart'), reason: 'sanity: the walk follows imports');
      expect(reachable, contains('lib/ella/capture_host/ella_capture_host.dart'));
      final leaked = reachable.where(_isUpstreamGraph).toList()..sort();
      expect(leaked, isEmpty, reason: 'flag-OFF graph must not reach: $leaked');
    });

    test('every other existing entry point is also free of upstream-capture code', () {
      final entries = Directory('$_app/lib')
          .listSync()
          .whereType<File>()
          .where(
            (f) =>
                f.path.endsWith('.dart') &&
                RegExp(r'^(void|Future<void>)\s+main\(', multiLine: true).hasMatch(f.readAsStringSync()),
          )
          .map((f) => f.path.substring(_app.length + 1))
          .where((rel) => rel != 'lib/main_upstream_capture.dart')
          .toList();
      expect(entries, contains('lib/main.dart'));
      for (final entry in entries) {
        expect(_reachable(entry).where(_isUpstreamGraph), isEmpty, reason: entry);
      }
    });

    test('the host registration file is import-free of the upstream graph', () {
      final host = _read('lib/ella/capture_host/ella_capture_host.dart');
      final imports = _import.allMatches(host).map((m) => m.group(1)!).toList();
      expect(imports, isNotEmpty);
      expect(imports.where((uri) => uri.contains('upstream_capture')), isEmpty);
    });

    test('lib/main_upstream_capture.dart (flag ON entry point) wires the upstream stack and the Ella gates', () {
      final reachable = _reachable('lib/main_upstream_capture.dart');
      for (final required in [
        'lib/main.dart',
        'lib/ella/capture_host/ella_capture_host.dart',
        'lib/ella/upstream_capture/ella_upstream_capture_runtime.dart',
        'lib/ella/upstream_capture/ella_upstream_capture_dock.dart',
        'lib/ella/upstream_capture/ella_upstream_device_service_adapter.dart',
        'lib/ella/upstream_capture/ella_gated_capture_seams.dart',
        'lib/ella/upstream_capture/ella_gated_device_connection.dart',
        'lib/ella/upstream_capture/ella_capture_authority.dart',
        'lib/ella/services/ella_audio_emission_gate.dart',
        'lib/upstream_capture/providers/capture_provider.dart',
        'lib/upstream_capture/services/capture/capture_controller.dart',
        'lib/upstream_capture/services/capture/capture_coordinator.dart',
        'lib/upstream_capture/services/capture/capture_seams.dart',
        'lib/upstream_capture/services/services.dart',
        'lib/upstream_capture/services/devices.dart',
        'lib/upstream_capture/services/devices/transports/native_ble_transport.dart',
        'lib/upstream_capture/services/mic/native_mic_recorder_service.dart',
        'lib/upstream_capture/services/mic/mic_arbiter.dart',
        'lib/upstream_capture/services/sockets.dart',
        'lib/upstream_capture/services/wals.dart',
        'lib/upstream_capture/services/wals/recording_transfer_coordinator.dart',
        'lib/upstream_capture/services/bridges/ble_bridge.dart',
        'lib/upstream_capture/gen/phone_mic_pigeon.g.dart',
        'lib/upstream_capture/gen/pigeon_communicator.g.dart',
      ]) {
        expect(reachable, contains(required));
      }
      final entry = _read('lib/main_upstream_capture.dart');
      expect(entry, contains('EllaCaptureHost.installUpstreamCapture('));
      expect(entry, contains('EllaUpstreamDeviceServiceAdapter.production(runtime)'));
      expect(entry, contains('EllaAccountIsolationService.registerCaptureProducer('));
      expect(entry, contains('legacy.main()'));
    });

    test('the Dart activation refuses to install unless the define from the xcconfig is true', () {
      expect(EllaCaptureHost.upstreamCaptureDefine, isFalse, reason: 'tests run with the default OFF define');
      expect(
        () => EllaCaptureHost.installUpstreamCapture(homeCaptureDockBuilder: (_) => const SizedBox()),
        throwsStateError,
      );
      expect(EllaCaptureHost.upstreamCaptureActive, isFalse);
      expect(EllaCaptureHost.legacyCaptureSuppressed, isFalse);
    });

    test('flag-ON registration swaps the home dock and suppresses the legacy capture starters', () {
      addTearDown(EllaCaptureHost.resetForTesting);
      EllaCaptureHost.installForTesting(homeCaptureDockBuilder: (_) => const SizedBox());
      expect(EllaCaptureHost.upstreamCaptureActive, isTrue);
      expect(EllaCaptureHost.legacyCaptureSuppressed, isTrue);
      final home = _read('lib/pages/home/today_page.dart');
      expect(home, contains('final upstreamDockBuilder = EllaCaptureHost.homeCaptureDockBuilder;'));
      expect(home, contains('SliverToBoxAdapter(child: upstreamDockBuilder(context))'));
      expect(home, contains(': TodayRecordMomentControl('));
      expect(
        _read('lib/providers/device_provider.dart'),
        contains('if (EllaCaptureHost.legacyCaptureSuppressed) return;'),
      );
      expect(_read('lib/main.dart'), contains('if (!EllaCaptureHost.legacyCaptureSuppressed) {'));
      expect(_read('lib/main.dart'), contains('ServiceManager.init(deviceService: EllaCaptureHost.deviceService)'));
      expect(_read('lib/main.dart'), contains('ServiceManager.instance().device.start()'));
    });
  });

  group('native graph (text-level)', () {
    const xcconfigPath = 'ios/Flutter/EllaUpstreamCapture.xcconfig';
    final xcconfig = _read(xcconfigPath);
    final settings =
        xcconfig.split('\n').map((l) => l.replaceFirst(RegExp(r'//.*$'), '').trim()).where((l) => l.isNotEmpty);

    test('one activation setting, default NO', () {
      final assignments = settings.where((l) => RegExp(r'^ELLA_UPSTREAM_CAPTURE_ENABLED\s*=').hasMatch(l)).toList();
      expect(assignments, ['ELLA_UPSTREAM_CAPTURE_ENABLED = NO']);
    });

    test('the same value drives source exclusion, the Swift #if condition and the C define', () {
      expect(
        settings,
        contains(
          r'EXCLUDED_SOURCE_FILE_NAMES = $(inherited) $(ELLA_UPSTREAM_CAPTURE_EXCLUDED_$(ELLA_UPSTREAM_CAPTURE_ENABLED))',
        ),
      );
      expect(
        settings,
        contains(
          r'SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) ELLA_UPSTREAM_CAPTURE_ENABLED_$(ELLA_UPSTREAM_CAPTURE_ENABLED)',
        ),
      );
      expect(
        settings,
        contains(
          r'GCC_PREPROCESSOR_DEFINITIONS = $(inherited) ELLA_UPSTREAM_CAPTURE_ENABLED_$(ELLA_UPSTREAM_CAPTURE_ENABLED)=1',
        ),
      );
      expect(settings, contains(r'ELLA_UPSTREAM_CAPTURE_EXCLUDED_NO = $(ELLA_UPSTREAM_CAPTURE_NATIVE_SOURCES)'));
      expect(settings, contains('ELLA_UPSTREAM_CAPTURE_EXCLUDED_YES = FlutterCommunicator.g.swift'));
    });

    test('flag OFF excludes exactly the vendored native sources', () {
      final line = settings.firstWhere((l) => l.startsWith('ELLA_UPSTREAM_CAPTURE_NATIVE_SOURCES'));
      final excluded = line.split('=')[1].trim().split(RegExp(r'\s+')).toSet();
      final vendored = _vendoredNativeSources().map((p) => p.split('/').last).toSet();
      expect(excluded, vendored);
      // Basenames must be unique in the Runner tree, or exclusion would hit fork files.
      final runnerNames = Directory(
        '$_app/ios/Runner',
      ).listSync(recursive: true).whereType<File>().map((f) => f.path.split('/').last).toList();
      for (final name in excluded) {
        expect(runnerNames.where((n) => n == name), hasLength(1), reason: name);
      }
    });

    test('every Runner xcconfig includes the activation xcconfig', () {
      for (final name in [
        'Debug',
        'Release',
        'devDebug',
        'devProfile',
        'devRelease',
        'prodDebug',
        'prodProfile',
        'prodRelease',
      ]) {
        expect(_read('ios/Flutter/$name.xcconfig'), contains('#include "EllaUpstreamCapture.xcconfig"'), reason: name);
      }
    });

    test('pbxproj: vendored sources are Runner Compile Sources members; overrides keep \$(inherited)', () {
      final pbx = _read('ios/Runner.xcodeproj/project.pbxproj');
      final runnerSources = RegExp(
        r'97C146EA1CF9000F007C117D /\* Sources \*/ = \{[^}]*?files = \(([^)]*)\);',
        dotAll: true,
      ).firstMatch(pbx)!.group(1)!;
      for (final path in _vendoredNativeSources()) {
        final name = path.split('/').last;
        expect(pbx, contains('path = $name;'), reason: '$name file reference');
        if (name.endsWith('.h')) continue;
        expect(runnerSources, contains('/* $name in Sources */'), reason: '$name must compile in Runner');
      }
      expect(runnerSources, contains('/* EllaUpstreamCaptureNativeHost.swift in Sources */'));
      expect(runnerSources, contains('/* FlutterCommunicator.g.swift in Sources */'));
      expect(
        pbx,
        isNot(contains('"EXCLUDED_SOURCE_FILE_NAMES[sdk=iphonesimulator*]" = omiWatchApp.app;')),
        reason: r'a simulator override without $(inherited) would drop the flag-OFF exclusion',
      );
      for (final key in ['EXCLUDED_SOURCE_FILE_NAMES', 'SWIFT_ACTIVE_COMPILATION_CONDITIONS']) {
        for (final match in RegExp('"?$key(?:\\[[^\\]]*\\])?"? = ([^;]*);').allMatches(pbx)) {
          expect(match.group(1), contains(r'$(inherited)'), reason: match.group(0));
        }
      }
      for (final match in RegExp(r'GCC_PREPROCESSOR_DEFINITIONS = \(([^)]*)\);', dotAll: true).allMatches(pbx)) {
        expect(match.group(1), contains(r'$(inherited)'), reason: match.group(0));
      }
    });

    test('AppDelegate registers the upstream Pigeon hosts only under the ON condition', () {
      final delegate = _read('ios/Runner/AppDelegate.swift');
      final blocks = RegExp(
        r'#if ELLA_UPSTREAM_CAPTURE_ENABLED_YES\n(.*?)#endif',
        dotAll: true,
      ).allMatches(delegate).toList();
      expect(
        blocks.map((m) => m.group(1)!).join(),
        contains('EllaUpstreamCaptureNativeHost.shared.register(binaryMessenger: controller.binaryMessenger)'),
      );
      final outside = delegate.replaceAll(
        RegExp(r'#if ELLA_UPSTREAM_CAPTURE_ENABLED_YES\n.*?#endif', dotAll: true),
        '',
      );
      for (final symbol in [
        'EllaUpstreamCaptureNativeHost',
        'OmiBleManager',
        'BleHostApiSetup',
        'PhoneMicHostApiSetup',
        'PhoneMicController',
      ]) {
        expect(outside, isNot(contains(symbol)), reason: '$symbol must only be referenced under the flag');
      }

      final host = _read('ios/Runner/EllaUpstreamCaptureNativeHost.swift');
      final code = host.split('\n').where((l) => !l.trimLeft().startsWith('//') && l.trim().isNotEmpty).toList();
      expect(code.first, '#if ELLA_UPSTREAM_CAPTURE_ENABLED_YES');
      expect(code.last, '#endif');
      for (final call in [
        'OmiBleManager.shared.setFlutterApi(bleFlutterApi)',
        'BleHostApiSetup.setUp(binaryMessenger: messenger, api: BleHostApiImpl(bleManager: OmiBleManager.shared))',
        'PhoneMicHostApiSetup.setUp(binaryMessenger: messenger, api: PhoneMicHostApiImpl(controller: micController))',
        'PhoneMicController(environment: PhoneMicLiveEnvironment.make(sink: phoneMicFlutterApi))',
        'name: "com.omi/capture_policy"',
        'name: "com.friend.ios/sync_transfer"',
      ]) {
        expect(host, contains(call));
      }
      expect(_read('ios/Runner/Runner-Bridging-Header.h'), contains('#import "PhoneMic/PhoneMicOpusShim.h"'));
    });

    test('Dart channels used by the vendored stack have ON-mode native hosts', () {
      final host = _read('ios/Runner/EllaUpstreamCaptureNativeHost.swift');
      expect(
        _read('lib/upstream_capture/backend/preferences.dart'),
        contains("MethodChannel('com.omi/capture_policy')"),
      );
      expect(host, contains('"com.omi/capture_policy"'));
      expect(
        _read('lib/upstream_capture/services/wals/sync_transfer_keep_alive.dart'),
        contains("'com.friend.ios/sync_transfer'"),
      );
      expect(host, contains('"com.friend.ios/sync_transfer"'));
    });

    Future<Map<String, String>> runConfig(String value) async {
      final tmp = await Directory.systemTemp.createTemp('ella_upstream_build_config_');
      addTearDown(() => tmp.deleteSync(recursive: true));
      Directory('${tmp.path}/app/ios/Flutter').createSync(recursive: true);
      Directory('${tmp.path}/app/ios/scripts').createSync(recursive: true);
      File('${tmp.path}/app/ios/Flutter/EllaUpstreamCapture.xcconfig').writeAsStringSync(
        xcconfig.replaceFirst('ELLA_UPSTREAM_CAPTURE_ENABLED = NO', 'ELLA_UPSTREAM_CAPTURE_ENABLED = $value'),
      );
      File('$_app/ios/scripts/ella_upstream_capture_build_config.sh').copySync('${tmp.path}/app/ios/scripts/cfg.sh');
      final out = '${tmp.path}/defines.json';
      final result = await Process.run('bash', ['${tmp.path}/app/ios/scripts/cfg.sh', out]);
      expect(result.exitCode, 0, reason: '${result.stdout}${result.stderr}');
      final vars = <String, String>{
        for (final line in (result.stdout as String).trim().split('\n'))
          line.substring(0, line.indexOf('=')): line.substring(line.indexOf('=') + 1),
      };
      vars['json'] = File(out).readAsStringSync();
      return vars;
    }

    test('build script derives the Dart define JSON + entry point from the xcconfig (OFF)', () async {
      final vars = await runConfig('NO');
      expect(vars['ELLA_UPSTREAM_CAPTURE_ENABLED'], 'NO');
      expect(vars['ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET'], 'lib/main.dart');
      expect(vars['json'], contains('"ELLA_UPSTREAM_CAPTURE_ENABLED": false'));
    });

    test('build script derives the Dart define JSON + entry point from the xcconfig (ON)', () async {
      final vars = await runConfig('YES');
      expect(vars['ELLA_UPSTREAM_CAPTURE_ENABLED'], 'YES');
      expect(vars['ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET'], 'lib/main_upstream_capture.dart');
      expect(vars['json'], contains('"ELLA_UPSTREAM_CAPTURE_ENABLED": true'));
    });

    test('build script rejects anything but a single YES/NO value', () async {
      final tmp = await Directory.systemTemp.createTemp('ella_upstream_build_config_bad_');
      addTearDown(() => tmp.deleteSync(recursive: true));
      Directory('${tmp.path}/app/ios/Flutter').createSync(recursive: true);
      Directory('${tmp.path}/app/ios/scripts').createSync(recursive: true);
      File('$_app/ios/scripts/ella_upstream_capture_build_config.sh').copySync('${tmp.path}/app/ios/scripts/cfg.sh');
      for (final body in [
        'ELLA_UPSTREAM_CAPTURE_ENABLED = maybe\n',
        'ELLA_UPSTREAM_CAPTURE_ENABLED = NO\nELLA_UPSTREAM_CAPTURE_ENABLED = YES\n',
        '',
      ]) {
        File('${tmp.path}/app/ios/Flutter/EllaUpstreamCapture.xcconfig').writeAsStringSync(body);
        final result = await Process.run('bash', ['${tmp.path}/app/ios/scripts/cfg.sh', '${tmp.path}/x.json']);
        expect(result.exitCode, isNot(0), reason: body);
      }
    });

    test('the release build script consumes the derived values (no second flag)', () {
      final script = _read('ios/build-and-upload.sh');
      expect(script, contains('ella_upstream_capture_build_config.sh'));
      expect(script, contains(r'--dart-define-from-file="$ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE"'));
      expect(script, contains(r'FLUTTER_TARGET_ARGS=(-t "$ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET")'));
      expect(script, contains(r'"${FLUTTER_TARGET_ARGS[@]}"'));
      expect(script, isNot(contains('--dart-define=ELLA_UPSTREAM_CAPTURE_ENABLED')));
    });

    Future<Map<String, dynamic>> runCloudBuild(String? config) async {
      final tmp = await Directory.systemTemp.createTemp('ella cloud build ');
      addTearDown(() => tmp.deleteSync(recursive: true));
      final app = '${tmp.path}/repo with spaces/app';
      final home = '${tmp.path}/home with spaces';
      for (final path in ['$app/ios/Flutter', '$app/ios/scripts', '$app/ios/ci_scripts', '$home/flutter/bin']) {
        Directory(path).createSync(recursive: true);
      }
      if (config != null) {
        File('$app/ios/Flutter/EllaUpstreamCapture.xcconfig').writeAsStringSync(config);
      }
      for (final script in ['scripts/ella_upstream_capture_build_config.sh', 'ci_scripts/ci_pre_xcodebuild.sh']) {
        File('$_app/ios/$script').copySync('$app/ios/$script');
      }
      final flutter = File('$home/flutter/bin/flutter')..writeAsStringSync(r'''#!/bin/bash
set -e
printf '%s\n' "$@" > "$FLUTTER_ARGUMENTS_FILE"
pwd > "$FLUTTER_WORKING_DIRECTORY_FILE"
''');
      final chmod = await Process.run('/bin/chmod', ['+x', flutter.path]);
      expect(chmod.exitCode, 0, reason: '${chmod.stdout}${chmod.stderr}');
      final arguments = File('${tmp.path}/arguments.txt');
      final workingDirectory = File('${tmp.path}/working-directory.txt');
      final result = await Process.run(
        '/bin/bash',
        ['$app/ios/ci_scripts/ci_pre_xcodebuild.sh'],
        includeParentEnvironment: false,
        environment: {
          'HOME': home,
          'PATH': '/usr/bin:/bin',
          'CI_PRIMARY_REPOSITORY_PATH': '${tmp.path}/repo with spaces',
          'FLUTTER_ARGUMENTS_FILE': arguments.path,
          'FLUTTER_WORKING_DIRECTORY_FILE': workingDirectory.path,
          'ELLA_UPSTREAM_CAPTURE_ENABLED': 'untrusted ambient override',
        },
      );
      return {
        'result': result,
        'app': app,
        'arguments': arguments.existsSync() ? arguments.readAsLinesSync() : null,
        'workingDirectory': workingDirectory.existsSync() ? workingDirectory.readAsStringSync().trim() : null,
      };
    }

    for (final enabled in [false, true]) {
      test('Xcode Cloud executes the shared build config with capture ${enabled ? 'ON' : 'OFF'}', () async {
        final cloud = await runCloudBuild('ELLA_UPSTREAM_CAPTURE_ENABLED = ${enabled ? 'YES' : 'NO'}\n');
        final result = cloud['result'] as ProcessResult;
        expect(result.exitCode, 0, reason: '${result.stdout}${result.stderr}');
        final arguments = cloud['arguments'] as List<String>;
        final defineArgument = arguments.last;
        expect(defineArgument, startsWith('--dart-define-from-file='));
        final definePath = defineArgument.substring('--dart-define-from-file='.length);
        final expectedDefineFile = File('${cloud['app']}/build/ella_upstream_capture_dart_defines.json');
        expect(File(definePath).resolveSymbolicLinksSync(), expectedDefineFile.resolveSymbolicLinksSync());
        expect(arguments, [
          'build',
          'ios',
          '--flavor',
          'prod',
          '--release',
          '--no-codesign',
          '--dart-define=ELLA_PUBLIC_BUILD=true',
          '-t',
          enabled ? 'lib/main_upstream_capture.dart' : 'lib/main.dart',
          '--dart-define-from-file=$definePath',
        ]);
        expect(jsonDecode(expectedDefineFile.readAsStringSync()), {'ELLA_UPSTREAM_CAPTURE_ENABLED': enabled});
        expect(
          Directory(cloud['workingDirectory'] as String).resolveSymbolicLinksSync(),
          Directory(cloud['app'] as String).resolveSymbolicLinksSync(),
        );
      });
    }

    test('Xcode Cloud never invokes Flutter when the shared config cannot be derived', () async {
      for (final config in [
        'ELLA_UPSTREAM_CAPTURE_ENABLED = maybe\n',
        'ELLA_UPSTREAM_CAPTURE_ENABLED = NO\nELLA_UPSTREAM_CAPTURE_ENABLED = YES\n',
        '',
        null,
      ]) {
        final cloud = await runCloudBuild(config);
        final result = cloud['result'] as ProcessResult;
        expect(result.exitCode, isNot(0), reason: 'config: $config\n${result.stdout}${result.stderr}');
        expect(cloud['arguments'], isNull, reason: 'Flutter must not run with invalid or missing config');
        expect(cloud['workingDirectory'], isNull);
      }
    });
  });
}
