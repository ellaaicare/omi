import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/transcript_view_diagnostics.dart';

void main() {
  Map<String, dynamic> decode(String line) =>
      jsonDecode(line.substring('ELLA_TRANSCRIPT_VIEW '.length)) as Map<String, dynamic>;
  final fixed = DateTime.utc(2026, 10, 21, 20, 30);

  test('view diagnostic records only a closed content-free snapshot with local build identity', () async {
    final lines = <String>[];
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1.0.572', build: '964'),
      sink: lines.add, now: () => fixed);
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: true, branch: TranscriptViewBranch.paused,
      recordingState: 'pause', transcriptionReady: false, segmentCount: 0);
    final d = decode(lines.single);
    expect(d.keys.toSet(), {'emitted_utc', 'observed_utc', 'schema', 'view_sequence', 'app_version',
      'app_build', 'viewer_current', 'branch', 'recording_state', 'transcription_ready', 'segment_count'});
    expect(d['viewer_current'], true);
    expect(d['branch'], 'paused');
    expect(d['app_build'], '964');
    expect(d['recording_state'], 'pause');
    expect(d['segment_count'], 0);
    r.dispose();
  });

  test('retired snapshot suppresses all supplied provider fields', () async {
    final lines = <String>[];
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '1'), sink: lines.add);
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: false, branch: TranscriptViewBranch.words,
      recordingState: 'record', transcriptionReady: true, segmentCount: 999);
    final d = decode(lines.single);
    expect(d['branch'], 'retired');
    expect(d['recording_state'], isNull);
    expect(d['transcription_ready'], isNull);
    expect(d['segment_count'], isNull);
    r.dispose();
  });

  test('repeated builds are deduplicated but genuine state changes emit', () async {
    final lines = <String>[];
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '1'), sink: lines.add);
    await Future<void>.delayed(Duration.zero);
    for (var i = 0; i < 3; i++) {
      r.observe(viewerCurrent: true, branch: TranscriptViewBranch.listening,
        recordingState: 'deviceRecord', transcriptionReady: true, segmentCount: 0);
    }
    expect(lines, hasLength(1));
    r.observe(viewerCurrent: true, branch: TranscriptViewBranch.words,
      recordingState: 'deviceRecord', transcriptionReady: true, segmentCount: 1);
    expect(lines, hasLength(2));
    r.dispose();
  });

  test('late metadata preserves original observed time instead of inventing a later observation', () async {
    final identity = Completer<TranscriptAppIdentity>();
    final lines = <String>[];
    var clock = fixed;
    final r = TranscriptViewDiagnostics(identity: () => identity.future, sink: lines.add, now: () => clock);
    r.observe(viewerCurrent: false, branch: TranscriptViewBranch.retired);
    clock = fixed.add(const Duration(seconds: 4));
    identity.complete((version: '1', build: '2'));
    await Future<void>.delayed(Duration.zero);
    expect(lines, hasLength(2));
    expect(decode(lines.last)['observed_utc'], fixed.toIso8601String());
    expect(decode(lines.last)['emitted_utc'], clock.toIso8601String());
    r.dispose();
  });

  test('dispose suppresses delayed metadata and subsequent view events', () async {
    final identity = Completer<TranscriptAppIdentity>();
    final lines = <String>[];
    final r = TranscriptViewDiagnostics(identity: () => identity.future, sink: lines.add);
    r.dispose();
    identity.complete((version: '1', build: '2'));
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: false, branch: TranscriptViewBranch.retired);
    expect(lines, isEmpty);
  });

  test('unknown metadata and arbitrary state strings are not serialized', () async {
    final lines = <String>[];
    final r = TranscriptViewDiagnostics(identity: () async => (version: 'PRIVATE VALUE', build: 'x\nSECRET'),
      sink: lines.add);
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: true, branch: TranscriptViewBranch.empty,
      recordingState: 'SECRET-STATE', segmentCount: -1);
    final d = decode(lines.single);
    expect(d['app_version'], isNull);
    expect(d['app_build'], isNull);
    expect(d['recording_state'], isNull);
    expect(d['segment_count'], isNull);
    expect(lines.single, isNot(contains('SECRET')));
    r.dispose();
  });

  test('diagnostic sink failure does not become an application failure', () async {
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '1'),
      sink: (_) => throw StateError('fake sink unavailable'));
    await Future<void>.delayed(Duration.zero);
    expect(() => r.observe(viewerCurrent: false, branch: TranscriptViewBranch.retired), returnsNormally);
    r.dispose();
  });

  test('support report bounds local history to sixteen state changes', () async {
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '0'), sink: (_) {});
    await Future<void>.delayed(Duration.zero);
    for (var count = 0; count < 40; count++) {
      r.observe(viewerCurrent: true, branch: TranscriptViewBranch.words,
        recordingState: 'deviceRecord', transcriptionReady: true, segmentCount: count);
    }
    final report = r.supportReport();
    final data = jsonDecode(report.substring(report.indexOf('{'))) as Map<String, dynamic>;
    expect(data['events'], hasLength(16));
    expect((data['events'] as List).last['segment_count'], 39);
    r.dispose();
  });

  test('support report clears old provider history before a retired export', () async {
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '0'), sink: (_) {});
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: true, branch: TranscriptViewBranch.words,
      recordingState: 'deviceRecord', transcriptionReady: true, segmentCount: 123);
    r.observe(viewerCurrent: false, branch: TranscriptViewBranch.retired,
      recordingState: 'record', transcriptionReady: true, segmentCount: 999);
    final report = r.supportReport();
    final data = jsonDecode(report.substring(report.indexOf('{'))) as Map<String, dynamic>;
    for (final event in data['events'] as List) {
      expect(event['viewer_current'], false);
      expect(event['segment_count'], isNull);
      expect(event['recording_state'], isNull);
      expect(event['transcription_ready'], isNull);
    }
    expect(report, isNot(contains('"segment_count":123')));
    expect(report, isNot(contains('"segment_count":999')));
    r.dispose();
  });

  test('support report is empty after view disposal', () async {
    final r = TranscriptViewDiagnostics(identity: () async => (version: '1', build: '0'), sink: (_) {});
    await Future<void>.delayed(Duration.zero);
    r.observe(viewerCurrent: true, branch: TranscriptViewBranch.words, segmentCount: 1);
    r.dispose();
    expect(r.supportReport(), contains('"events":[]'));
  });

}
