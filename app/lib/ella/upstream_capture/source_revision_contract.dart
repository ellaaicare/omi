import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:uuid/uuid.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';

/// Canonical JSON used by the agreed Python v2 source digest (ASCII escaping).
String canonicalSourceJson(Object? value) {
  if (value is Map) {
    final keys = value.keys.cast<String>().toList()..sort();
    return '{${keys.map((key) => '${canonicalSourceJson(key)}:${canonicalSourceJson(value[key])}').join(',')}}';
  }
  if (value is List) return '[${value.map(canonicalSourceJson).join(',')}]';
  if (value is String) {
    final encoded = jsonEncode(value);
    return encoded.codeUnits
        .map((c) => c > 127 ? '\\u${c.toRadixString(16).padLeft(4, '0')}' : String.fromCharCode(c))
        .join();
  }
  return jsonEncode(value);
}

Future<Map<String, dynamic>?> buildSourceRevisionRequest(List<Wal> wals, List<File> files, String uid) async {
  if (wals.isEmpty || wals.length != files.length) return null;
  final first = wals.first;
  final session = first.sourceProvenance?['source_session_id'];
  if (first.conversationId == null || first.captureWindowId == null || session == null) return null;
  final claims = <Map<String, dynamic>>[];
  int previousChunk = -1;
  for (var i = 0; i < wals.length; i++) {
    final wal = wals[i];
    final p = wal.sourceProvenance;
    if (wal.ownerUid != uid ||
        wal.conversationId != first.conversationId ||
        wal.captureWindowId != first.captureWindowId ||
        p?['version'] != 2 ||
        p?['source_session_id'] != session ||
        wal.captureChunkSequence == null ||
        wal.captureChunkSequence! <= previousChunk) return null;
    previousChunk = wal.captureChunkSequence!;
    final codec = wal.codec.name;
    if (!['pcm16', 'opus', 'opusFS320'].contains(codec) || wal.sampleRate != 16000 || wal.channel != 1) return null;
    final bytes = await files[i].readAsBytes();
    final samples = <int>[];
    var offset = 0;
    while (offset < bytes.length) {
      if (offset + 4 > bytes.length) return null;
      final length = ByteData.sublistView(bytes, offset, offset + 4).getUint32(0, Endian.little);
      offset += 4;
      if (length == 0 || offset + length > bytes.length || (codec == 'pcm16' && length.isOdd)) return null;
      samples.add(codec == 'pcm16'
          ? length ~/ 2
          : codec == 'opus'
              ? 160
              : 320);
      offset += length;
    }
    if (samples.isEmpty || samples.length != wal.totalFrames) return null;
    if (p!['origin_runs'] is! List || (p['origin_runs'] as List).any((r) => r['origin']['account_id'] != uid))
      return null;
    if (p['sync_keys'] is! List || p['runs'] is! List) return null;
    final keys = p['sync_keys'] as List;
    final runs = p['runs'] as List;
    if (keys.length != samples.length ||
        keys.any((k) => k is! List || k.isEmpty || k.any((v) => v is! int || v < 0 || v > 255))) return null;
    var end = 0;
    var sampleCount = 0;
    for (final raw in runs) {
      final run = Map<String, dynamic>.from(raw);
      if (run['frame_start'] != end ||
          run['frame_end'] is! int ||
          run['frame_end'] <= end ||
          run['frame_end'] > samples.length ||
          run['socket_generation'] is! int ||
          run['source_frame_start'] is! int ||
          run['source_sample_start'] is! int ||
          run['valid_sample_count'] is! int ||
          run['padding_sample_count'] is! int ||
          run['valid_sample_count'] < 0 ||
          run['padding_sample_count'] < 0 ||
          !['none', 'unknown', 'interruption', 'transport_rebind'].contains(run['gap_before'])) return null;
      if (run['source_frame_start'] < 0 ||
          run['source_sample_start'] < 0 ||
          run['socket_generation'] < 0 ||
          run['source_epoch_id'] is! String ||
          !RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$').hasMatch(run['source_epoch_id']))
        return null;
      final stored = samples.sublist(end, run['frame_end']).fold<int>(0, (a, b) => a + b);
      if (run['valid_sample_count'] + run['padding_sample_count'] != stored ||
          (codec != 'pcm16' && run['padding_sample_count'] != 0)) return null;
      if (run['padding_sample_count'] > 0) {
        if (run['frame_end'] != end + 1) return null;
        var packetOffset = 0;
        for (var f = 0; f < end; f++) packetOffset += 4 + samples[f] * 2;
        final payloadStart = packetOffset + 4;
        final paddingStart = payloadStart + (run['valid_sample_count'] as int) * 2;
        if (bytes.sublist(paddingStart, payloadStart + stored * 2).any((b) => b != 0)) return null;
      }
      sampleCount += stored;
      end = run['frame_end'];
    }
    if (end != samples.length) return null;
    claims.add({
      'name': files[i].uri.pathSegments.last,
      'sha256': sha256.convert(bytes).toString(),
      'byte_length': bytes.length,
      'frame_count': samples.length,
      'codec': codec == 'opusFS320' ? 'opus_fs320' : codec,
      'chunk_index': wal.captureChunkSequence,
      'sample_count': sampleCount,
      'sync_keys': keys,
      'runs': runs
    });
  }
  final digest = sha256.convert(utf8.encode(canonicalSourceJson(claims))).toString();
  final existing = wals.map((w) => w.sourceRevisionRequest).whereType<Map<String, dynamic>>().toList();
  if (existing.isNotEmpty) {
    final old = existing.first;
    if (old['version'] != 2 ||
        old['mode'] != 'whole_source_revision' ||
        old['operation_id'] is! String ||
        !RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$').hasMatch(old['operation_id']) ||
        old['account_id'] != uid ||
        old['conversation_id'] != first.conversationId ||
        old['capture_window_id'] != first.captureWindowId ||
        old['source_session_id'] != session ||
        old['source_manifest_sha256'] != digest ||
        canonicalSourceJson(old['files']) != canonicalSourceJson(claims) ||
        existing.any((e) => canonicalSourceJson(e) != canonicalSourceJson(old))) return null;
    return old;
  }
  return {
    'version': 2,
    'mode': 'whole_source_revision',
    'account_id': uid,
    'conversation_id': first.conversationId,
    'capture_window_id': first.captureWindowId,
    'source_session_id': session,
    'operation_id': const Uuid().v4(),
    'source_manifest_sha256': digest,
    'files': claims
  };
}

String expectedSourceRevisionId(Map<String, dynamic> request) => const Uuid().v5('87bcbf77-b29c-47d9-aa85-87a3b1f01c95',
    '${request['account_id']}:${request['conversation_id']}:${request['operation_id']}');

bool sourceRevisionReceiptMatches(Map<String, dynamic>? request, Map<String, dynamic>? receipt) {
  if (request == null ||
      receipt == null ||
      receipt['status'] != 'revision_committed' ||
      receipt['preservation'] != 'live_transcript_unchanged' ||
      receipt['revision_id'] is! String ||
      !(RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$').hasMatch(receipt['revision_id'])))
    return false;
  if (receipt['revision_id'] != expectedSourceRevisionId(request)) return false;
  for (final key in [
    'version',
    'mode',
    'account_id',
    'conversation_id',
    'capture_window_id',
    'source_session_id',
    'operation_id',
    'source_manifest_sha256'
  ]) {
    if (receipt[key] != request[key]) return false;
  }
  final expected = request['files'];
  final actual = receipt['files'];
  if (expected is! List || actual is! List || expected.isEmpty || expected.length != actual.length) return false;
  for (var i = 0; i < expected.length; i++) {
    if (actual[i] is! Map) return false;
    final file = Map<String, dynamic>.from(actual[i]);
    if (file.remove('status') != 'completed' ||
        file.remove('frame_start') != 0 ||
        file.remove('frame_end') != expected[i]['frame_count'] ||
        canonicalSourceJson(file) != canonicalSourceJson(expected[i])) return false;
  }
  return true;
}
