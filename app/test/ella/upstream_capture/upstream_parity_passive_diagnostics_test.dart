import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';
import 'package:omi/upstream_capture/services/devices/transports/native_ble_transport.dart';

class _Host extends Fake implements BleHostApi {
  int subscribes = 0;
  @override
  Future<void> subscribeCharacteristic(String p, String s, String c) async {
    subscribes++;
  }
}

void main() {
  test('stock capture UI and its dependencies differ only by declared import relocation', () {
    final map = jsonDecode(File('lib/stock_capture/SOURCE_PINS.json').readAsStringSync()) as Map;
    final imports = (map['imports_only'] as Map).cast<String, String>();
    for (final row in map['files'] as List) {
      final raw = File(row['local_path'] as String).readAsStringSync();
      expect(sha256.convert(utf8.encode(raw)).toString(), row['local_sha256']);
      var restored = raw;
      // Longest destination first prevents an import prefix from shadowing another.
      final ordered = imports.entries.toList()..sort((a, b) => b.value.length.compareTo(a.value.length));
      for (final entry in ordered) {
        restored = restored.replaceAll('package:omi/${entry.value}', 'package:omi/${entry.key}');
      }
      expect(sha256.convert(utf8.encode(restored)).toString(), row['official_sha256'], reason: row['official_path']);
    }
  });

  test('observing audio leaves official first-packet cancellation behavior unchanged', () {
    fakeAsync((time) {
      final host = _Host();
      final transport = NativeBleTransport('FICTIONAL-PASSIVE-ONLY',
          hostApi: host,
          monotonicMicros: () => time.elapsed.inMicroseconds,
          utcNow: () => DateTime.utc(2026, 10, 16).add(time.elapsed));
      BleBridge.instance.onDeviceReady(transport.deviceId, [
        BleService(uuid: omiServiceUuid, characteristicUuids: [audioDataStreamCharacteristicUuid])
      ]);
      final sub = transport.getCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid).listen((_) {});
      time.flushMicrotasks();
      expect(host.subscribes, 1);
      BleBridge.instance.onCharacteristicValueUpdated(
          transport.deviceId, omiServiceUuid, audioDataStreamCharacteristicUuid, Uint8List.fromList([1, 2, 3, 4]));
      time.flushMicrotasks();
      expect(transport.audioHealth['packet_count'], 1);
      expect(transport.audioHealth['notification_bytes'], 4);
      expect(transport.audioHealth['liveness_watch_active'], false);
      time.elapse(const Duration(seconds: 21));
      time.flushMicrotasks();
      expect(host.subscribes, 1, reason: 'Diagnostics must not add a CCCD retry after the stock timer was cancelled');
      expect(transport.audioHealth['stream_health'], 'audio_stale');
      expect(transport.audioHealth['last_packet_age_ms'], 21000);
      sub.cancel();
      transport.dispose();
      time.flushMicrotasks();
      expect(time.pendingTimers, isEmpty);
    });
  });

  test('a wrong-route notification cannot become current audio evidence', () {
    fakeAsync((time) {
      final transport = NativeBleTransport('FICTIONAL-WRONG-ROUTE', hostApi: _Host());
      BleBridge.instance.onDeviceReady(transport.deviceId, [
        BleService(uuid: omiServiceUuid, characteristicUuids: [audioDataStreamCharacteristicUuid])
      ]);
      final sub = transport.getCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid).listen((_) {});
      time.flushMicrotasks();
      BleBridge.instance.onCharacteristicValueUpdated(
          transport.deviceId, 'UNRELATED-SERVICE', audioDataStreamCharacteristicUuid, Uint8List.fromList([1, 2, 3, 4]));
      time.flushMicrotasks();
      expect(transport.audioHealth['packet_count'], 0);
      expect(transport.audioHealth['last_packet_utc'], isNull);
      sub.cancel();
      transport.dispose();
      time.flushMicrotasks();
      expect(time.pendingTimers, isEmpty);
    });
  });
}
