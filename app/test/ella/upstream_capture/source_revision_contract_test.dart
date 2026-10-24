import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/audio_sources/phone_mic_source.dart';
import 'package:omi/ella/upstream_capture/captured_source_provenance.dart';
import 'package:omi/ella/upstream_capture/source_revision_contract.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';
import 'package:omi/upstream_capture/services/wals/local_wal_sync.dart';

void main() {
  late Directory dir;
  setUp(() async {
    dir = await Directory.systemTemp.createTemp('source_revision_');
  });
  tearDown(() async {
    await dir.delete(recursive: true);
  });
  Map<String, dynamic> context(int generation) => {
        'account_id': 'owner',
        'binding_epoch': 1,
        'source_recording_id': 'source-recording',
        'capture_session_id': 7,
        'source_kind': 'phone',
        'socket_generation': generation,
        'protocol_generation': 'server-$generation'
      };
  Future<(Wal, File)> fixture({bool padded = false}) async {
    final source = PhoneMicSource();
    final seq = CapturedSourceSequence();
    final frames = source.processBytes(List<int>.generate(320 * 3, (i) => i % 255));
    if (padded) {
      source.processBytes([1, 0, 2, 0]);
      frames.addAll(source.flush());
    }
    final entries = frames.map((f) => seq.observe(f, BleAudioCodec.pcm16, context(1))).toList();
    final wal = Wal(
        timerStart: 1,
        codec: BleAudioCodec.pcm16,
        seconds: 0,
        totalFrames: frames.length,
        ownerUid: 'owner',
        conversationId: 'target',
        captureWindowId: '11111111-1111-4111-8111-111111111111',
        captureChunkSequence: 0,
        sourceProvenance: foldCapturedProvenance(entries));
    final bytes = <int>[];
    for (final f in frames) {
      final n = ByteData(4)..setUint32(0, f.payload.length, Endian.little);
      bytes.addAll(n.buffer.asUint8List());
      bytes.addAll(f.payload);
    }
    final file = File('${dir.path}/${wal.getFileName().split('/').last}');
    await file.writeAsBytes(bytes, flush: true);
    return (wal, file);
  }

  Map<String, dynamic> receipt(Map<String, dynamic> request) => {
        ...request,
        'status': 'revision_committed',
        'preservation': 'live_transcript_unchanged',
        'revision_id': expectedSourceRevisionId(request),
        'files': [
          for (final f in request['files'])
            {...f, 'frame_start': 0, 'frame_end': f['frame_count'], 'status': 'completed'}
        ]
      };
  test('source revision batches are ordered, bounded and isolate immutable operations', () {
    Wal chunk(int index, {String? operation, String window = 'window'}) => Wal(
        timerStart: index + 1,
        codec: BleAudioCodec.pcm16,
        seconds: 1,
        ownerUid: 'owner',
        conversationId: 'target',
        captureWindowId: window,
        captureChunkSequence: index,
        sourceProvenance: {'source_session_id': 'session'},
        sourceRevisionRequest: operation == null ? null : {'operation_id': operation});
    final pending = [for (var i = 0; i < 8; i++) chunk(i)];
    expect(nextSyncUploadBatch(pending, 99).map((w) => w.captureChunkSequence), [0, 1, 2, 3, 4]);
    pending[0].sourceRevisionRequest = {'operation_id': 'ambiguous'};
    expect(nextSyncUploadBatch(pending, 99).map((w) => w.captureChunkSequence), [1, 2, 3, 4, 5]);
    pending.add(chunk(20, operation: 'ambiguous'));
    expect(nextSyncUploadBatch(pending, 99).map((w) => w.captureChunkSequence), [0, 20]);
    pending.add(chunk(30, window: 'other'));
    expect(nextSyncUploadBatch(pending, 99).map((w) => w.captureChunkSequence), [30]);
  });
  test('odd PCM payload and malformed source epoch cannot form a request', () async {
    final f = await fixture();
    f.$1.sourceProvenance!['runs'][0]['source_epoch_id'] = '-' * 36;
    expect(await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'), isNull);
    final odd = await fixture();
    final bytes = await odd.$2.readAsBytes();
    bytes[0] = 63;
    bytes[1] = 1;
    await odd.$2.writeAsBytes(bytes);
    expect(await buildSourceRevisionRequest([odd.$1], [odd.$2], 'owner'), isNull);
  });
  test('PCM tails retain actual valid samples and separate zero padding', () async {
    final f = await fixture(padded: true);
    final p = f.$1.sourceProvenance!;
    expect((p['sync_keys'] as List).length, 4);
    expect((p['runs'] as List).last['valid_sample_count'], 2);
    expect((p['runs'] as List).last['padding_sample_count'], 158);
    final request = await buildSourceRevisionRequest([f.$1], [f.$2], 'owner');
    expect(request, isNotNull);
    expect(request!['files'][0]['sample_count'], 640);
    expect(sourceRevisionReceiptMatches(request, receipt(request)), isTrue);
  });
  test('transport rebind retains source sequence; interruption resets epoch explicitly', () {
    final source = PhoneMicSource();
    final seq = CapturedSourceSequence();
    final f = source.processBytes(List<int>.filled(960, 0));
    final a = seq.observe(f[0], BleAudioCodec.pcm16, context(1))!;
    seq.discontinuity('transport_rebind', newEpoch: false);
    final b = seq.observe(f[1], BleAudioCodec.pcm16, context(2))!;
    expect(b['run']['source_epoch_id'], a['run']['source_epoch_id']);
    expect(b['run']['source_frame_start'], 1);
    expect(b['run']['source_sample_start'], 160);
    expect(b['run']['gap_before'], 'transport_rebind');
    seq.discontinuity('interruption');
    final c = seq.observe(f[2], BleAudioCodec.pcm16, context(2))!;
    expect(c['run']['source_epoch_id'], isNot(a['run']['source_epoch_id']));
    expect(c['run']['source_frame_start'], 0);
    expect(c['run']['gap_before'], 'interruption');
  });
  test('legacy WAL roundtrip never manufactures provenance', () {
    final old = Wal(timerStart: 1, codec: BleAudioCodec.pcm16, seconds: 1);
    final loaded = Wal.fromJson(old.toJson());
    expect(loaded.sourceProvenance, isNull);
    expect(loaded.sourceRevisionRequest, isNull);
    expect(loaded.toJson().containsKey('source_provenance'), isFalse);
  });
  test('atomic inventory roundtrip retains exact operation and provenance before retry', () async {
    final f = await fixture();
    final request = await buildSourceRevisionRequest([f.$1], [f.$2], 'owner');
    f.$1.sourceRevisionRequest = request;
    final index = File('${dir.path}/index.json');
    await index.writeAsString(jsonEncode(f.$1.toJson()), flush: true);
    final restored = Wal.fromJson(jsonDecode(await index.readAsString()));
    final retry = await buildSourceRevisionRequest([restored], [f.$2], 'owner');
    expect(canonicalSourceJson(retry), canonicalSourceJson(request));
    final changed = await f.$2.readAsBytes();
    changed[10] ^= 1;
    await f.$2.writeAsBytes(changed);
    expect(await buildSourceRevisionRequest([restored], [f.$2], 'owner'), isNull,
        reason: 'no new operation after ambiguous source mutation');
  });
  test('missing target, wrong account and unsupported codec fail before request', () async {
    final f = await fixture();
    expect(await buildSourceRevisionRequest([f.$1], [f.$2], 'other'), isNull);
    f.$1.conversationId = null;
    expect(await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'), isNull);
    f.$1.conversationId = 'target';
    f.$1.codec = BleAudioCodec.aac;
    expect(await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'), isNull);
  });
  for (final key in [
    'account_id',
    'conversation_id',
    'capture_window_id',
    'source_session_id',
    'operation_id',
    'source_manifest_sha256',
    'version',
    'mode',
    'preservation'
  ]) {
    test('receipt rejects wrong $key', () async {
      final f = await fixture();
      final request = (await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'))!;
      final wrong = receipt(request)..[key] = 'wrong';
      expect(sourceRevisionReceiptMatches(request, wrong), isFalse);
    });
  }
  test('receipt rejects incomplete, reordered, duplicate and modified source coverage', () async {
    final f = await fixture();
    final request = (await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'))!;
    final partial = receipt(request);
    partial['files'][0]['frame_end'] = 2;
    expect(sourceRevisionReceiptMatches(request, partial), isFalse);
    final duplicate = receipt(request);
    duplicate['files'].add(duplicate['files'][0]);
    expect(sourceRevisionReceiptMatches(request, duplicate), isFalse);
    final modified = receipt(request);
    modified['files'][0]['sha256'] = '0' * 64;
    expect(sourceRevisionReceiptMatches(request, modified), isFalse);
    expect(sourceRevisionReceiptMatches(null, receipt(request)), isFalse);
  });
  test('exact independent backend route response matches actual exported client claim', () {
    final root = 'test/ella/upstream_capture/fixtures/source_revision_v2';
    final request = jsonDecode(File('$root/client-request.json').readAsStringSync()) as Map<String, dynamic>;
    final response = jsonDecode(File('$root/backend-response.json').readAsStringSync()) as Map<String, dynamic>;
    expect(sourceRevisionReceiptMatches(request, Map<String, dynamic>.from(response['coverage_receipt'])), isTrue);
    expect(response['updated_memories'], isEmpty, reason: 'live transcript is preserved, not replaced');
    expect(response['coverage_receipt']['preservation'], 'live_transcript_unchanged');
  });

  test('export actual PCM request for independent backend contract reconciliation', () async {
    final f = await fixture(padded: true);
    final request = (await buildSourceRevisionRequest([f.$1], [f.$2], 'owner'))!;
    final destination = Platform.environment['ELLA_SOURCE_FIXTURE_DIR'];
    if (destination != null) {
      final output = Directory(destination);
      await output.create(recursive: true);
      await f.$2.copy('${output.path}/${f.$2.uri.pathSegments.last}');
      await File('${output.path}/coverage.json').writeAsString(jsonEncode(request));
      await File('${output.path}/local-provenance.json').writeAsString(jsonEncode(f.$1.toJson()));
    }
    expect(sourceRevisionReceiptMatches(request, receipt(request)), isTrue);
  });
}
