import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_auth_platform_interface/firebase_auth_platform_interface.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_core_platform_interface/test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/whisper_startup_diagnostics.dart';
import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/iris_whisper_stop_bar.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/services.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:fake_async/fake_async.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';

const device = '00000000-0000-4000-8000-000000000001';
const event = '00000000-0000-4000-8000-000000000002';

class Authority implements ExactAccountAuthorityVerifier {
  Authority(this.uid);
  @override
  final String uid;
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

class Media implements WhisperDeliveryMedia {
  final effects = <String>[];
  bool denyStop = false;
  Completer<void>? activation, admission;
  @override
  Future<void> stop(int g) async {
    effects.add('stop:$g');
    if (denyStop) throw StateError('unconfirmed');
  }

  @override
  Future<void> activate(WhisperDeliveryScope s, int g) async {
    effects.add('activate:${s.session}');
    await activation?.future;
  }

  @override
  Future<void> audio(WhisperDeliveryScope s, WhisperDeliveryEvent e, int g) async {
    effects.add('audio:${e.id}');
    await admission?.future;
  }

  @override
  Future<void> text(WhisperDeliveryScope s, WhisperDeliveryEvent e, int g) async {
    effects.add('text:${e.id}');
  }
}

class Harness {
  Authority authority = Authority('synthetic-owner');
  String lifetime = 'authentication-lifetime1';
  final media = Media();
  final requests = <({String method, String path, Map<String, dynamic> body})>[];
  final receipts = <String, Map<String, dynamic>>{};
  Map<String, dynamic>? saved;
  Map<String, dynamic>? offered;
  Map<String, dynamic>? state;
  void Function(String, Map<String, dynamic>)? corrupt;
  Completer<Map<String, dynamic>>? heldStop, heldPoll, heldClaim;
  bool failSeenWrite = false, failResumeWrite = false;
  Completer<void>? heldWrite;
  int writesStarted = 0;
  int counter = 10;
  String id() => '00000000-0000-4000-8000-${(counter++).toString().padLeft(12, '0')}';
  Map<String, dynamic> copy(Map<String, dynamic> m) => jsonDecode(jsonEncode(m)) as Map<String, dynamic>;
  Map<String, dynamic> fresh(Map<String, dynamic> fields) => {
        'contract': 1,
        'uid': authority.uid,
        'device_id': device,
        'session_id': id(),
        'client_epoch': fields['client_epoch'],
        'presentation': fields['presentation'],
        'revision': 1,
        'closed': false,
        'stop_request_id': null,
        'cursor': {'revision': 1, 'position': 0},
        'event': null
      };
  late WhisperDeliveryClient client = makeClient();
  WhisperDeliveryClient makeClient() => WhisperDeliveryClient(
      media: media,
      newId: id,
      authorityProvider: () => authority,
      lifetimeIdentity: (_) => lifetime,
      read: (_) async => saved == null ? null : copy(saved!),
      write: (_, m) async {
        writesStarted++;
        final hold = heldWrite;
        if (hold != null) await hold.future;
        if (failSeenWrite && (m['seen'] as List).isNotEmpty ||
            failResumeWrite && m['pending_stop'] == null && receipts.isNotEmpty) {
          throw StateError('synthetic disk failure');
        }
        saved = copy(m);
      },
      transport: (method, path, fields, a) async {
        requests.add((method: method, path: path, body: copy(Map.of(fields))));
        expect(fields.containsKey('uid'), isFalse);
        Map<String, dynamic> response;
        switch (path) {
          case 'devices':
            response = {'contract': 1, 'uid': a.uid, 'device_id': device};
            break;
          case 'sessions':
            state = fresh(fields);
            response = copy(state!);
            break;
          case 'next':
            response = {...copy(state!), 'event': offered};
            if (heldPoll != null) return heldPoll!.future;
            break;
          case 'claim':
            response = {
              ...copy(state!),
              'event_id': fields['event_id'],
              'position': fields['position'],
              'claim_request_id': fields['claim_request_id'],
              'status': 'claimed'
            };
            if (heldClaim != null) return heldClaim!.future;
            break;
          case 'ack':
            state!['cursor'] = {'revision': 1, 'position': fields['position']};
            offered = null;
            response = {
              ...copy(state!),
              'event_id': fields['event_id'],
              'ack_request_id': fields['ack_request_id'],
              'status': 'acknowledged'
            };
            break;
          case 'report':
            response = {
              ...copy(state!),
              'event_id': fields['event_id'],
              'request_id': fields['request_id'],
              'status': fields['status']
            };
            break;
          case 'stop':
            final request = fields['request_id'] as String;
            if (!receipts.containsKey(request)) {
              final terminal = {
                ...copy(state!),
                'closed': true,
                'revision': 2,
                'stop_request_id': request,
                'cursor': {'revision': 2, 'position': state!['cursor']['position']},
                'event': null
              };
              final resume = fields['resume'] == true ? fresh(state!) : null;
              response = {...terminal, 'resume': resume};
              receipts[request] = copy(response);
              if (resume != null) state = resume;
            } else {
              response = copy(receipts[request]!);
            }
            if (heldStop != null) return heldStop!.future;
            break;
          default:
            throw StateError('unexpected endpoint');
        }
        corrupt?.call(path, response);
        return response;
      });
  Future<void> start({WhisperPresentation presentation = WhisperPresentation.audio}) => client.start(presentation);
  void offer({bool text = false}) => offered = {
        'event_id': event,
        'position': 1,
        'url': text ? null : 'https://synthetic.invalid/server-tts.mp3',
        'message': 'Synthetic server-generated message',
        'priority': 'normal',
        'trigger_type': 'memory',
        'metadata': {}
      };
}

class _WhisperAuth extends FirebaseAuthPlatform {
  String? owner = 'synthetic-owner';
  Future<IdTokenResult> Function()? token;
  @override
  FirebaseAuthPlatform delegateFor({required FirebaseApp app}) => this;
  @override
  FirebaseAuthPlatform setInitialValues({PigeonUserDetails? currentUser, String? languageCode}) => this;
  @override
  UserPlatform? get currentUser => owner == null ? null : _WhisperUser(this, owner!);
}

class _WhisperMultiFactor extends MultiFactorPlatform {
  _WhisperMultiFactor(super.auth);
}

class _WhisperUser extends UserPlatform {
  _WhisperUser(this.auth, String owner)
      : super(
            auth,
            _WhisperMultiFactor(auth),
            PigeonUserDetails(
                userInfo: PigeonUserInfo(uid: owner, isAnonymous: false, isEmailVerified: false), providerData: []));
  final _WhisperAuth auth;
  @override
  Future<IdTokenResult> getIdTokenResult(bool forceRefresh) async =>
      auth.token == null ? IdTokenResult(PigeonIdTokenResult(authTimestamp: 1790000000000)) : await auth.token!();
}

class _DiagnosticMedia implements WhisperDeliveryMedia {
  _DiagnosticMedia(this.media, this.stage);
  final Media media;
  final String stage;
  @override
  Future<void> stop(int generation) => media.stop(generation);
  @override
  Future<void> activate(WhisperDeliveryScope scope, int generation) {
    if (stage == 'nativeActivate')
      throw PlatformException(code: 'STRICT_OWNER', message: 'private-native', details: 'private-token');
    return media.activate(scope, generation);
  }

  @override
  Future<void> audio(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation) =>
      media.audio(scope, event, generation);
  @override
  Future<void> text(WhisperDeliveryScope scope, WhisperDeliveryEvent event, int generation) =>
      media.text(scope, event, generation);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const nativeChannel = MethodChannel('iris.strict.runtime.synthetic');
  tearDown(() => WhisperStartupDiagnostics.testSink = null);
  test('diagnostic stream drops exception content, identifiers and unknown codes', () {
    final lines = <String>[];
    WhisperStartupDiagnostics.testSink = lines.add;
    for (final error in [
      PlatformException(code: 'private-owner-token', message: 'private transcript', details: {'uid': 'private-owner'}),
      FirebaseAuthException(code: 'private-owner', message: 'private-secret'),
      FormatException('private-memory'),
      StateError('private-password'),
      Exception('private-url')
    ]) {
      WhisperStartupDiagnostics.failure(WhisperStartupStage.stateRead, error);
    }
    WhisperStartupDiagnostics.record(WhisperStartupStage.deviceApi, WhisperStartupResult.failed,
        code: WhisperStartupCode.httpFailure, httpStatus: 503);
    expect(lines.join(), isNot(contains('private')));
    expect(lines.last, contains('http=503'));
  });

  group('real Firebase SDK startup wrappers', () {
    final auth = _WhisperAuth();
    setUpAll(() async {
      setupFirebaseCoreMocks();
      FirebaseAuthPlatform.instance = auth;
      await Firebase.initializeApp();
    });
    setUp(() async {
      auth.owner = 'synthetic-owner';
      auth.token = null;
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
    });
    for (final failure in ['none', 'owner-change', 'authority-change', 'missing-auth-time']) {
      test('startup with fresh SDK wrappers: $failure', () async {
        final lines = <String>[];
        WhisperStartupDiagnostics.testSink = lines.add;
        final authentication = FirebaseAuth.instance;
        final first = authentication.currentUser!, second = authentication.currentUser!;
        expect(first.uid, second.uid);
        expect(identical(first, second), isFalse);
        final h = Harness();
        auth.token = () async {
          if (failure == 'owner-change') auth.owner = 'replacement-private-owner';
          if (failure == 'authority-change') h.authority.current = false;
          return IdTokenResult(
              PigeonIdTokenResult(authTimestamp: failure == 'missing-auth-time' ? null : 1790000000000));
        };
        const channel = MethodChannel('whisper-startup-sdk-fixture');
        final calls = <String>[];
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel,
            (call) async {
          calls.add(call.method);
          return call.method == 'strictStop' ? {'stopped': true} : {'accepted': true};
        });
        addTearDown(() =>
            TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
        final original = h.client;
        final runtime = WhisperDeliveryRuntime.testing(
            transport: original.transport,
            read: original.read,
            write: original.write,
            authorityProvider: () => h.authority,
            channel: channel,
            authentication: authentication);
        h.client = runtime.client;
        if (failure == 'none') {
          await runtime.startOutput(explicitOn: true);
          expect(runtime.client.ready, isTrue);
          expect(calls, contains('strictActivate'));
          expect(lines, contains(contains('stage=ready result=ok')));
          await runtime.stopOutput(resume: false);
        } else {
          await expectLater(runtime.startOutput(explicitOn: true), throwsStateError);
          expect(runtime.client.ready, isFalse);
          expect(h.requests, isEmpty);
          expect(calls, isEmpty);
          expect(lines, contains(contains('stage=authenticationReceipt result=failed')));
        }
        expect(lines.join(), isNot(contains('synthetic-owner')));
        expect(lines.join(), isNot(contains('replacement-private-owner')));
      });
    }
  });

  for (final failingStage in [
    'nativeStop',
    'stateRead',
    'preferencesPersist',
    'deviceApi',
    'sessionApi',
    'stateValidation',
    'deviceValidation',
    'sessionValidation',
    'nativeActivate'
  ]) {
    test('startup diagnostics identify $failingStage and preserve fail-closed output', () async {
      final lines = <String>[];
      WhisperStartupDiagnostics.testSink = lines.add;
      final h = Harness();
      final original = h.client;
      if (failingStage == 'nativeStop') h.media.denyStop = true;
      final client = WhisperDeliveryClient(
          authorityProvider: () => h.authority,
          media: _DiagnosticMedia(h.media, failingStage),
          newId: h.id,
          lifetimeIdentity: (_) => 'synthetic-lifetime',
          read: (owner) async {
            if (failingStage == 'stateRead') throw const FormatException('private-json');
            if (failingStage == 'stateValidation')
              return {
                'lifetime': 'synthetic-lifetime',
                'pending_stop': {'request_id': 'private-bad'},
                'scope': null
              };
            return null;
          },
          write: (owner, state) async {
            if (failingStage == 'preferencesPersist') throw StateError('private-storage');
            await original.write(owner, state);
          },
          transport: (method, path, fields, authority) async {
            if ((failingStage == 'deviceApi' && path == 'devices') ||
                (failingStage == 'sessionApi' && path == 'sessions')) throw TimeoutException('private-api');
            final result = await original.transport(method, path, fields, authority);
            if (failingStage == 'deviceValidation' && path == 'devices') result['contract'] = 0;
            if (failingStage == 'sessionValidation' && path == 'sessions') result['session_id'] = 'private-invalid';
            return result;
          });
      await expectLater(client.start(WhisperPresentation.audio), throwsA(anything));
      expect(client.ready, isFalse);
      expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
      expect(lines, contains(contains('stage=$failingStage result=failed')));
      expect(lines.join(), isNot(contains('private')));
    });
  }

  test('pending Stop diagnosis preserves immutable reconciliation and fail-closed output', () async {
    final lines = <String>[];
    WhisperStartupDiagnostics.testSink = lines.add;
    final h = Harness();
    final base = h.client;
    h.client = WhisperDeliveryClient(
        authorityProvider: () => h.authority,
        media: h.media,
        read: base.read,
        write: base.write,
        newId: h.id,
        transport: (method, path, fields, authority) async {
          if (path == 'stop') throw TimeoutException('private-stop');
          return base.transport(method, path, fields, authority);
        });
    await h.start();
    await expectLater(h.client.stop(resume: false), throwsA(isA<TimeoutException>()));
    final pending = jsonEncode(h.saved!['pending_stop']);
    lines.clear();
    await expectLater(h.client.start(WhisperPresentation.audio, explicitOn: true), throwsA(isA<TimeoutException>()));
    expect(h.client.ready, isFalse);
    expect(h.client.stopPending, isTrue);
    expect(jsonEncode(h.saved!['pending_stop']), pending);
    expect(lines, contains(contains('stage=pendingStop result=failed code=timeout')));
    expect(lines.join(), isNot(contains('private-stop')));
  });

  test('failed diagnostic writer cannot grant authority or prevent Stop', () async {
    WhisperStartupDiagnostics.testSink = (_) => throw StateError('private-writer');
    final h = Harness();
    await h.start();
    expect(h.client.ready, isTrue);
    await h.client.stop(resume: false);
    expect(h.client.ready, isFalse);
    expect(h.client.stopPending, isFalse);
  });

  for (final heldMethod in ['strictActivate', 'strictStop']) {
    test('production runtime late $heldMethod receipt cannot damage replacement owner Stop', () async {
      final h = Harness();
      final original = h.client;
      final calls = <MethodCall>[];
      final held = Completer<Map<String, dynamic>>();
      bool hold = heldMethod == 'strictActivate';
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(nativeChannel,
          (call) async {
        calls.add(call);
        if (call.method == heldMethod && hold) {
          hold = false;
          return held.future;
        }
        return call.method == 'strictStop' ? {'stopped': true} : {'accepted': true};
      });
      addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(nativeChannel, null));
      final runtime = WhisperDeliveryRuntime.testing(
          transport: original.transport,
          read: original.read,
          write: original.write,
          authorityProvider: () => h.authority,
          channel: nativeChannel);
      h.client = runtime.client;
      Future<void>? oldOperation;
      Future<void>? oldFailure;
      if (heldMethod == 'strictActivate') {
        oldOperation = h.start();
        oldFailure = expectLater(oldOperation, throwsStateError);
        await Future<void>.delayed(Duration.zero);
      } else {
        await h.start();
        hold = true;
        oldOperation = h.client.stop();
        oldFailure = expectLater(oldOperation, throwsStateError);
        await Future<void>.delayed(Duration.zero);
      }
      h.authority.current = false;
      h.authority = Authority('replacement-owner');
      await h.client.retireAuthority();
      await h.start();
      final replacement = h.client.scope!;
      held.complete(heldMethod == 'strictStop' ? {'stopped': true} : {'accepted': true});
      await oldFailure;
      expect(h.client.ready, isTrue);
      await h.client.stop();
      final stopCalls = calls.where((c) => c.method == 'strictStop').toList();
      expect((stopCalls.last.arguments as Map)['uid'], replacement.uid);
      expect((stopCalls.last.arguments as Map)['client_epoch'], replacement.epoch);
      expect(h.client.ready, isTrue);
    });
  }

  test('production permission hold stops before alert and clears hold-window backlog before polling', () async {
    final h = Harness(), original = h.client;
    final native = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(nativeChannel,
        (call) async {
      native.add(call);
      return call.method == 'strictStop' ? {'stopped': true} : {'accepted': true};
    });
    addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(nativeChannel, null));
    final runtime = WhisperDeliveryRuntime.testing(
        transport: original.transport,
        read: original.read,
        write: original.write,
        authorityProvider: () => h.authority,
        channel: nativeChannel);
    h.client = runtime.client;
    await h.start();
    final before = h.requests.where((r) => r.path == 'stop').length;
    final alert = Completer<bool>();
    var entered = false;
    final operation = runtime.withoutWhisperOutput(() {
      entered = true;
      expect(native.last.method, 'strictActivate');
      expect(h.requests.where((r) => r.path == 'stop').length, before + 1);
      expect(h.requests.lastWhere((r) => r.path == 'stop').body['resume'], true);
      return alert.future;
    });
    await Future<void>.delayed(Duration.zero);
    expect(entered, true);
    final during = h.client.scope!.session;
    alert.complete(true);
    expect(await operation, true);
    expect(h.requests.where((r) => r.path == 'stop').length, before + 2);
    expect(h.client.scope!.session, isNot(during));
    await runtime.stopOutput(resume: false);
  });
  test('claim predicate and playing presentation reject silent same-UID authority retirement', () async {
    final h = Harness();
    await h.start();
    h.offer();
    await h.client.poll();
    expect(h.client.hasCurrentClaim(event), true);
    expect(h.client.hasCurrentClaim(device), false);
    h.authority.current = false;
    expect(h.client.hasCurrentClaim(event), false);
    expect(h.client.scope, isNull);
  });

  test('native started fact requires current admitted claim and rejects silent lease retirement', () async {
    final h = Harness(), original = h.client;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        nativeChannel, (call) async => call.method == 'strictStop' ? {'stopped': true} : {'accepted': true});
    addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(nativeChannel, null));
    final runtime = WhisperDeliveryRuntime.testing(
        transport: original.transport,
        read: original.read,
        write: original.write,
        authorityProvider: () => h.authority,
        channel: nativeChannel);
    h.client = runtime.client;
    await h.start();
    h.offer();
    await h.client.poll();
    final scope = h.client.scope!;
    Future<void> fact(String id, {String status = "started"}) async {
      final done = Completer<void>();
      // Inject a native-channel fact, not a physical player/acoustic result.
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.handlePlatformMessage(
          nativeChannel.name,
          const StandardMethodCodec().encodeMethodCall(MethodCall('strictPlaybackFact', {
            'uid': scope.uid,
            'client_epoch': scope.epoch,
            'session_id': scope.session,
            'generation': h.client.generation,
            'event_id': id,
            'status': status
          })),
          (_) => done.complete());
      await done.future;
    }

    await fact(device);
    expect(runtime.playing, false);
    await fact(event);
    expect(runtime.playing, true);
    await fact(event, status: 'completed');
    expect(runtime.playing, false);
    await fact(event);
    expect(runtime.playing, false, reason: 'late duplicate started must not revive completed playback');
    final reports = h.requests.where((r) => r.path == 'report').length;
    h.authority.current = false;
    expect(runtime.playing, false);
    await fact(event);
    expect(runtime.playing, false);
    expect(h.requests.where((r) => r.path == 'report').length, reports);
  });

  testWidgets('admitted Text-only output appears on Chat, clears on silent retirement, uses no native audio',
      (tester) async {
    final h = Harness(), original = h.client;
    final calls = <String>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(nativeChannel,
        (call) async {
      calls.add(call.method);
      return call.method == 'strictStop' ? {'stopped': true} : {'accepted': true};
    });
    addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(nativeChannel, null));
    final runtime = WhisperDeliveryRuntime.testing(
        transport: original.transport,
        read: original.read,
        write: original.write,
        authorityProvider: () => h.authority,
        channel: nativeChannel);
    h.client = runtime.client;
    await h.start(presentation: WhisperPresentation.textOnly);
    h.offer(text: true);
    await h.client.poll();
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: IrisWhisperMessage(runtime: runtime))));
    await tester.pump();
    expect(find.text('Synthetic server-generated message'), findsOneWidget);
    expect(find.text('Ella · Whisper'), findsOneWidget);
    expect(find.text('Whisper playing'), findsNothing);
    expect(calls.where((c) => c == 'strictAudio'), isEmpty);
    h.authority.current = false;
    await tester.pump(const Duration(milliseconds: 251));
    expect(find.text('Synthetic server-generated message'), findsNothing);
    h.authority = Authority('replacement-owner');
    await h.client.retireAuthority();
    await h.start(presentation: WhisperPresentation.textOnly);
    expect(runtime.message, isNull, reason: 'new output scope never adopts old admitted text');
    await tester.pumpWidget(const SizedBox());
  });

  test('audio uses admitted server URL after claim and durable seen; ACK advances cursor', () async {
    final h = Harness();
    await h.start();
    h.offer();
    await h.client.poll();
    expect(h.requests.map((r) => r.path), ['devices', 'sessions', 'next', 'claim', 'ack']);
    expect(h.saved!['seen'], [event]);
    expect(h.media.effects.last, 'audio:$event');
    expect(h.saved!['cursor'], 1);
    expect(h.client.ready, isTrue);
    await h.client.report(event, 'started');
    await h.client.report(event, 'started');
    expect(h.requests.where((r) => r.path == 'report').length, 1);
  });
  test('text_only displays message with no audio/TTS effect', () async {
    final h = Harness();
    await h.start(presentation: WhisperPresentation.textOnly);
    h.offer(text: true);
    await h.client.poll();
    expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
    expect(h.media.effects.last, 'text:$event');
    expect(h.requests[1].body['presentation'], 'text_only');
  });
  test('empty response produces no claim or media', () async {
    final h = Harness();
    await h.start();
    await h.client.poll();
    expect(h.requests.last.path, 'next');
    expect(h.media.effects.where((e) => e.startsWith('audio:') || e.startsWith('text:')), isEmpty);
  });
  for (final field in [
    'uid',
    'device_id',
    'session_id',
    'client_epoch',
    'presentation',
    'revision',
    'contract',
    'closed',
    'cursor',
    'stop_request_id'
  ]) {
    test('wrong or boolean poll $field fails closed without legacy fallback', () async {
      final h = Harness();
      await h.start();
      h.offer();
      h.corrupt = (path, m) {
        if (path == 'next') m[field] = true;
      };
      await expectLater(h.client.poll(), throwsStateError);
      expect(h.client.ready, isFalse);
      expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
      expect(h.requests.any((r) => r.path.contains('next-audio')), isFalse);
    });
  }
  test('audio cannot fallback to TTS message when URL absent', () async {
    final h = Harness();
    await h.start();
    h.offer(text: true);
    await expectLater(h.client.poll(), throwsStateError);
    expect(h.media.effects.where((e) => e.startsWith('text:')), isEmpty);
  });
  test('text_only rejects unexpected audio URL', () async {
    final h = Harness();
    await h.start(presentation: WhisperPresentation.textOnly);
    h.offer();
    await expectLater(h.client.poll(), throwsStateError);
  });
  test('seen persistence failure prevents media and ACK', () async {
    final h = Harness();
    await h.start();
    h.offer();
    h.failSeenWrite = true;
    await expectLater(h.client.poll(), throwsStateError);
    expect(h.requests.where((r) => r.path == 'ack'), isEmpty);
    expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
  });
  test('Stop closes local media first, validates complete ACK, and later eligible event plays', () async {
    final h = Harness();
    await h.start();
    final old = h.client.scope!.session;
    await h.client.stop();
    expect(h.client.scope!.session, isNot(old));
    expect(h.client.acknowledgedStopRequest, isNotNull);
    expect(h.saved!['pending_stop'], isNull);
    h.offer();
    await h.client.poll();
    expect(h.media.effects.last, 'audio:$event');
  });
  for (final field in ['presentation', 'session_id', 'revision', 'cursor', 'event', 'stop_request_id']) {
    test('malformed fresh resume $field retains original Stop and closed fence', () async {
      final h = Harness();
      await h.start();
      h.corrupt = (path, m) {
        if (path == 'stop') m['resume'][field] = true;
      };
      await expectLater(h.client.stop(), throwsStateError);
      expect(h.client.ready, isFalse);
      expect(h.client.stopPending, isTrue);
      expect(h.client.acknowledgedStopRequest, isNull);
    });
  }
  test('terminal owner/presentation/stop request must match', () async {
    for (final field in ['uid', 'presentation', 'stop_request_id']) {
      final h = Harness();
      await h.start();
      h.corrupt = (path, m) {
        if (path == 'stop') m[field] = 'wrong';
      };
      await expectLater(h.client.stop(), throwsStateError);
      expect(h.client.ready, isFalse);
    }
  });
  test('unknown Stop outcome retries identical original request, not fresh resumed work', () async {
    final h = Harness();
    await h.start();
    h.heldStop = Completer();
    final pending = h.client.stop();
    final failed = expectLater(pending, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    final first = h.requests.last.body;
    h.heldStop!.completeError(StateError('uncertain'));
    await failed;
    h.heldStop = null;
    await h.client.reconcileStop();
    expect(h.requests.last.body, first);
    expect(h.client.ready, isTrue);
  });
  test('persisted Stop restores original tuple and request across same authentication lifetime', () async {
    final h = Harness();
    await h.start();
    h.heldStop = Completer();
    final pending = h.client.stop();
    final failed = expectLater(pending, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    final first = h.requests.last.body;
    h.heldStop!.completeError(StateError('uncertain'));
    await failed;
    h.heldStop = null;
    h.client = h.makeClient();
    await h.start();
    expect(h.requests.last.path, 'stop');
    expect(h.requests.last.body, first);
    expect(h.client.ready, isTrue);
  });
  test('resume commit failure keeps immutable Stop retry and silences opened native ticket', () async {
    final h = Harness();
    await h.start();
    h.failResumeWrite = true;
    await expectLater(h.client.stop(), throwsStateError);
    expect(h.client.stopPending, isTrue);
    expect(h.client.ready, isFalse);
    expect(h.media.effects.last, startsWith('stop:'));
    h.failResumeWrite = false;
    await h.client.reconcileStop();
    expect(h.client.ready, isTrue);
    expect(h.requests.where((r) => r.path == 'stop').length, 2);
  });
  test('Off during held Stop immediately retires output and delayed ACK cannot resume', () async {
    final h = Harness();
    await h.start();
    h.heldStop = Completer();
    final pending = h.client.stop();
    final failed = expectLater(pending, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    await h.client.stop(resume: false);
    expect(h.client.ready, isFalse);
    expect(h.media.effects.last, startsWith('stop:'));
    h.heldStop!.complete(h.receipts.values.single);
    await failed;
    h.heldStop = null;
    final activationCount = h.media.effects.where((e) => e.startsWith('activate:')).length;
    await h.client.reconcileStop();
    expect(h.client.ready, isFalse);
    expect(h.media.effects.where((e) => e.startsWith('activate:')).length, activationCount);
  });
  test('late poll after Stop does not claim or play', () async {
    final h = Harness();
    await h.start();
    h.offer();
    final stale = {...h.copy(h.state!), 'event': h.offered};
    h.heldPoll = Completer();
    final p = h.client.poll();
    final fail = expectLater(p, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    await h.client.stop();
    h.heldPoll!.complete(stale);
    await fail;
    expect(h.requests.where((r) => r.path == 'claim'), isEmpty);
    expect(h.client.ready, isTrue);
  });
  test('account ABA rotates epoch and stale native/media callbacks never report', () async {
    final h = Harness();
    await h.start();
    final old = h.client.scope!.epoch;
    h.offer();
    await h.client.poll();
    h.authority.current = false;
    await h.client.retireAuthority();
    h.authority = Authority('foreign-owner');
    h.lifetime = 'lifetime2';
    await h.start();
    await h.client.report(event, 'started');
    expect(h.requests.where((r) => r.path == 'report'), isEmpty);
    h.authority.current = false;
    await h.client.retireAuthority();
    h.authority = Authority('synthetic-owner');
    h.lifetime = 'lifetime3';
    await h.start();
    expect(h.client.scope!.epoch, isNot(old));
  });
  test('native Stop failure prevents request and server ACK claim', () async {
    final h = Harness();
    await h.start();
    h.media.denyStop = true;
    await expectLater(h.client.stop(), throwsStateError);
    expect(h.requests.where((r) => r.path == 'stop'), isEmpty);
    expect(h.client.acknowledgedStopRequest, isNull);
    expect(h.client.ready, isFalse);
  });
  test('confirmed saved Off forgets uncertain Stop and next On rotates epoch', () async {
    final h = Harness();
    await h.start();
    final oldEpoch = h.client.scope!.epoch;
    h.corrupt = (path, response) {
      if (path == 'stop') response['uid'] = 'foreign-owner';
    };
    await expectLater(h.client.stop(resume: false), throwsStateError);
    expect(h.client.stopPending, isTrue);
    h.corrupt = null;
    await h.client.savedOffConfirmed(h.authority);
    expect(h.client.stopPending, isFalse);
    expect(h.client.ready, isFalse);
    await h.client.start(WhisperPresentation.audio, explicitOn: true);
    expect(h.client.scope!.epoch, isNot(oldEpoch));
  });
  test('foreign saved Off receipt cannot retire current owner output', () async {
    final h = Harness();
    await h.start();
    final generation = h.client.generation;
    await h.client.savedOffConfirmed(Authority('foreign-owner'));
    expect(h.client.generation, generation);
    expect(h.client.ready, isTrue);
  });
  test('held seen write cannot overwrite a newer Stop fence snapshot', () async {
    final h = Harness();
    await h.start();
    h.offer();
    h.heldWrite = Completer<void>();
    final pollFailure = expectLater(h.client.poll(), throwsStateError);
    await Future<void>.delayed(Duration.zero);
    final writesBeforeStop = h.writesStarted;
    final stop = h.client.stop();
    await Future<void>.delayed(Duration.zero);
    expect(h.writesStarted, writesBeforeStop);
    expect(h.media.effects.last, startsWith('stop:'));
    h.heldWrite!.complete();
    h.heldWrite = null;
    await pollFailure;
    await stop;
    expect(h.saved!['pending_stop'], isNull);
    expect(h.saved!['scope']['session_id'], h.client.scope!.session);
    expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
  });
  test('held claim after Stop never admits audio or ACK on retired session', () async {
    final h = Harness();
    await h.start();
    h.offer();
    final old = h.copy(h.state!);
    h.heldClaim = Completer();
    final failure = expectLater(h.client.poll(), throwsStateError);
    await Future<void>.delayed(Duration.zero);
    final claim = h.requests.last.body;
    await h.client.stop();
    h.heldClaim!.complete(
        {...old, 'event_id': event, 'position': 1, 'claim_request_id': claim['claim_request_id'], 'status': 'claimed'});
    await failure;
    expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
    expect(h.requests.where((r) => r.path == 'ack'), isEmpty);
    expect(h.client.ready, isTrue);
  });
  test('restored Off latch reconciles original Stop without opening another session', () async {
    final h = Harness();
    await h.start();
    h.corrupt = (path, response) {
      if (path == 'stop') response['uid'] = 'foreign-owner';
    };
    await expectLater(h.client.stop(resume: false), throwsStateError);
    h.corrupt = null;
    final sessions = h.requests.where((r) => r.path == 'sessions').length;
    h.client = h.makeClient();
    await h.start();
    expect(h.client.ready, isFalse);
    expect(h.requests.where((r) => r.path == 'sessions').length, sessions);
    await h.client.start(WhisperPresentation.audio, explicitOn: true);
    expect(h.client.ready, isTrue);
    expect(h.requests.where((r) => r.path == 'sessions').length, sessions + 1);
  });
  test('direct cold explicit On reconciles old Off before enabling resumable output', () async {
    final h = Harness();
    await h.start();
    h.corrupt = (path, response) {
      if (path == 'stop') response['uid'] = 'foreign-owner';
    };
    await expectLater(h.client.stop(resume: false), throwsStateError);
    h.corrupt = null;
    h.client = h.makeClient();
    await h.client.start(WhisperPresentation.audio, explicitOn: true);
    expect(h.client.ready, isTrue);
    expect(h.saved!['resume_allowed'], isTrue);
    await h.client.stop();
    expect(h.client.ready, isTrue);
  });
  test('confirmed Off remains closed on automatic startup and same-process retry', () async {
    final h = Harness();
    await h.start();
    await h.client.savedOffConfirmed(h.authority);
    final sessions = h.requests.where((r) => r.path == 'sessions').length;
    await h.start();
    expect(h.client.ready, isFalse);
    h.client = h.makeClient();
    await h.start();
    expect(h.client.ready, isFalse);
    expect(h.requests.where((r) => r.path == 'sessions').length, sessions);
    await h.client.start(WhisperPresentation.audio, explicitOn: true);
    expect(h.client.ready, isTrue);
  });
  test('hung retired media receipt cannot prevent fresh-session polling', () {
    fakeAsync((clock) {
      final h = Harness();
      h.start();
      clock.flushMicrotasks();
      h.offer();
      h.media.admission = Completer<void>();
      Object? error;
      h.client.poll().catchError((Object e) {
        error = e;
      });
      clock.flushMicrotasks();
      h.client.stop();
      clock.flushMicrotasks();
      h.offered = null;
      final reads = h.requests.where((r) => r.path == 'next').length;
      h.client.poll();
      clock.flushMicrotasks();
      expect(h.requests.where((r) => r.path == 'next').length, reads + 1);
      clock.elapse(const Duration(seconds: 10));
      clock.flushMicrotasks();
      expect(error, isA<TimeoutException>());
      expect(h.client.ready, isTrue);
    });
  });
  test('unknown claim timeout is finite and never releases audio', () {
    fakeAsync((clock) {
      final h = Harness();
      h.start();
      clock.flushMicrotasks();
      h.offer();
      h.heldClaim = Completer();
      Object? error;
      h.client.poll().catchError((Object e) {
        error = e;
      });
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 10));
      clock.flushMicrotasks();
      expect(error, isA<TimeoutException>());
      expect(h.client.ready, isFalse);
      expect(h.media.effects.where((e) => e.startsWith('audio:')), isEmpty);
    });
  });
}
