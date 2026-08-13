import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  test('Demo voice fixtures never initialize speech recognition', () {
    expect(
      EllaVoiceChatPage.shouldInitializeSpeech(
        EllaVoiceDemoState(quota: EllaAccessDemoFixtures.active.quota),
      ),
      isFalse,
    );
    expect(EllaVoiceChatPage.shouldInitializeSpeech(null), isTrue);
  });

  for (final path in ['standard', 'v2v']) {
    test('$path voice waits for active phone capture stop/finalization before microphone takeover', () async {
      final coordinator = VoicePhoneCaptureTakeoverCoordinator();
      final finalization = Completer<bool>();
      final events = <String>[];

      final takeover = path == 'standard'
          ? coordinator.prepareStandard(
              phoneCaptureActive: true,
              phoneCaptureContentful: true,
              stopAndFinalizePhoneCapture: () {
                events.add('finalize-start');
                return finalization.future.whenComplete(() => events.add('finalize-ack'));
              },
            )
          : coordinator.prepareV2V(
              phoneCaptureActive: true,
              phoneCaptureContentful: true,
              stopAndFinalizePhoneCapture: () {
                events.add('finalize-start');
                return finalization.future.whenComplete(() => events.add('finalize-ack'));
              },
            );

      await pumpEventQueue();
      expect(events, ['finalize-start']);

      finalization.complete(true);
      if (await takeover) events.add('microphone-start');

      expect(events, ['finalize-start', 'finalize-ack', 'microphone-start']);
    });
  }

  test('failed phone capture acknowledgement blocks voice microphone takeover', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final events = <String>[];

    final acknowledged = await coordinator.prepareStandard(
      phoneCaptureActive: false,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () async {
        events.add('finalize');
        return false;
      },
    );
    if (acknowledged) events.add('microphone-start');

    expect(acknowledged, isFalse);
    expect(events, ['finalize']);
  });

  test('thrown phone capture finalization fails closed before voice microphone takeover', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();

    expect(
      await coordinator.prepareV2V(
        phoneCaptureActive: true,
        phoneCaptureContentful: true,
        stopAndFinalizePhoneCapture: () => throw StateError('synthetic finalization failure'),
      ),
      isFalse,
    );
  });

  test('active empty phone capture may hand off after its transcript stop completes', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final transcriptStop = Completer<bool>();
    var microphoneStarted = false;

    final takeover = coordinator.prepareStandard(
      phoneCaptureActive: true,
      phoneCaptureContentful: false,
      stopAndFinalizePhoneCapture: () => transcriptStop.future,
    );
    await pumpEventQueue();
    expect(microphoneStarted, isFalse);

    transcriptStop.complete(false);
    if (await takeover) microphoneStarted = true;

    expect(microphoneStarted, isTrue);
  });

  test('concurrent standard and V2V takeover share one phone capture finalization', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final finalization = Completer<bool>();
    var finalizationCalls = 0;

    final standard = coordinator.prepareStandard(
      phoneCaptureActive: true,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () {
        finalizationCalls++;
        return finalization.future;
      },
    );
    final v2v = coordinator.prepareV2V(
      phoneCaptureActive: true,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () async {
        finalizationCalls++;
        return false;
      },
    );

    await pumpEventQueue();
    expect(finalizationCalls, 1);

    finalization.complete(true);
    expect(await standard, isTrue);
    expect(await v2v, isTrue);
    expect(finalizationCalls, 1);

    expect(
      await coordinator.prepareStandard(
        phoneCaptureActive: false,
        phoneCaptureContentful: false,
        stopAndFinalizePhoneCapture: () async {
          finalizationCalls++;
          return false;
        },
      ),
      isTrue,
    );
    expect(finalizationCalls, 1);
  });

  test('standard and V2V production paths use the finalizing phone capture takeover', () {
    final source = File('lib/ella/pages/ella_voice_chat_page.dart').readAsStringSync();

    expect(RegExp(r'_preparePhoneCaptureForVoice\(v2v: false\)').allMatches(source), hasLength(1));
    expect(RegExp(r'_preparePhoneCaptureForVoice\(v2v: true\)').allMatches(source), hasLength(1));
    expect(
      RegExp(r'stopAndFinalizePhoneCapture: captureProvider\.stopStreamRecordingAndFinalize').allMatches(source),
      hasLength(2),
    );
    expect(source, contains('captureDiagnostics.source == CaptureDiagnosticSource.phone'));
    expect(source, contains('captureDiagnostics.hasPhysicalAudio || captureProvider.hasCapturableContent'));
    expect(source, isNot(contains('captureProvider.stopStreamRecording()')));
  });

  Future<void> pumpVoice(
    WidgetTester tester, {
    required EllaVoiceDemoState state,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: EllaVoiceChatPage(demoState: state),
      ),
    );
    await tester.pump();
  }

  testWidgets('soft warning and remaining time are small gentle voice-surface states', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(quota: EllaAccessDemoFixtures.softDaily.quota),
    );

    expect(find.textContaining('left'), findsOneWidget);
    expect(find.textContaining('nearing today’s voice time'), findsOneWidget);
    expect(find.text('Demo preview — voice is not active'), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsNothing);
  });

  testWidgets('all policy outcomes have distinct claim-compliant copy', (tester) async {
    final cases = {
      EllaVoicePolicyReason.quotaDaily: 'you can talk again tomorrow',
      EllaVoicePolicyReason.quotaMonthly: 'after the monthly reset',
      EllaVoicePolicyReason.concurrent: 'End the other voice conversation',
      EllaVoicePolicyReason.suspended: 'You can still use Ella’s other features',
      EllaVoicePolicyReason.sessionMax: 'Start a new voice conversation',
    };

    for (final entry in cases.entries) {
      await pumpVoice(
        tester,
        state: EllaVoiceDemoState(
          quota: EllaAccessDemoFixtures.active.quota,
          policyReason: entry.key,
        ),
      );
      expect(find.textContaining(entry.value), findsOneWidget, reason: entry.key.name);
      expect(find.textContaining('connection needs a moment'), findsNothing, reason: entry.key.name);
    }
  });

  testWidgets('technical failure is not labeled as quota or policy denial', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(
        quota: EllaAccessDemoFixtures.active.quota,
        technicalFailure: true,
      ),
    );

    expect(find.textContaining('connection needs a moment'), findsOneWidget);
    expect(find.textContaining('tomorrow'), findsNothing);
    expect(find.textContaining('monthly reset'), findsNothing);
  });
}
