import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/ella/voice/dictation_draft_snapshot.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'ephemeral_dictation_session_test.dart' show FictionalDraftAuthority;

void main() {
  final binding = TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('plugin.csdcorp.com/speech_to_text');
  tearDown(() => binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
  for (final initialTerminal in [true, false]) {
    test('actual Darwin codec ephemeral final insertion; initial terminal $initialTerminal', () async {
      final authority = FictionalDraftAuthority();
      final lease = EllaAccountCommitBarrier.begin(authorityProvider: () => authority, onInvalidated: () {})!;
      final registry = AudioCaptureOwnership();
      var value = const TextEditingValue(text: 'draft');
      var revision = 0, applies = 0, generation = 0, legacyStops = 0;
      var running = false, task = false, terminal = false, allowTerminal = initialTerminal;
      Map<String, Object?> proof() => {
            'generation': generation,
            'engineRunning': running,
            'tapMayBeInstalled': running,
            'stopAmbiguous': false,
            'taskRetained': task,
            'recognitionDrainSupported': true,
            'recognitionTaskCreated': generation > 0,
            'recognitionTerminal': terminal
          };
      binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
        if (call.method == 'ellaOwnershipProbe') return proof();
        if (call.method == 'ellaStopAndDrain') {
          expect(call.arguments, {'generation': 1});
          running = false;
          task = false;
          terminal = allowTerminal;
          return {
            ...proof(),
            'finalResultJSON': jsonEncode({
              'alternates': [
                {'recognizedWords': 'codec final', 'confidence': 1.0}
              ],
              'finalResult': true
            })
          };
        }
        throw StateError('unexpected native method');
      });
      final session = EphemeralDictationSession(
          port: DarwinStandardSpeechReceiptPort(stopSpeech: () async {
            legacyStops++;
          }),
          draft: DictationDraftSnapshot(value: value, lease: lease, epoch: 1),
          draftRevision: 0,
          readEditor: () => DictationEditorBinding(value: value, lease: lease, epoch: 1, revision: revision),
          hasExactCaptureAuthority: () => authority.isCurrent(),
          canStartFresh: () => true,
          requestPermission: () async => true,
          initializeRecognition: () async => true,
          listen: () async {
            generation = 1;
            running = task = true;
          },
          applyDraft: (next) {
            value = next;
            revision++;
            applies++;
          },
          ownership: registry);
      expect(await session.start(), true);
      if (!initialTerminal) {
        await expectLater(session.finish(), throwsStateError);
        expect(applies, 0);
        expect(legacyStops, 0);
        expect(registry.isReserved, true);
        allowTerminal = true;
      }
      expect((await session.finish())!.text, 'draft codec final');
      expect(applies, 1);
      expect(legacyStops, 1);
      expect(registry.isReserved, false);
      lease.close();
    });
  }
}
