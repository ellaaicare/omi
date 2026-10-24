import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/backend/schema/structured.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'support/ella_upstream_capture_harness.dart';

ServerConversation snapshot(String id) => ServerConversation(id:id,
  createdAt:DateTime.utc(2026,10,23), structured:Structured('Fictional title','Fictional summary'),
  transcriptSegments:[TranscriptSegment(id:'segment-$id',text:'Synthetic $id words',
    speaker:'SPEAKER_0',isUser:false,personId:null,start:0,end:1,translations:const [])]);

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;
  setUp(() async {
    directory=await Directory.systemTemp.createTemp('transcript_rollover_');
    h=await EllaUpstreamCaptureHarness.boot(tempDir:directory);
    expect(await h.bind(), isTrue);
  });
  tearDown(() async {
    await h.dispose();
    if(directory.existsSync()) directory.deleteSync(recursive:true);
  });
  test('real coordinator preserves viewer across server rollover but rejects stale Finish',() async {
    await h.connectPendant();
    h.provider.applyInProgressConversation(snapshot('window-a'));
    final exact=h.runtime.captureSession!;
    final producer=h.provider.transcriptPresentationIdentity!;
    final lease=h.runtime.transcriptPresentationFor(exact)!;
    h.provider.applyInProgressConversation(snapshot('window-b'));
    expect(h.provider.transcriptPresentationIdentity,producer);
    expect(h.runtime.isCurrentCaptureSession(exact),isFalse);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isTrue);
    expect(h.provider.segments.single.text,'Synthetic window-b words');
    await h.runtime.finishCaptureSession(exact,stopDeviceIngress:false);
    expect(h.processCalls,0);
  });
  test('real manual Create leaves viewer current while same necklace keeps supplying audio',() async {
    final link=await h.connectPendant();
    for(var i=0;i<3;i++) { link.emitAudio(); }
    await h.settle();
    final exact=h.runtime.captureSession!;
    final producer=h.provider.transcriptPresentationIdentity!;
    final lease=h.runtime.transcriptPresentationFor(exact)!;
    final before=h.audioFramesSent;
    await h.runtime.finishCaptureSession(exact,stopDeviceIngress:false);
    await h.settle();
    expect(h.processCalls,1);
    expect(h.provider.transcriptPresentationIdentity,producer);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isTrue);
    h.provider.applyInProgressConversation(snapshot('after-explicit-create'));
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isTrue);
    await h.runtime.finishCaptureSession(exact,stopDeviceIngress:false);
    expect(h.processCalls,1);
    for(var i=0;i<3;i++) { link.emitAudio(); }
    await h.settle();
    expect(h.audioFramesSent,greaterThan(before));
    expect(h.disconnectedDevices,isEmpty);
  });
  test('real Stop then new phone capture retires old viewer with idle notification coalesced',() async {
    await h.startPhone();
    final exact=h.runtime.captureSession!;
    final oldProducer=h.provider.transcriptPresentationIdentity!;
    final lease=h.runtime.transcriptPresentationFor(exact)!;
    await h.runtime.stopPhoneCapture();
    await h.settle();
    // Deliberately do not observe the lease while idle.
    await h.startPhone();
    expect(h.provider.transcriptPresentationIdentity!.sessionKey,isNot(oldProducer.sessionKey));
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    expect(h.runtime.transcriptPresentationFor(h.runtime.captureSession!),isNotNull);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
  });
  test('real same-owner release and rebind cannot revive the old presentation lease',() async {
    await h.startPhone();
    final lease=h.runtime.transcriptPresentationFor(h.runtime.captureSession!)!;
    final epoch=h.authority.bindingEpoch;
    await h.runtime.releaseAccount();
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    expect(await h.bind(),isTrue);
    expect(h.authority.bindingEpoch,isNot(epoch));
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
  });
  test('real authenticated-owner change retires before exposing successor segments',() async {
    await h.connectPendant();
    final lease=h.runtime.transcriptPresentationFor(h.runtime.captureSession!)!;
    h.switchAccount(accountB);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    h.switchAccount(accountA);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    expect(h.processCalls,0);
  });
  test('real phone Pause and Resume preserve the same committed producer viewer',() async {
    await h.startPhone();
    final exact=h.runtime.captureSession!;
    final lease=h.runtime.transcriptPresentationFor(exact)!;
    await h.runtime.setCapturePaused(exact,paused:true);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isTrue);
    await h.runtime.setCapturePaused(exact,paused:false);
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isTrue);
    expect(h.processCalls,0);
  });
  test('real phone takeover retires the pendant viewer even if pendant later resumes',() async {
    await h.connectPendant();
    final lease=h.runtime.transcriptPresentationFor(h.runtime.captureSession!)!;
    await h.startPhone();
    expect(h.runtime.captureSession!.source,'phone');
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    await h.runtime.stopPhoneCapture();
    await h.settle();
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
  });
  test('real disconnect retires the viewer without creating another capture',() async {
    await h.connectPendant();
    final lease=h.runtime.transcriptPresentationFor(h.runtime.captureSession!)!;
    await h.runtime.disconnectNecklace();
    await h.settle();
    expect(h.runtime.isCurrentTranscriptPresentation(lease),isFalse);
    expect(h.disconnectedDevices,hasLength(1));
  });
  test('held real start cannot publish a staged presentation identity',() async {
    final hold=Completer<void>(),entered=Completer<void>();
    h.hostApi.holdNextStart=hold;h.hostApi.nextStartEntered=entered;
    final start=h.startPhone();
    try {
      await entered.future.timeout(const Duration(seconds:3));
      expect(h.provider.transcriptPresentationIdentity,isNull);
      expect(h.runtime.captureSession,isNull);
    } finally {
      if(!hold.isCompleted) hold.complete();
      await start;
    }
    expect(h.runtime.transcriptPresentationFor(h.runtime.captureSession!),isNotNull);
  });
}
