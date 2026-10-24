import 'dart:convert';
import 'package:uuid/uuid.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/services/audio_sources/audio_source.dart';

/// Captured-order declarations, never wall-clock or uninterrupted hardware time.
class CapturedSourceSequence {
  String sessionId = const Uuid().v4();
  String epochId = const Uuid().v4();
  int frame = 0;
  int sample = 0;
  String gap = 'unknown';
  String? _origin;

  void newSession() {
    sessionId = const Uuid().v4();
    _origin = null;
    gap = 'unknown';
    discontinuity('unknown');
  }

  void discontinuity(String reason, {bool newEpoch = true}) {
    if (newEpoch) {
      epochId = const Uuid().v4();
      frame = sample = 0;
    }
    // An interruption is stronger than a simultaneous transport refresh.
    if (gap != 'interruption') gap = reason;
  }

  Map<String, dynamic>? observe(WalFrame audio, BleAudioCodec codec, Map<String, dynamic>? context) {
    if (context == null || context['account_id'] == null) return null;
    final origin = jsonEncode([
      context['account_id'],
      context['binding_epoch'],
      context['source_recording_id'],
      context['capture_session_id'],
      context['source_kind']
    ]);
    if (_origin != null && _origin != origin) discontinuity('unknown');
    _origin = origin;
    final validBytes = audio.validPcmByteLength;
    final storedSamples = codec == BleAudioCodec.pcm16 && audio.payload.length.isEven
        ? audio.payload.length ~/ 2
        : codec == BleAudioCodec.opus
            ? 160
            : codec == BleAudioCodec.opusFS320
                ? 320
                : null;
    final validSamples = codec == BleAudioCodec.pcm16
        ? (validBytes != null && validBytes.isEven && validBytes <= audio.payload.length ? validBytes ~/ 2 : null)
        : storedSamples;
    final entry = <String, dynamic>{
      'session_id': sessionId,
      'sync_key': List<int>.from(audio.syncKey.bytes),
      'run': <String, dynamic>{
        'source_epoch_id': epochId,
        'socket_generation': context['socket_generation'],
        'source_frame_start': frame++,
        'source_sample_start': validSamples == null ? null : sample,
        'valid_sample_count': validSamples,
        'padding_sample_count': storedSamples == null || validSamples == null ? null : storedSamples - validSamples,
        'gap_before': gap,
      },
      'origin': Map<String, dynamic>.from(context),
    };
    if (validSamples != null) sample += validSamples;
    gap = 'none';
    return entry;
  }
}

Map<String, dynamic>? foldCapturedProvenance(List<Map<String, dynamic>?> entries) {
  if (entries.isEmpty || entries.any((e) => e == null)) return null;
  final session = entries.first!['session_id'];
  if (entries.any((e) => e!['session_id'] != session)) return null;
  final runs = <Map<String, dynamic>>[];
  final origins = <Map<String, dynamic>>[];
  final keys = <List<int>>[];
  for (var i = 0; i < entries.length; i++) {
    final entry = entries[i]!;
    keys.add(List<int>.from(entry['sync_key']));
    final run = Map<String, dynamic>.from(entry['run']);
    run['frame_start'] = i;
    run['frame_end'] = i + 1;
    final origin = Map<String, dynamic>.from(entry['origin']);
    if (runs.isNotEmpty && _canJoin(runs.last, run) && jsonEncode(origins.last['origin']) == jsonEncode(origin)) {
      runs.last['frame_end'] = i + 1;
      runs.last['valid_sample_count'] += run['valid_sample_count'];
      origins.last['frame_end'] = i + 1;
    } else {
      runs.add(run);
      origins.add({'frame_start': i, 'frame_end': i + 1, 'origin': origin});
    }
  }
  return {'version': 2, 'source_session_id': session, 'sync_keys': keys, 'runs': runs, 'origin_runs': origins};
}

bool _canJoin(Map<String, dynamic> a, Map<String, dynamic> b) =>
    b['gap_before'] == 'none' &&
    a['padding_sample_count'] == 0 &&
    b['padding_sample_count'] == 0 &&
    a['valid_sample_count'] is int &&
    b['valid_sample_count'] is int &&
    a['source_epoch_id'] == b['source_epoch_id'] &&
    a['socket_generation'] == b['socket_generation'] &&
    b['source_frame_start'] == a['source_frame_start'] + a['frame_end'] - a['frame_start'] &&
    b['source_sample_start'] == a['source_sample_start'] + a['valid_sample_count'];

Map<String, dynamic>? appendCapturedProvenance(Map<String, dynamic>? a, Map<String, dynamic>? b) {
  if (a == null || b == null || a['source_session_id'] != b['source_session_id']) return null;
  // Detach from the old immutable disk claim before appending a checkpoint.
  final out = jsonDecode(jsonEncode(a)) as Map<String, dynamic>;
  final offset = (out['sync_keys'] as List).length;
  (out['sync_keys'] as List).addAll(b['sync_keys']);
  for (final key in ['runs', 'origin_runs']) {
    for (final raw in b[key] as List) {
      final run = Map<String, dynamic>.from(raw);
      run['frame_start'] += offset;
      run['frame_end'] += offset;
      (out[key] as List).add(run);
    }
  }
  return out;
}
