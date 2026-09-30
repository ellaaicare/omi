// ellaaicare/ella-ai#1280: switching the bound account mid-session tears the
// session down and no frame is ever emitted under the old account afterwards.
import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/utils/enums.dart';

import '../../upstream_capture/support/capture/scripted_device_connection.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_upstream_account_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
  });

  tearDown(() async {
    await h.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  test('bind refuses a uid that is not the signed-in account', () async {
    expect(await h.bind(accountB), isFalse);
    expect(await h.bind(''), isFalse);
    expect(h.authority.isBound, isFalse);
  });

  test('real runtime rejects an A start after boot resumes under B without prompting consent', () async {
    final boot = Completer<CaptureProvider>();
    final runtime = EllaUpstreamCaptureRuntime(authority: h.authority, bootForTesting: () => boot.future);
    final pendingPhone = runtime.startPhoneCapture(accountA);
    final pendingNecklace = runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
    h.switchAccount(accountB);
    boot.complete(h.provider);
    expect(await pendingPhone, EllaCaptureStartOutcome.unavailable);
    expect(await pendingNecklace, EllaCaptureStartOutcome.unavailable);
    expect(h.authority.isBound, isFalse);
    expect(h.hostApi.nativeRecording, isFalse);
  });

  test('late necklace connection cannot persist or record under a same-UID replacement binding', () async {
    expect(await h.bind(accountA), isTrue);
    final originalEpoch = h.authority.bindingEpoch;
    final connection = Completer<DeviceConnection?>();
    final connectionStarted = Completer<void>();
    final runtime = EllaUpstreamCaptureRuntime(
      authority: h.authority,
      bootForTesting: () async => h.provider,
      connectDeviceForTesting: (_) {
        connectionStarted.complete();
        return connection.future;
      },
    );
    final pending = runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
    await connectionStarted.future;
    await h.runtime.releaseAccount();
    expect(await h.bind(accountA), isTrue);
    expect(h.authority.bindingEpoch, isNot(originalEpoch));
    connection.complete(ScriptedDeviceConnection());

    expect(await pending, EllaCaptureStartOutcome.unavailable);
    expect(upstream.SharedPreferencesUtil().btDevice.id, isNot(EllaUpstreamCaptureHarness.pendant.id));
    expect(h.provider.recordingState, isNot(RecordingState.deviceRecord));
  });

  for (final source in ['phone', 'necklace']) {
    test('$source start cannot adopt a same-UID lease replaced during policy unmute', () async {
      expect(await h.bind(accountA), isTrue);
      await h.runtime.releaseAccount();
      expect(upstream.SharedPreferencesUtil().capturePolicy.muted, isTrue);

      final unmuteStarted = Completer<void>();
      final finishUnmute = Completer<void>();
      upstream.SharedPreferencesUtil.capturePolicyBridgeForTesting = (method, arguments) async {
        if (method == 'setMuted' && arguments['muted'] == false) {
          unmuteStarted.complete();
          await finishUnmute.future;
        }
        return null;
      };
      try {
        final pending = source == 'phone'
            ? h.runtime.startPhoneCapture(accountA)
            : h.runtime.connectNecklace(accountA, EllaUpstreamCaptureHarness.pendant);
        await unmuteStarted.future;
        final retiredEpoch = h.authority.bindingEpoch;
        h.authority.release();
        expect(h.authority.bind(accountA), isTrue);
        expect(h.authority.bindingEpoch, isNot(retiredEpoch));
        finishUnmute.complete();

        expect(await pending, EllaCaptureStartOutcome.unavailable);
        expect(h.hostApi.nativeRecording, isFalse);
        expect(h.connectionAttempts, 0);
        expect(upstream.SharedPreferencesUtil().btDevice.id, isNot(EllaUpstreamCaptureHarness.pendant.id));
        await h.runtime.pendingTeardown;
      } finally {
        if (!finishUnmute.isCompleted) finishUnmute.complete();
        upstream.SharedPreferencesUtil.capturePolicyBridgeForTesting = null;
      }
    });
  }

  test('real runtime retries a rejected boot while concurrent callers share each attempt', () async {
    var attempts = 0;
    final runtime = EllaUpstreamCaptureRuntime(
      authority: h.authority,
      bootForTesting: () async {
        attempts++;
        if (attempts == 1) throw StateError('transient boot error');
        return h.provider;
      },
    );
    final first = runtime.ensureBooted();
    expect(identical(runtime.ensureBooted(), first), isTrue);
    await expectLater(first, throwsStateError);
    expect(await runtime.ensureBooted(), same(h.provider));
    expect(attempts, 2);
  });

  test('phone: account switch mid-session drops the next frame, revokes, and stops upstream capture', () async {
    expect(await h.bind(accountA), isTrue);
    final session = await h.startPhone();
    h.injectPhoneFrames(15, sessionId: session);
    await h.settle();
    final sentUnderA = h.audioFramesSent;
    expect(sentUnderA, greaterThan(0));

    h.switchAccount(accountB);
    h.injectPhoneFrames(15, sessionId: session, firstFrameIndex: 15);
    await h.settle();

    expect(h.audioFramesSent, sentUnderA, reason: 'no frame is emitted under the old account after the switch');
    expect(h.runtime.lastRevocation.value?.reason, EllaCaptureRevocationReason.accountSwitch);
    expect(h.runtime.lastRevocation.value?.uid, accountA);
    expect(h.authority.boundUid, isNull);
    expect(h.hostApi.nativeRecording, isFalse, reason: 'the native recorder is stopped');
    expect(h.provider.recordingState, isNot(RecordingState.record));
    expect(upstream.SharedPreferencesUtil().capturePolicy.muted, isTrue,
        reason: 'the upstream native admission latch is closed so native writers drop audio too');
  });

  test('necklace: account switch mid-stream stops pendant emission and device recording', () async {
    expect(await h.bind(accountA), isTrue);
    final link = await h.connectPendant();
    for (var i = 0; i < 4; i++) {
      link.emitAudio();
    }
    await h.settle();
    final sentUnderA = h.audioFramesSent;
    expect(sentUnderA, greaterThan(0));

    h.switchAccount(accountB);
    for (var i = 0; i < 4; i++) {
      link.emitAudio();
    }
    await h.settle();
    expect(h.audioFramesSent, sentUnderA);
    expect(h.runtime.lastRevocation.value?.reason, EllaCaptureRevocationReason.accountSwitch);
    expect(link.openAudioSubscriptions, 0);
  });

  test('the new account needs its own consent; the old session never resumes under it', () async {
    expect(await h.bind(accountA), isTrue);
    final session = await h.startPhone();
    h.switchAccount(accountB);
    h.injectPhoneFrames(3, sessionId: session);
    await h.settle();
    final sentBefore = h.audioFramesSent;

    // Account B has not granted consent yet: binding fails closed.
    expect(await h.bind(accountB), isFalse);
    await expectLater(h.provider.streamRecording(), throwsA(anything));
    await h.settle();
    expect(h.audioFramesSent, sentBefore);

    // After B grants consent, a NEW session (new lease generation) may start.
    grantEllaConsent(h.ellaPreferences, accountB);
    final previousGeneration = h.leases.first.generation;
    expect(await h.bind(accountB), isTrue);
    expect(h.authority.boundUid, accountB);
    expect(h.authority.expectedGeneration, isNot(previousGeneration));
    expect(upstream.SharedPreferencesUtil().capturePolicy.muted, isFalse,
        reason: 'the revocation latch is released only by a successful new bind');
    final newSession = await h.startPhone();
    expect(newSession, isNot(session));
    h.injectPhoneFrames(10, sessionId: newSession);
    await h.settle();
    expect(h.audioFramesSent, greaterThan(sentBefore));
  });

  test('a late native frame tagged with the old account session is never emitted', () async {
    expect(await h.bind(accountA), isTrue);
    final session = await h.startPhone();
    h.switchAccount(accountB);
    grantEllaConsent(h.ellaPreferences, accountB);
    expect(await h.bind(accountB), isTrue);
    final sent = h.audioFramesSent;
    // Account A's native session emits after B was bound: stale session id.
    h.injectPhoneFrames(10, sessionId: session, firstFrameIndex: 50);
    await h.settle();
    expect(h.audioFramesSent, sent);
  });

  test('explicit release (sign-out) revokes and clears the upstream user session', () async {
    expect(await h.bind(accountA), isTrue);
    await h.startPhone();
    await h.runtime.releaseAccount();
    await h.settle();
    expect(h.authority.isBound, isFalse);
    expect(h.runtime.lastRevocation.value?.reason, EllaCaptureRevocationReason.released);
    expect(h.hostApi.nativeRecording, isFalse);
  });
}
