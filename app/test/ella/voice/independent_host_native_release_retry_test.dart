import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/services/native_dictation_output_admission.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'foreground_dictation_host_test.dart' show Engine;
import 'ephemeral_dictation_session_test.dart' show Rig;
import '../audio_ownership/standard_speech_held_listen_test.dart' show settleSpeech;
void main(){TestWidgetsFlutterBinding.ensureInitialized();
 test('Cancel retry clears host after exact native release settles beyond original public deadline',()async{
  const channel=MethodChannel('independent-fictional-host-native-release');
  final r=Rig(),e=Engine(),registry=AppOutputAdmission();final release=Completer<Object?>();MethodCall? releaseCall;
  Map<String,Object> reply(MethodCall c,String flag)=>{'version':1,'request_id':(c.arguments as Map)['request_id'] as String,flag:true};
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel,(c)async{
   if(c.method=='dictationReleaseOutput'){releaseCall=c;return release.future;}
   return reply(c,'held');
  });
  final adapter=NativeDictationOutputAdmission(admission:registry,channel:channel,deadline:const Duration(milliseconds:30));
  final host=ForegroundDictationHost(registration:ForegroundDictationRegistration(isPolicyEnabled:()=>true,reserveOutput:adapter.tryReserve,createEngine:()=>e),
   readEditor:()=>DictationEditorBinding(value:r.value,lease:r.lease,epoch:3,revision:r.revision),hasExactAuthority:()=>true,canStartFresh:()=>true,applyDraft:(_){r.applies++;});
  await host.start();expect(host.phase,ForegroundDictationPhase.listening);
  await host.finish();expect(host.phase,ForegroundDictationPhase.failed);expect(host.hasHeldOutput,true);expect(registry.dictationReserved,true);
  release.complete(reply(releaseCall!,'released'));await settleSpeech();
  expect(registry.dictationReserved,false,reason:'exact native release and raw preparation actually settled');
  Object? retryError;try{await host.retire();}catch(e){retryError=e;}
  final stillHeld=host.hasHeldOutput;host.dispose();r.lease.close();
  expect(retryError,isNull,reason:'exact confirmed late release must permit documented Cancel cleanup retry');
  expect(stillHeld,false);
 });}
