import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_audio_emission_gate.dart';
import 'package:omi/ella/services/ella_capture_uid_gate.dart';
import 'package:omi/ella/services/ella_upstream_capture_flag.dart';

void main() {
  group('hasNonEmptyBoundUid', () {
    test('rejects null and blank uids, accepts a real one', () {
      expect(hasNonEmptyBoundUid(null), isFalse);
      expect(hasNonEmptyBoundUid(''), isFalse);
      expect(hasNonEmptyBoundUid('   '), isFalse);
      expect(hasNonEmptyBoundUid('uid-a'), isTrue);
    });
  });

  group('mayEmitAudio', () {
    test('is fail-closed: every input must hold, missing input means no', () {
      expect(mayEmitAudio(boundUid: null, hasConsentAuthority: true), isFalse);
      expect(mayEmitAudio(boundUid: '', hasConsentAuthority: true), isFalse);
      expect(mayEmitAudio(boundUid: 'uid-a', hasConsentAuthority: false), isFalse);
      expect(mayEmitAudio(boundUid: null, hasConsentAuthority: false), isFalse);
      expect(mayEmitAudio(boundUid: 'uid-a', hasConsentAuthority: true), isTrue);
    });
  });

  group('ellaUpstreamCaptureFlag', () {
    test('defaults OFF when the dart-define is not supplied', () {
      // This test suite is compiled without --dart-define=ELLA_UPSTREAM_CAPTURE_ENABLED,
      // so the constant must resolve to its documented default.
      expect(isEllaUpstreamCaptureEnabled, isFalse);
      expect(ellaUpstreamCaptureFlagName, 'ELLA_UPSTREAM_CAPTURE_ENABLED');
    });

    test('selects the compile-graph path the flag value names, with no third option', () {
      expect(selectCaptureCompileGraphPath(flagOverride: false), CaptureCompileGraphPath.legacyEllaCapture);
      expect(selectCaptureCompileGraphPath(flagOverride: true), CaptureCompileGraphPath.vendoredUpstreamCapture);
      // No override falls back to the compiled-in (default OFF) constant.
      expect(selectCaptureCompileGraphPath(), CaptureCompileGraphPath.legacyEllaCapture);
    });
  });

  group('flag coherence: Dart and Swift read the same name from the same build invocation', () {
    test(
        'build-and-upload.sh derives both the dart-define and the Swift compilation condition '
        'from one ELLA_UPSTREAM_CAPTURE_ENABLED env var, default OFF', () {
      final script = File('${_appRoot().path}/ios/build-and-upload.sh').readAsStringSync();

      expect(script, contains('ELLA_UPSTREAM_CAPTURE_ENABLED="\${ELLA_UPSTREAM_CAPTURE_ENABLED:-false}"'));
      expect(script, contains(r'DART_DEFINES+=(--dart-define=ELLA_UPSTREAM_CAPTURE_ENABLED=true)'));
      expect(
        script,
        contains(r"EXTRA_XCODEBUILD_SETTINGS+=('SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) "
            r"ELLA_UPSTREAM_CAPTURE_ENABLED')"),
      );
      // Both branches must be gated by the exact same shell variable so one
      // build invocation cannot turn on one side without the other.
      final dartBranchIndex = script.indexOf(r'DART_DEFINES+=(--dart-define=ELLA_UPSTREAM_CAPTURE_ENABLED=true)');
      final swiftBranchIndex = script.indexOf('EXTRA_XCODEBUILD_SETTINGS+=');
      final guardIndex = script.lastIndexOf(
        'if [ "\$ELLA_UPSTREAM_CAPTURE_ENABLED" = "true" ] || [ "\$ELLA_UPSTREAM_CAPTURE_ENABLED" = "1" ]; then',
        dartBranchIndex,
      );
      expect(guardIndex, greaterThan(-1), reason: 'both branches must sit under the same env-var guard');
      expect(dartBranchIndex, greaterThan(guardIndex));
      expect(swiftBranchIndex, greaterThan(dartBranchIndex));
    });

    test('the Swift flag file mirrors the exact same flag name and defaults OFF', () {
      final swiftFlag = File('${_appRoot().path}/ios/Runner/EllaUpstreamCaptureFlag.swift').readAsStringSync();

      expect(swiftFlag, contains('#if $ellaUpstreamCaptureFlagName'));
      expect(swiftFlag, contains('let ellaUpstreamCaptureEnabled = true'));
      expect(swiftFlag, contains('#else'));
      expect(swiftFlag, contains('let ellaUpstreamCaptureEnabled = false'));
    });

    test('the new Swift file is registered on the Runner target compile graph, not dormant', () {
      final pbxproj = File('${_appRoot().path}/ios/Runner.xcodeproj/project.pbxproj').readAsStringSync();

      expect(pbxproj, contains('EllaUpstreamCaptureFlag.swift in Sources'));
      expect(pbxproj, contains('EllaUpstreamCaptureFlag.swift'));
    });
  });

  group('upstream capture manifest (ella-ai#1280 P1: honestly empty)', () {
    test('UPSTREAM_CAPTURE_MANIFEST.json names the recorded pin and currently guards zero paths', () {
      final repoRoot = _appRoot().parent;
      final manifest = jsonDecode(
        File('${repoRoot.path}/UPSTREAM_CAPTURE_MANIFEST.json').readAsStringSync(),
      ) as Map<String, dynamic>;

      expect(manifest['upstream_repo'], 'https://github.com/BasedHardware/omi.git');
      expect(manifest['upstream_sha'], 'f16699aea7fe9ba089baceb628922f2882c51153');
      // See UPSTREAM_PATCHES.md: every vendoring candidate this PR checked had
      // already drifted from upstream in the fork, so nothing is claimed yet.
      // This test exists so a future PR that populates `paths` here is the one
      // that has to update this expectation — not silently pass either way.
      expect(manifest['paths'], isEmpty);
    });
  });

  group('capture gates stay wired at every native-to-socket and start/resume call site', () {
    test('necklace native-to-socket boundary calls mayEmitAudio before sending', () {
      final source = File('${_appRoot().path}/lib/providers/capture_provider.dart').readAsStringSync();
      expect(
        source,
        contains(
          'if (!mayEmitAudio(boundUid: session.authority.uid, hasConsentAuthority: session.authority.isCurrent())) {',
        ),
      );
    });

    test('phone native-to-socket boundary calls mayEmitAudio before sending', () {
      final source = File('${_appRoot().path}/lib/providers/capture_provider.dart').readAsStringSync();
      expect(
        source,
        contains(
          'if (!mayEmitAudio(boundUid: captureAuthority.uid, hasConsentAuthority: captureAuthority.isCurrent())) {',
        ),
      );
    });

    test('the transcription socket send authority also delegates to mayEmitAudio', () {
      final source = File('${_appRoot().path}/lib/services/sockets/transcription_service.dart').readAsStringSync();
      expect(source, contains('bool get _hasProtectedSendAuthority => mayEmitAudio('));
    });

    test('necklace, phone, and system-audio start paths all check hasNonEmptyBoundUid', () {
      final source = File('${_appRoot().path}/lib/providers/capture_provider.dart').readAsStringSync();
      expect('hasNonEmptyBoundUid(captureAuthority.uid)'.allMatches(source).length, greaterThanOrEqualTo(3));
    });
  });
}

Directory _appRoot() {
  final current = Directory.current;
  if (File('${current.path}/pubspec.yaml').existsSync()) return current;
  final nested = Directory('${current.path}/app');
  if (File('${nested.path}/pubspec.yaml').existsSync()) return nested;
  throw StateError('Unable to locate Flutter app root from ${current.path}');
}
