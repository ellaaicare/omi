// ellaaicare/ella-ai#1280 items 1 & 5: every upstream-owned file equals
// BasedHardware/omi@f16699a except for the mechanical import relocation.
//
// Two independent checks:
//  1. runs scripts/verify_upstream_capture_identity.py (offline blob ids; online
//     byte diff too when the pin commit object is present locally);
//  2. re-implements the same check in Dart (inverse relocation + git blob id)
//     and proves it rejects any change other than the relocation.
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';

const _pin = 'f16699aea7fe9ba089baceb628922f2882c51153';
final _repoRoot = Directory.current.parent.path; // flutter test runs in app/
final _manifest = File('$_repoRoot/app/lib/upstream_capture/UPSTREAM_OWNED.txt');
final _quotedPackageUri = RegExp(r'''(['"])package:omi/([^'"\s]+)\1''');

class _Entry {
  _Entry(this.kind, this.blob, this.upstreamPath, this.localPath);

  final String kind;
  final String blob;
  final String upstreamPath;
  final String localPath;
}

List<_Entry> _entries() => _manifest
    .readAsLinesSync()
    .where((line) => line.isNotEmpty && !line.startsWith('#') && !line.startsWith('pin '))
    .map((line) => line.split('\t'))
    .map((p) => _Entry(p[0], p[1], p[2], p[3]))
    .toList();

Set<String> _relocatedRels(List<_Entry> entries) => entries
    .where((e) => e.kind == 'dart-relocated' && e.upstreamPath.startsWith('app/lib/'))
    .map((e) => e.upstreamPath.substring('app/lib/'.length))
    .toSet();

String _unrelocate(String source, Set<String> rels) => source.replaceAllMapped(_quotedPackageUri, (m) {
      final rel = m.group(2)!;
      const prefix = 'upstream_capture/';
      if (rel.startsWith(prefix) && rels.contains(rel.substring(prefix.length))) {
        return '${m.group(1)}package:omi/${rel.substring(prefix.length)}${m.group(1)}';
      }
      return m.group(0)!;
    });

String _gitBlobId(List<int> bytes) => sha1.convert([...utf8.encode('blob ${bytes.length}'), 0, ...bytes]).toString();

/// True when [localBytes] is the relocated form of the upstream blob [blob].
bool _matchesPin(_Entry entry, List<int> localBytes, Set<String> rels) {
  final original =
      entry.kind == 'dart-relocated' ? utf8.encode(_unrelocate(utf8.decode(localBytes), rels)) : localBytes;
  return _gitBlobId(original) == entry.blob;
}

void main() {
  test('manifest pins BasedHardware/omi at f16699a and lists the vendored stack', () {
    final text = _manifest.readAsStringSync();
    expect(text, contains('pin $_pin'));
    final entries = _entries();
    final upstream = entries.map((e) => e.upstreamPath).toSet();
    for (final required in [
      'app/lib/services/capture/capture_composition.dart',
      'app/lib/services/capture/capture_controller.dart',
      'app/lib/services/capture/capture_coordinator.dart',
      'app/lib/services/capture/capture_seams.dart',
      'app/lib/providers/capture_provider.dart',
      'app/lib/services/devices.dart',
      'app/lib/services/devices/connectors/omi_connection.dart',
      'app/lib/services/mic/native_mic_recorder_service.dart',
      'app/lib/services/mic/mic_arbiter.dart',
      'app/lib/services/sockets.dart',
      'app/lib/services/wals.dart',
      'app/lib/services/wals/wal_interfaces.dart',
      'app/lib/services/wals/recording_transfer_coordinator.dart',
      'app/lib/services/audio_sources/audio_source.dart',
      'app/lib/services/bridges/ble_bridge.dart',
      'app/lib/gen/phone_mic_pigeon.g.dart',
      'app/lib/gen/pigeon_communicator.g.dart',
      'app/ios/Runner/Ble/OmiBleManager.swift',
      'app/ios/Runner/Ble/OmiBleConnectionPolicy.swift',
      'app/ios/Runner/Ble/OmiBleDiscoveryNaming.swift',
      'app/ios/Runner/Ble/OmiBleEnergyPolicy.swift',
      'app/ios/Runner/Ble/OmiBlePairingPolicy.swift',
      'app/ios/Runner/Ble/BleHostApiImpl.swift',
      'app/ios/Runner/PhoneMic/PhoneMicHostApiImpl.swift',
      'app/ios/Runner/PhoneMic/PhoneMicPigeon.g.swift',
    ]) {
      expect(upstream, contains(required));
    }
    expect(entries.where((e) => e.kind == 'patched'), isEmpty, reason: 'no upstream-owned file is patched');
  });

  test('scripts/verify_upstream_capture_identity.py passes on this checkout', () async {
    final result = await Process.run('python3', ['$_repoRoot/scripts/verify_upstream_capture_identity.py']);
    expect(result.exitCode, 0, reason: '${result.stdout}\n${result.stderr}');
    expect(result.stdout as String, contains('UPSTREAM CAPTURE IDENTITY: OK'));
  });

  test('Dart re-check: every upstream-owned file is the pin blob modulo the import relocation', () {
    final entries = _entries();
    final rels = _relocatedRels(entries);
    final mismatches = <String>[];
    for (final entry in entries) {
      final file = File('$_repoRoot/${entry.localPath}');
      if (!file.existsSync() || !_matchesPin(entry, file.readAsBytesSync(), rels)) mismatches.add(entry.localPath);
    }
    expect(mismatches, isEmpty);
    expect(entries.length, greaterThan(190));
  });

  group('the identity check rejects everything except the mechanical relocation', () {
    late _Entry controller;
    late List<int> bytes;
    late Set<String> rels;

    setUp(() {
      final entries = _entries();
      rels = _relocatedRels(entries);
      controller = entries.firstWhere((e) => e.upstreamPath == 'app/lib/services/capture/capture_controller.dart');
      bytes = File('$_repoRoot/${controller.localPath}').readAsBytesSync();
      expect(_matchesPin(controller, bytes, rels), isTrue);
    });

    test('a one-byte behavior change is detected', () {
      final source = utf8.decode(bytes);
      final patched = source.replaceFirst('if (!_admitsCapture(revision)) return;', 'if (false) return;');
      expect(patched, isNot(source));
      expect(_matchesPin(controller, utf8.encode(patched), rels), isFalse);
    });

    test('reformatting (whitespace only) is detected', () {
      expect(_matchesPin(controller, utf8.encode('${utf8.decode(bytes)}\n'), rels), isFalse);
    });

    test('relocating an import of a NON-vendored file is detected', () {
      final source = utf8.decode(bytes);
      const logger = "'package:omi/utils/logger.dart'";
      expect(source, contains(logger));
      final patched = source.replaceFirst(logger, "'package:omi/upstream_capture/utils/logger.dart'");
      expect(_matchesPin(controller, utf8.encode(patched), rels), isFalse);
    });

    test('pointing a relocated import at a different vendored file is detected', () {
      final source = utf8.decode(bytes);
      const seams = "'package:omi/upstream_capture/services/capture/capture_seams.dart'";
      expect(source, contains(seams));
      final patched = source.replaceFirst(seams, "'package:omi/upstream_capture/services/capture/capture_policy.dart'");
      expect(_matchesPin(controller, utf8.encode(patched), rels), isFalse);
    });

    test('an extra adapter import injected into an upstream file is detected', () {
      final patched = "import 'package:omi/ella/services/ella_audio_emission_gate.dart';\n${utf8.decode(bytes)}";
      expect(_matchesPin(controller, utf8.encode(patched), rels), isFalse);
    });
  });

  test('Ella adapter code lives outside the upstream-owned trees', () {
    final owned = _entries().map((e) => e.localPath).toSet();
    for (final root in ['app/lib/upstream_capture', 'app/test/upstream_capture']) {
      for (final entity in Directory('$_repoRoot/$root').listSync(recursive: true)) {
        if (entity is! File) continue;
        final rel = entity.path.substring(_repoRoot.length + 1);
        final allowedDoc = rel.startsWith('app/lib/upstream_capture/') &&
            ['UPSTREAM_OWNED.txt', 'UPSTREAM_PATCHES.md', 'README.md'].contains(rel.split('/').last) &&
            rel.split('/').length == 4;
        expect(owned.contains(rel) || allowedDoc, isTrue, reason: '$rel is not upstream-owned');
      }
    }
  });
}
