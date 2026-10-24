import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:package_info_plus/package_info_plus.dart';

enum TranscriptViewBranch { retired, words, empty, paused, listening, transcriptionUnconfirmed }

typedef TranscriptAppIdentity = ({String version, String build});

/// Local, content-free view diagnostics. No network, account/device identifier,
/// transcript text, receipt, or file persistence. Platform log retention is not
/// guaranteed. Each view has only a process-local sequence, not a device ID.
class TranscriptViewDiagnostics {
  TranscriptViewDiagnostics({
    Future<TranscriptAppIdentity> Function()? identity,
    void Function(String)? sink,
    DateTime Function()? now,
  }) : _sink = sink ?? ((line) => debugPrint(line)),
       _now = now ?? DateTime.now {
    Future<TranscriptAppIdentity>.sync(identity ?? _installedIdentity).then((value) {
      if (!_active) return;
      _version = _publicVersion(value.version);
      _build = _publicVersion(value.build);
      final latest = _latest;
      if (latest != null) _emit(latest);
    }, onError: (Object _, StackTrace __) {
      // Metadata failure must not affect rendering or recording admission.
    });
  }

  static int _sequence = 0;
  final int _view = ++_sequence;
  final void Function(String) _sink;
  final DateTime Function() _now;
  bool _active = true;
  String? _version;
  String? _build;
  Map<String, Object?>? _latest;
  final List<Map<String, Object?>> _history = [];
  String? _lastState;
  String? _observedAt;

  static Future<TranscriptAppIdentity> _installedIdentity() async {
    final info = await PackageInfo.fromPlatform();
    return (version: info.version, build: info.buildNumber);
  }

  static String? _publicVersion(String value) =>
      RegExp(r'^[0-9][0-9A-Za-z.+-]{0,63}$').hasMatch(value) ? value : null;

  void observe({
    required bool viewerCurrent,
    required TranscriptViewBranch branch,
    String? recordingState,
    bool? transcriptionReady,
    int? segmentCount,
  }) {
    if (!_active) return;
    // Do not serialize provider state from a retired/foreign viewer. The caller
    // also avoids reading it in the first place. State values have a closed set.
    const states = {'stop', 'record', 'deviceRecord', 'pause', 'interrupted', 'error'};
    final sample = <String, Object?>{
      'viewer_current': viewerCurrent,
      'branch': viewerCurrent ? branch.name : TranscriptViewBranch.retired.name,
      'recording_state': viewerCurrent && states.contains(recordingState) ? recordingState : null,
      'transcription_ready': viewerCurrent ? transcriptionReady : null,
      'segment_count': viewerCurrent && segmentCount != null && segmentCount >= 0 ? segmentCount : null,
    };
    if (jsonEncode(sample) != jsonEncode(_latest)) {
      _observedAt = _now().toUtc().toIso8601String();
    }
    if (!viewerCurrent && _latest?['viewer_current'] != false) {
      // An old account/window's counts must not survive into a retired report.
      _history.clear();
      _lastState = null;
    }
    _latest = sample;
    _emit(sample);
  }

  void _emit(Map<String, Object?> sample) {
    final fields = <String, Object?>{
      'schema': 1,
      'view_sequence': _view,
      'app_version': _version,
      'app_build': _build,
      ...sample,
    };
    final state = jsonEncode(fields);
    if (!_active || state == _lastState) return;
    _lastState = state;
    final event = <String, Object?>{
      'emitted_utc': _now().toUtc().toIso8601String(),
      'observed_utc': _observedAt,
      ...fields,
    };
    _history.add(Map<String, Object?>.unmodifiable(event));
    if (_history.length > 16) _history.removeAt(0);
    try {
      _sink('ELLA_TRANSCRIPT_VIEW ${jsonEncode(event)}');
    } catch (_) {
      // Diagnostic sink failures are never capture or UI failures.
    }
  }

  /// Existing explicit user-initiated support sharing consumes only this closed
  /// snapshot schema. No raw text, account IDs, receipt or stable device ID.
  String supportReport() => 'Ella transcript status report\n'
      'Content-free local view diagnostics; not proof of server or Bluetooth failure.\n'
      '${jsonEncode({'schema': 1, 'events': _history})}';

  void dispose() {
    _active = false;
    _latest = null;
    _history.clear();
    _lastState = null;
    _observedAt = null;
  }
}
