// ellaaicare/ella-ai#1280 item 4: mayEmitAudio is fail-closed on CURRENT
// authority and is evaluated per frame at every audio boundary of the real
// upstream capture stack (phone mic, necklace, socket) — not just at start.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as fork;
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_audio_emission_gate.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_gated_capture_seams.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import 'package:omi/upstream_capture/utils/enums.dart';

import 'support/ella_upstream_capture_harness.dart';

AiConsentActiveSessionLease _lease(fork.SharedPreferencesUtil preferences, String uid, {void Function()? onLost}) =>
    AiConsentActiveSessionLease(
      uid: uid,
      onAuthorityLost: () => onLost?.call(),
      preferences: preferences,
      refreshAuthority: (uid, receiptId, decidedAt) async =>
          const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified),
      revalidateProvisioning: (uid, receiptId) => true,
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('mayEmitAudio', () {
    late fork.SharedPreferencesUtil preferences;
    final leases = <AiConsentActiveSessionLease>[];

    setUp(() async {
      EllaProvisioningAuthorityCoordinator.resetForTesting();
      SharedPreferences.setMockInitialValues({});
      await fork.SharedPreferencesUtil.init();
      preferences = fork.SharedPreferencesUtil();
      grantEllaConsent(preferences, accountA);
    });

    tearDown(() {
      for (final lease in leases) {
        lease.stop();
      }
      leases.clear();
      EllaProvisioningAuthorityCoordinator.resetForTesting();
    });

    AiConsentActiveSessionLease started(String uid) {
      final lease = _lease(preferences, uid)..start();
      leases.add(lease);
      return lease;
    }

    test('admits only a nonempty bound uid with a current lease in the expected generation', () {
      final lease = started(accountA);
      expect(lease.hasCurrentAuthority, isTrue);
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: lease.generation), isTrue);
      expect(mayEmitAudio(boundUid: null, lease: lease, expectedGeneration: lease.generation), isFalse);
      expect(mayEmitAudio(boundUid: '', lease: lease, expectedGeneration: lease.generation), isFalse);
      expect(mayEmitAudio(boundUid: '   ', lease: lease, expectedGeneration: lease.generation), isFalse);
      expect(mayEmitAudio(boundUid: accountB, lease: lease, expectedGeneration: lease.generation), isFalse,
          reason: 'a lease is never authority for another account');
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: lease.generation + 1), isFalse);
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: 0), isFalse);
    });

    test('a never-started lease has no authority and generation 0', () {
      final lease = _lease(preferences, accountA);
      expect(lease.generation, 0);
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: 0), isFalse);
    });

    test('stop and restart mint new generations; a superseded session never regains authority', () {
      final lease = started(accountA);
      final first = lease.generation;
      lease.stop();
      expect(lease.generation, isNot(first));
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: first), isFalse);
      lease.start();
      expect(lease.hasCurrentAuthority, isTrue);
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: first), isFalse,
          reason: 'the restarted lease is a different session');
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: lease.generation), isTrue);
    });

    test('generations are unique across leases (a new lease never aliases an old generation)', () {
      final a = started(accountA);
      final b = started(accountA);
      expect(a.generation, isNot(b.generation));
      expect(mayEmitAudio(boundUid: accountA, lease: b, expectedGeneration: a.generation), isFalse);
    });

    test('a persisted historical acceptance alone is not authority: local decline revokes immediately', () {
      final lease = started(accountA);
      final generation = lease.generation;
      preferences.declineAiConsent();
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: generation), isFalse);
    });

    test('an account switch in prefs revokes the old account lease immediately', () {
      final lease = started(accountA);
      final generation = lease.generation;
      preferences.uid = accountB;
      expect(mayEmitAudio(boundUid: accountA, lease: lease, expectedGeneration: generation), isFalse);
    });
  });

  group('per-frame gate on the real upstream capture stack', () {
    late Directory directory;
    late EllaUpstreamCaptureHarness h;

    setUp(() async {
      directory = await Directory.systemTemp.createTemp('ella_upstream_consent_');
      h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
    });

    tearDown(() async {
      await h.dispose();
      if (directory.existsSync()) directory.deleteSync(recursive: true);
    });

    test('phone capture refuses to start without a bound consent session', () async {
      await expectLater(h.provider.streamRecording(), throwsA(anything));
      await h.settle();
      expect(h.audioFramesSent, 0);
      expect(h.hostApi.startCalls, 0, reason: 'the native recorder must never open without authority');
    });

    test('phone frames stop at the very next frame when consent is revoked mid-stream', () async {
      expect(await h.bind(), isTrue);
      final session = await h.startPhone();
      h.injectPhoneFrames(20, sessionId: session);
      await h.settle();
      final sentWhileAuthorized = h.audioFramesSent;
      expect(sentWhileAuthorized, greaterThan(0));

      // Revoke locally mid-session: no new start, just the next frames.
      h.ellaPreferences.declineAiConsent();
      h.injectPhoneFrames(20, sessionId: session, firstFrameIndex: 20);
      await h.settle();

      expect(h.audioFramesSent, sentWhileAuthorized, reason: 'no frame may leave after authority is gone');
      expect(h.authority.isBound, isFalse);
      expect(h.runtime.lastRevocation.value?.reason, EllaCaptureRevocationReason.authorityNotCurrent);
      expect(h.hostApi.nativeRecording, isFalse, reason: 'revocation stops upstream capture via its public API');
      expect(h.provider.recordingState, isNot(RecordingState.record));
    });

    test('a superseded lease generation drops frames even though consent is still accepted', () async {
      expect(await h.bind(), isTrue);
      final session = await h.startPhone();
      h.injectPhoneFrames(10, sessionId: session);
      await h.settle();
      final sent = h.audioFramesSent;
      expect(sent, greaterThan(0));

      // The bound lease is stopped and restarted under the same account: its
      // generation moves, so the capture session bound to the old one is stale.
      final lease = h.leases.single;
      lease.stop();
      lease.start();
      expect(lease.hasCurrentAuthority, isTrue);
      h.injectPhoneFrames(10, sessionId: session, firstFrameIndex: 10);
      await h.settle();
      expect(h.audioFramesSent, sent);
      expect(h.authority.isBound, isFalse);
    });

    test('necklace packets are gated per packet before upstream WAL/socket processing', () async {
      expect(await h.bind(), isTrue);
      final link = await h.connectPendant();
      expect(h.provider.recordingState, RecordingState.deviceRecord);
      for (var i = 0; i < 5; i++) {
        link.emitAudio();
      }
      await h.settle();
      final sent = h.audioFramesSent;
      expect(sent, greaterThan(0));

      h.ellaPreferences.declineAiConsent();
      for (var i = 0; i < 5; i++) {
        link.emitAudio();
      }
      await h.settle();
      expect(h.audioFramesSent, sent);
      expect(h.authority.framesDropped, greaterThan(0));
      expect(link.openAudioSubscriptions, 0, reason: 'revocation stops upstream device recording');
    });

    test('the socket boundary itself refuses binary audio without authority (last line before egress)', () async {
      expect(await h.bind(), isTrue);
      await h.startPhone();
      final gated = EllaGatedTranscriptSocket(h.sockets.last.service, h.authority);
      final before = h.socket!.sentBinary.length;
      await gated.send(List<int>.filled(160, 1));
      expect(h.socket!.sentBinary.length, before + 1);

      h.authority.release();
      await gated.send(List<int>.filled(160, 2));
      await gated.sendText('{"type":"ping"}');
      expect(h.socket!.sentBinary.length, before + 1, reason: 'binary audio is dropped once unbound');
      expect(gated.droppedAudioFrames, 1);
      expect(h.socket!.sentText, contains('{"type":"ping"}'), reason: 'control text frames are not audio');
    });

    test('opening a transcription socket is refused while unbound', () async {
      final open = ellaGatedConversationSocketOpen(
        (
                {required codec,
                required sampleRate,
                required language,
                required force,
                source,
                clientConversationId,
                customSttConfig,
                geolocation}) async =>
            fail('the inner opener must not be called without authority'),
        h.authority,
      );
      expect(
        await open(codec: BleAudioCodec.pcm16, sampleRate: 16000, language: 'en', force: false),
        isNull,
      );
    });

    test('native mic frames never reach upstream callbacks after revocation, even if native keeps emitting', () async {
      expect(await h.bind(), isTrue);
      final session = await h.startPhone();
      h.authority.release();
      await h.settle();
      // A late native frame tagged with the old session id.
      h.injectPhoneFrames(5, sessionId: session, firstFrameIndex: 200);
      await h.settle();
      expect(h.provider.recordingState, isNot(RecordingState.record));
      expect(h.hostApi.stopCalls, greaterThan(0));
      h.mic.onStateChanged(PhoneMicCaptureState.idle, session);
      await h.settle();
    });
  });
}
