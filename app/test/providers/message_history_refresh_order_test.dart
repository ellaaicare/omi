import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _Authority implements AccountCommitAuthority {
  const _Authority(this.uid, this.current);
  @override
  final String uid;
  final bool Function() current;
  @override
  bool isCurrent() => current();
  @override
  bool isExactCurrent() => current();
}

ServerMessage _message(String id, {String text = 'Synthetic history text', MessageSender sender = MessageSender.ai}) =>
    ServerMessage(id, DateTime.utc(2026, 10, 12), text, sender, MessageType.text, null, false, [], [], []);
EllaServiceResult<EllaChatHistoryPage> _result(bool success, String id, ClientApiFailure failure) => success
    ? EllaServiceResult.success(EllaChatHistoryPage(messages: [_message(id)], hasMore: false))
    : EllaServiceResult.failure(failure);
const _oldFailure = ClientApiFailure(ClientApiFailureKind.unavailable, statusCode: 429, retryable: true);
const _newFailure = ClientApiFailure(ClientApiFailureKind.unavailable, statusCode: 503, retryable: true);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  PlatformManager.initializeForTesting();
  Env.init(_TestEnv());
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'owner-a'});
    await SharedPreferencesUtil.init();
  });
  for (final oldSuccess in [false, true]) {
    for (final newSuccess in [false, true]) {
      for (final oldFirst in [false, true]) {
        test(
            'latest refresh owns timeline/error/loading oldSuccess=$oldSuccess newSuccess=$newSuccess oldFirst=$oldFirst',
            () async {
          final prefs = SharedPreferencesUtil();
          prefs.cachedMessages = [_message('cache')];
          final old = Completer<EllaServiceResult<EllaChatHistoryPage>>(),
              fresh = Completer<EllaServiceResult<EllaChatHistoryPage>>();
          var calls = 0;
          final provider = MessageProvider(
              activeAuthority: () => _Authority('owner-a', () => prefs.uid == 'owner-a'),
              ellaChatHistoryFetcher: (
                      {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
                  calls++ == 0 ? old.future : fresh.future);
          addTearDown(provider.dispose);
          final draftEpoch = provider.localDraftResetGeneration;
          final first = provider.refreshMessages(), second = provider.refreshMessages();
          if (oldFirst) {
            old.complete(_result(oldSuccess, 'old', _oldFailure));
            await first;
            expect(provider.messages.single.id, 'cache');
            expect(provider.lastHistoryFailure, isNull);
            expect(provider.isLoadingMessages, isTrue);
            fresh.complete(_result(newSuccess, 'new', _newFailure));
            await second;
          } else {
            fresh.complete(_result(newSuccess, 'new', _newFailure));
            await second;
            old.complete(_result(oldSuccess, 'old', _oldFailure));
            await first;
          }
          expect(provider.messages.single.id, newSuccess ? 'new' : 'cache');
          expect(prefs.cachedMessages.single.id, newSuccess ? 'new' : 'cache');
          expect(provider.lastHistoryFailure, newSuccess ? isNull : same(_newFailure));
          expect(provider.isLoadingMessages, isFalse);
          expect(provider.localDraftResetGeneration, draftEpoch);
        });
      }
    }
  }
  for (final oldSuccess in [false, true]) {
    for (final oldFirst in [false, true]) {
      test('account transition rejects old history oldSuccess=$oldSuccess oldFirst=$oldFirst', () async {
        final prefs = SharedPreferencesUtil();
        prefs.cachedMessages = [_message('owner-a-cache')];
        final old = Completer<EllaServiceResult<EllaChatHistoryPage>>(),
            fresh = Completer<EllaServiceResult<EllaChatHistoryPage>>();
        final requested = <String>[];
        final provider = MessageProvider(activeAuthority: () {
          final uid = prefs.uid;
          return _Authority(uid, () => prefs.uid == uid);
        }, ellaChatHistoryFetcher: (
            {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) {
          requested.add(expectedAuthenticatedUid);
          return requested.length == 1 ? old.future : fresh.future;
        });
        addTearDown(provider.dispose);
        final first = provider.refreshMessages();
        EllaAccountCommitBarrier.quiesceForAccountTransition();
        prefs.uid = 'owner-b';
        prefs.cachedMessages = [_message('owner-b-cache')];
        final second = provider.refreshMessages();
        if (oldFirst) {
          old.complete(_result(oldSuccess, 'owner-a-old', _oldFailure));
          await first;
          expect(provider.isLoadingMessages, isTrue);
          expect(provider.messages.single.id, 'owner-b-cache');
        }
        fresh.complete(_result(true, 'owner-b-new', _newFailure));
        await second;
        if (!oldFirst) {
          old.complete(_result(oldSuccess, 'owner-a-old', _oldFailure));
          await first;
        }
        expect(requested, ['owner-a', 'owner-b']);
        expect(provider.messages.single.id, 'owner-b-new');
        expect(prefs.cachedMessages.single.id, 'owner-b-new');
        expect(prefs.getString('cachedMessagesUid'), 'owner-b');
        expect(provider.lastHistoryFailure, isNull);
        expect(provider.isLoadingMessages, isFalse);
      });
    }
  }
  test('exact account authority rejects old result even without a reset or newer request', () async {
    final prefs = SharedPreferencesUtil();
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final provider = MessageProvider(
        activeAuthority: () => _Authority('owner-a', () => prefs.uid == 'owner-a'),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
            pending.future);
    addTearDown(provider.dispose);
    final first = provider.refreshMessages();
    prefs.uid = 'owner-b';
    prefs.cachedMessages = [_message('b-retained')];
    pending.complete(_result(false, 'old', _oldFailure));
    await first;
    expect(provider.lastHistoryFailure, isNull);
    expect(prefs.cachedMessages.single.id, 'b-retained');
  });
  test('same-UID exact binding drift rejects old success without a reset', () async {
    final prefs = SharedPreferencesUtil();
    prefs.cachedMessages = [_message('retained')];
    var binding = 0;
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final provider = MessageProvider(
        activeAuthority: () {
          final enteredBinding = binding;
          return _Authority(prefs.uid, () => binding == enteredBinding);
        },
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
            pending.future);
    addTearDown(provider.dispose);
    final read = provider.refreshMessages();
    binding++;
    pending.complete(_result(true, 'old-binding', _oldFailure));
    await read;
    expect(provider.messages.single.id, 'retained');
    expect(prefs.cachedMessages.single.id, 'retained');
    expect(provider.lastHistoryFailure, isNull);
  });
  test('same-UID binding retirement rejects old failure while new request remains loading', () async {
    final prefs = SharedPreferencesUtil();
    final old = Completer<EllaServiceResult<EllaChatHistoryPage>>(),
        fresh = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var calls = 0;
    final provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
            calls++ == 0 ? old.future : fresh.future);
    addTearDown(provider.dispose);
    final first = provider.refreshMessages();
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    final second = provider.refreshMessages();
    old.complete(_result(false, 'old', _oldFailure));
    await first;
    expect(provider.isLoadingMessages, isTrue);
    expect(provider.lastHistoryFailure, isNull);
    fresh.complete(_result(true, 'new', _newFailure));
    await second;
    expect(provider.messages.single.id, 'new');
  });
  test('reentrant loading notification starts latest request and prevents superseded fetch', () async {
    final prefs = SharedPreferencesUtil();
    var fetches = 0;
    Future? latest;
    late MessageProvider provider;
    provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
            {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) async {
          fetches++;
          return _result(true, 'new', _newFailure);
        });
    addTearDown(provider.dispose);
    var restarted = false;
    provider.addListener(() {
      if (provider.isLoadingMessages && !restarted) {
        restarted = true;
        latest = provider.refreshMessages();
      }
    });
    await provider.refreshMessages();
    await latest;
    expect(fetches, 1);
    expect(provider.messages.single.id, 'new');
    expect(provider.isLoadingMessages, isFalse);
  });
  test('refresh started by history publication keeps loading ownership after notification', () async {
    final prefs = SharedPreferencesUtil();
    final firstResult = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final secondResult = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var calls = 0;
    final provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
            calls++ == 0 ? firstResult.future : secondResult.future);
    addTearDown(provider.dispose);
    var restarted = false;
    Future? latest;
    provider.addListener(() {
      if (!restarted && provider.messages.any((message) => message.id == 'published')) {
        restarted = true;
        latest = provider.refreshMessages();
      }
    });
    final first = provider.refreshMessages();
    firstResult.complete(_result(true, 'published', _oldFailure));
    await first;
    expect(restarted, isTrue);
    expect(provider.isLoadingMessages, isTrue);
    secondResult.complete(_result(false, 'unused', _newFailure));
    await latest;
    expect(provider.messages.single.id, 'published');
    expect(provider.lastHistoryFailure, same(_newFailure));
    expect(provider.isLoadingMessages, isFalse);
  });
  test('successful refresh preserves an active-send timeline and draft boundary', () async {
    final prefs = SharedPreferencesUtil();
    final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    final local = _message('pending', sender: MessageSender.human)
      ..clientDeliveryState = ClientMessageDeliveryState.pending;
    final provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
            pending.future);
    addTearDown(provider.dispose);
    provider.messages = [local];
    prefs.cachedMessages = [local];
    provider.setSendingMessage(true);
    final epoch = provider.localDraftResetGeneration;
    final read = provider.refreshMessages();
    pending.complete(_result(true, 'server', _newFailure));
    await read;
    expect(provider.messages.single, same(local));
    expect(local.clientDeliveryState, ClientMessageDeliveryState.pending);
    expect(provider.sendingMessage, isTrue);
    expect(provider.localDraftResetGeneration, epoch);
  });
  test('current failure retains cached and locally unsent messages', () async {
    final prefs = SharedPreferencesUtil();
    final unsent = _message('unsent', sender: MessageSender.human)
      ..clientDeliveryState = ClientMessageDeliveryState.failed;
    prefs.cachedMessages = [_message('cache'), unsent];
    final provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) async =>
            const EllaServiceResult.failure(_newFailure));
    addTearDown(provider.dispose);
    await provider.refreshMessages();
    expect(provider.messages.map((m) => m.id), ['cache', 'unsent']);
    expect(prefs.cachedMessages.map((m) => m.id), ['cache', 'unsent']);
    expect(provider.lastHistoryFailure, same(_newFailure));
    expect(provider.isLoadingMessages, isFalse);
  });
  test('diagnostics contain only closed metadata, bounded status and request order', () async {
    final prefs = SharedPreferencesUtil();
    prefs.uid = 'private-owner-sentinel';
    prefs.cachedMessages = [_message('private-id-sentinel', text: 'private-text-sentinel')];
    const failure =
        ClientApiFailure(ClientApiFailureKind.invalidResponse, statusCode: 999, backendCode: 'private-token-sentinel');
    final before = Logger.instance.talker.history.length;
    final provider = MessageProvider(
        activeAuthority: () => _Authority(prefs.uid, () => true),
        ellaChatHistoryFetcher: (
                {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) async =>
            const EllaServiceResult.failure(failure));
    addTearDown(provider.dispose);
    await provider.refreshMessages();
    final lines = Logger.instance.talker.history
        .skip(before)
        .map((e) => e.message ?? '')
        .where((e) => e.startsWith('[EllaChatHistory]'))
        .toList();
    expect(lines, hasLength(3));
    expect(lines.last, contains('event=failure failure_kind=invalidResponse status=unknown retryable=false'));
    for (final line in lines) {
      expect(line, contains('binding_generation=0 request=1'));
      expect(line, contains('utc='));
      for (final private in [
        'private-owner-sentinel',
        'private-id-sentinel',
        'private-text-sentinel',
        'private-token-sentinel'
      ]) {
        expect(line, isNot(contains(private)));
      }
    }
  });
  var schemaCase = 0;
  for (final body in [
    {'messages': <dynamic>[]},
    {'messages': <dynamic>[], 'hasMore': 'false'},
    {
      'messages': [
        {'sender': 'ai', 'text': 'synthetic', 'created_at': 'invalid-date'}
      ],
      'hasMore': false
    }
  ]) {
    final caseIndex = schemaCase++;
    test('strict malformed-success validation is preserved case=$caseIndex', () async {
      final prefs = SharedPreferencesUtil();
      final result = await fetchEllaChatHistoryPage(
          expectedAuthenticatedUid: prefs.uid,
          exactAuthority: _Authority(prefs.uid, () => true),
          transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async =>
              http.Response(jsonEncode(body), 200));
      expect(result.failure!.kind, ClientApiFailureKind.invalidResponse);
      expect(result.value, isNull);
      expect(result.failure!.retryable, isFalse);
    });
  }
}

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}
