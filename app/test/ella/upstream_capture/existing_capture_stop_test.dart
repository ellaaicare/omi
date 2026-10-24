import 'dart:async';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart' as remembered;
import 'support/ella_upstream_capture_harness.dart';
import 'stop_escape_fonts.dart';
import '../../upstream_capture/support/capture/scripted_device_connection.dart';

Future<EllaUpstreamCaptureHarness> boot(WidgetTester tester) async =>
  (await tester.runAsync(() async {
    final directory=await Directory.systemTemp.createTemp('owned_stop_widget_');
    final h=await EllaUpstreamCaptureHarness.boot(tempDir:directory);
    expect(await h.bind(),isTrue);
    return h;
  }))!;
Future<void> cleanup(WidgetTester tester,EllaUpstreamCaptureHarness h) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.runAsync(() async {
    await h.dispose();
    if(h.tempDir.existsSync()) h.tempDir.deleteSync(recursive:true);
  });
}
Future<void> errorGate(WidgetTester tester,EllaUpstreamCaptureHarness h,String error,
    {double scale=1,void Function()? onSignOut}) async {
  final setup=EllaProvisioningProvider()..state=EllaProvisioningState.blocked..errorCode=error;
  addTearDown(setup.dispose);
  await tester.pumpWidget(ChangeNotifierProvider.value(value:setup,child:MaterialApp(
    theme:ellaThemeData(),localizationsDelegates:AppLocalizations.localizationsDelegates,
    supportedLocales:AppLocalizations.supportedLocales,
    builder:(context,child)=>MediaQuery(data:MediaQuery.of(context).copyWith(
      textScaler:TextScaler.linear(scale)),child:child!),
    home:EllaProvisioningGatePage(startOnMount:false,captureRuntime:h.runtime,
      authenticatedUidProvider:()=>h.authenticatedUid,onSignOutOverride:onSignOut ?? (){},
      readyChild:const SizedBox(key:Key('protected-home'))))));
  await tester.pump();
}

// A real controller-owned audio subscription with a bounded held cancellation.
// This does not replace runtime admission or Stop; it holds only the fake BLE port.
class HeldStopConnection extends ScriptedDeviceConnection {
  bool armed=false;
  int heldCancels=0;
  final entered=Completer<void>(), release=Completer<void>();
  @override Future<StreamSubscription?> getBleAudioBytesListener({required void Function(List<int>) onAudioBytesReceived}) async {
    final inner=await super.getBleAudioBytesListener(onAudioBytesReceived:onAudioBytesReceived);
    return HeldStopSubscription(inner!,() async {
      if(armed){heldCancels++;if(!entered.isCompleted)entered.complete();await release.future;}
    });
  }
}
class HeldStopSubscription implements StreamSubscription<dynamic> {
  HeldStopSubscription(this.inner,this.beforeCancel);
  final StreamSubscription<dynamic> inner;
  final Future<void> Function() beforeCancel;
  @override Future<void> cancel() async { await beforeCancel();await inner.cancel(); }
  @override void onData(void Function(dynamic)? f)=>inner.onData(f);
  @override void onError(Function? f)=>inner.onError(f);
  @override void onDone(void Function()? f)=>inner.onDone(f);
  @override void pause([Future<void>? f])=>inner.pause(f);
  @override void resume()=>inner.resume();
  @override bool get isPaused=>inner.isPaused;
  @override Future<E> asFuture<E>([E? value])=>inner.asFuture<E>(value);
}

void main() {
  setUpAll(fonts);
  for(final error in ['provider_unavailable','request_timeout','auth_required']) {
    testWidgets('existing Stop stays reachable in actual $error setup gate without opening Home',(tester) async {
      final h=await boot(tester);addTearDown(()=>cleanup(tester,h));
      await tester.runAsync(()=>h.connectPendant());
      // A lost frame lease is not a new grant: the authenticated producer owner
      // remains known, and Stop must work even when captureSession is null.
      h.leases.single.stop();
      expect(h.runtime.captureSession,isNull);
      expect(h.runtime.existingCaptureStopTarget(h.authenticatedUid),isNotNull);
      var signOuts=0;
      await errorGate(tester,h,error,onSignOut:()=>signOuts++);
      final button=find.byKey(const Key('existing-capture-stop'));
      await tester.ensureVisible(button);
      expect(button.hitTestable(),findsOneWidget);
      await tester.tap(button);await tester.pump();
      await tester.runAsync(()=>h.settle());await tester.pump();
      expect(h.provider.liveCaptureSource,isNull);
      expect(find.text('Recording stopped.'),findsOneWidget);
      expect(find.byKey(const Key('protected-home')),findsNothing);
      expect(h.processCalls,0);expect(h.disconnectedDevices,isEmpty);
      expect(signOuts,0);expect(tester.takeException(),isNull);
    });
  }
  testWidgets('held necklace Stop deduplicates taps and does not claim stopped early',(tester) async {
    final h=await boot(tester);addTearDown(()=>cleanup(tester,h));
    final link=HeldStopConnection();h.deviceConnection=link;
    await tester.runAsync(() async {
      await h.provider.streamDeviceRecording(device:EllaUpstreamCaptureHarness.pendant);
      await h.settle();
    });
    link.armed=true;
    addTearDown(() {if(!link.release.isCompleted)link.release.complete();});
    await errorGate(tester,h,'provider_unavailable');
    final button=find.byKey(const Key('existing-capture-stop'));
    await tester.ensureVisible(button);await tester.tap(button);await tester.tap(button);
    await tester.pump();await tester.runAsync(()=>link.entered.future.timeout(const Duration(seconds:3)));
    expect(link.heldCancels,1);expect(find.text('Recording stopped.'),findsNothing);
    link.release.complete();await tester.runAsync(()=>h.settle());await tester.pump();
    expect(link.openAudioSubscriptions,0);expect(link.heldCancels,1);
    expect(find.text('Recording stopped.'),findsOneWidget);
    expect(find.byKey(const Key('protected-home')),findsNothing);
  });
  testWidgets('stale account press cannot stop a known producer or show old result',(tester) async {
    final h=await boot(tester);addTearDown(()=>cleanup(tester,h));
    await tester.runAsync(()=>h.connectPendant());
    await errorGate(tester,h,'provider_unavailable');
    final button=find.byKey(const Key('existing-capture-stop'));
    await tester.ensureVisible(button);
    h.switchAccount(accountB);
    final before=h.hostApi.stopCalls;
    await tester.tap(button);await tester.pump();
    expect(h.hostApi.stopCalls,before);
    expect(h.deviceConnection!.openAudioSubscriptions,1);
    expect(find.text('Recording stopped.'),findsNothing);
    expect(find.byKey(const Key('existing-capture-stop-result')),findsNothing);
  });
  testWidgets('large text retains an accessible deliberate Stop in setup failure',(tester) async {
    final h=await boot(tester);addTearDown(()=>cleanup(tester,h));
    await tester.runAsync(()=>h.connectPendant());
    await errorGate(tester,h,'provider_unavailable',scale:3.5);
    final button=find.byKey(const Key('existing-capture-stop'));
    await tester.ensureVisible(button);await tester.pump();
    expect(button.hitTestable(),findsOneWidget);
    expect(tester.getSize(button).height,greaterThanOrEqualTo(48));
    expect(tester.takeException(),isNull);
  });
  test('guarded phone stop rejects a stale queued owner predicate',() async {
    final dir=await Directory.systemTemp.createTemp('owned_stop_runtime_');
    final h=await EllaUpstreamCaptureHarness.boot(tempDir:dir);
    try {
      expect(await h.bind(),isTrue);await h.startPhone();
      final stops=h.hostApi.stopCalls;
      final applied=await h.provider.stopStreamRecording(isCurrent:()=>false,resumeHandedOffPendant:false);
      expect(applied,isFalse);expect(h.hostApi.stopCalls,stops);expect(h.hostApi.nativeRecording,isTrue);
    } finally { await h.dispose();if(dir.existsSync())dir.deleteSync(recursive:true); }
  });
  test('an old Stop target cannot stop a later same-owner phone producer',() async {
    final dir=await Directory.systemTemp.createTemp('owned_stop_successor_');
    final h=await EllaUpstreamCaptureHarness.boot(tempDir:dir);
    try {
      expect(await h.bind(),isTrue);await h.startPhone();
      final target=h.runtime.existingCaptureStopTarget(accountA)!;
      expect(await h.runtime.stopExistingCapture(target),isTrue);
      await h.startPhone();final stops=h.hostApi.stopCalls;
      expect(await h.runtime.stopExistingCapture(target),isFalse);
      expect(h.hostApi.stopCalls,stops);expect(h.hostApi.nativeRecording,isTrue);
    } finally { await h.dispose();if(dir.existsSync())dir.deleteSync(recursive:true); }
  });
  test('deliberate Stop does not create a persistent automatic-resume latch',() async {
    final dir=await Directory.systemTemp.createTemp('owned_stop_reopen_');
    final h=await EllaUpstreamCaptureHarness.boot(tempDir:dir);
    try {
      expect(await h.bind(),isTrue);await h.connectPendant();
      await h.runtime.connectNecklace(accountA,EllaUpstreamCaptureHarness.pendant);
      h.ellaPreferences.btDevice=remembered.BtDevice.fromJson(EllaUpstreamCaptureHarness.pendant.toJson());
      await h.ellaPreferences.btDeviceOwnerBindingSet(accountA);await h.settle();
      final target=h.runtime.existingCaptureStopTarget(accountA)!;
      expect(await h.runtime.stopExistingCapture(target),isTrue);
      expect(h.provider.liveCaptureSource,isNull);
      h.runtime.didChangeAppLifecycleState(AppLifecycleState.resumed);
      await h.settle();
      expect(h.provider.liveCaptureSource,'omi');
      expect(h.deviceConnection!.openAudioSubscriptions,1);
    } finally { await h.dispose();if(dir.existsSync())dir.deleteSync(recursive:true); }
  });
}
