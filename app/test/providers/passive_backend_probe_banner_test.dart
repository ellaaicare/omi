import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _InterfaceConnectivityProvider extends ConnectivityProvider {
  _InterfaceConnectivityProvider(this.interfaceAvailable);

  final bool interfaceAvailable;

  @override
  bool get isConnected => interfaceAvailable;
}

class _ChatMessageProvider extends MessageProvider {
  _ChatMessageProvider({
    required super.activeAuthority,
    required super.aiConsentEnsurer,
    required super.ellaChatStreamSender,
    super.ellaChatHistoryFetcher,
    EllaChatTurnLookup? ellaChatTurnLookup,
  }) : super(
          chatAppsRetriever: () async => [],
          ellaChatTurnLookup: ellaChatTurnLookup ??
              ({
                required clientMessageId,
                required expectedAuthenticatedUid,
                required exactAuthority,
              }) async =>
                  const EllaServiceResult.success(false),
        );

  bool allowHistoryRefresh = false;

  @override
  Future<void> refreshMessages({bool dropdownSelected = false}) async {
    if (allowHistoryRefresh) await super.refreshMessages(dropdownSelected: dropdownSelected);
  }

  Future<void> refreshSyntheticHistory() async {
    allowHistoryRefresh = true;
    await super.refreshMessages();
  }

  @override
  Future<void> fetchChatApps() async {}
}

class _CurrentAuthority implements AccountCommitAuthority {
  @override
  String get uid => 'test-banner-owner';

  @override
  bool isCurrent() => true;

  @override
  bool isExactCurrent() => true;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  PlatformManager.initializeForTesting();

  final service = ConnectivityService();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    service.applyHealthProbeForTest(statusCode: 200);
  });

  tearDown(() {
    service.applyHealthProbeForTest(statusCode: 200);
  });

  Future<AppLocalizations> mountChat(WidgetTester tester, MessageProvider messages,
      {bool interfaceAvailable = true}) async {
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<MessageProvider>.value(value: messages),
        ChangeNotifierProvider<ConnectivityProvider>(
          create: (_) => _InterfaceConnectivityProvider(interfaceAvailable),
        ),
        ChangeNotifierProvider(create: (_) => AppProvider()),
        ChangeNotifierProvider(create: (_) => HomeProvider()),
        ChangeNotifierProvider(create: (_) => IntegrationProvider()),
        ChangeNotifierProvider(create: (_) => VoiceRecorderProvider()),
      ],
      child: MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const ChatPage(),
      ),
    ));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pump(const Duration(milliseconds: 350));
    return AppLocalizations.of(tester.element(find.byType(ChatPage)));
  }

  _ChatMessageProvider provider(
      {bool authorityAvailable = true, bool consent = true, bool failSend = false, VoidCallback? onSend}) {
    final messages = _ChatMessageProvider(
      activeAuthority: () => authorityAvailable ? _CurrentAuthority() : null,
      aiConsentEnsurer: () async => consent,
      ellaChatStreamSender: (text,
          {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) async* {
        onSend?.call();
        if (failSend) throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
        yield ServerMessageChunk(
          'test-assistant',
          '',
          MessageChunkType.done,
          message: ServerMessage('test-assistant', DateTime(2026, 1, 1), 'Reply received', MessageSender.ai,
              MessageType.text, null, false, [], [], []),
        );
      },
    );
    addTearDown(messages.dispose);
    return messages;
  }

  for (final historyAfterSend in [false, true]) {
    for (final historyFails in [true, false]) {
      testWidgets(
          'late history ${historyFails ? "failure" : "success"} preserves a send (history after terminal: $historyAfterSend)',
          (tester) async {
        final history = Completer<EllaServiceResult<EllaChatHistoryPage>>();
        final reply = StreamController<ServerMessageChunk>();
        final messages = _ChatMessageProvider(
          activeAuthority: () => _CurrentAuthority(),
          aiConsentEnsurer: () async => true,
          ellaChatHistoryFetcher: (
                  {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
              history.future,
          ellaChatStreamSender: (text,
                  {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) =>
              reply.stream,
        );
        addTearDown(messages.dispose);
        final l10n = await mountChat(tester, messages);
        final refresh = messages.refreshSyntheticHistory();
        final send = messages.sendMessageStreamToServer('Synthetic pending turn');
        await tester.pump();
        await tester.pump(const Duration(seconds: 29));
        expect(messages.showTypingIndicator, isTrue);
        expect(messages.messages.length, 2);
        if (historyAfterSend) {
          reply.add(ServerMessageChunk('synthetic-reply', '', MessageChunkType.done,
              message: ServerMessage('synthetic-reply', DateTime(2026, 1, 1), 'Synthetic reply received',
                  MessageSender.ai, MessageType.text, null, false, [], [], [])));
          await reply.close();
          await send;
        }
        history.complete(historyFails
            ? const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true))
            : const EllaServiceResult.success(EllaChatHistoryPage(messages: [], hasMore: false)));
        await refresh;
        await tester.pump();
        expect(find.text(l10n.ellaChatCouldntSend), findsNothing,
            reason: 'History loading must not claim that the still-pending outgoing turn failed');
        expect(messages.messages.length, 2, reason: 'History must retain the active assistant placeholder');
        expect(messages.showTypingIndicator, !historyAfterSend);
        if (!historyAfterSend) {
          reply.add(ServerMessageChunk('synthetic-reply', '', MessageChunkType.done,
              message: ServerMessage('synthetic-reply', DateTime(2026, 1, 1), 'Synthetic reply received',
                  MessageSender.ai, MessageType.text, null, false, [], [], [])));
          await reply.close();
          await send;
        }
        await tester.pump();
        expect(messages.showTypingIndicator, isFalse);
        expect(messages.sendingMessage, isFalse);
        expect(messages.messages.where((message) => message.sender == MessageSender.ai).single.text,
            'Synthetic reply received');
        expect(messages.messages.where((message) => message.sender == MessageSender.human).single.clientDeliveryState,
            isNull);
        expect(find.text(l10n.ellaChatCouldntSend), findsNothing);
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }

  testWidgets('exact accepted-turn lookup is not erased by an older history response', (tester) async {
    final history = Completer<EllaServiceResult<EllaChatHistoryPage>>();
    var sends = 0;
    final messages = _ChatMessageProvider(
      activeAuthority: () => _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatHistoryFetcher: (
              {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) =>
          history.future,
      ellaChatTurnLookup: (
              {required clientMessageId, required expectedAuthenticatedUid, required exactAuthority}) async =>
          const EllaServiceResult.success(true),
      ellaChatStreamSender: (text,
          {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) async* {
        sends++;
        throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
      },
    );
    addTearDown(messages.dispose);
    await mountChat(tester, messages);
    await messages.sendMessageStreamToServer('Synthetic lost acknowledgment');
    final originalId = messages.messages.single.id;
    final refresh = messages.refreshSyntheticHistory();
    await messages.retryLastFailedMessage();
    expect(messages.messages.single.clientDeliveryState, isNull);
    history.complete(const EllaServiceResult.success(EllaChatHistoryPage(messages: [], hasMore: false)));
    await refresh;
    expect(sends, 1, reason: 'An accepted user event must not create a second provider invocation');
    expect(messages.messages.single.id, originalId);
    expect(messages.messages.single.clientDeliveryState, isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('terminal error clears pending UI even when the source stream stays open', (tester) async {
    final reply = StreamController<ServerMessageChunk>();
    final messages = _ChatMessageProvider(
      activeAuthority: () => _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (text,
              {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) =>
          reply.stream,
    );
    addTearDown(messages.dispose);
    addTearDown(reply.close);
    final l10n = await mountChat(tester, messages);
    final send = messages.sendMessageStreamToServer('Synthetic terminal error');
    await tester.pump();
    expect(messages.showTypingIndicator, isTrue);
    expect(reply.hasListener, isTrue);
    reply.add(ServerMessageChunk('synthetic-error', '', MessageChunkType.error));
    await tester.pump(const Duration(milliseconds: 1));
    await tester.pump();
    expect(reply.hasListener, isFalse, reason: 'Collector cancels terminal failure');
    await tester.runAsync(() => send.timeout(const Duration(seconds: 1)));
    await tester.pump();
    expect(messages.showTypingIndicator, isFalse, reason: 'A known terminal error must not wait for stream EOF');
    expect(messages.sendingMessage, isFalse);
    expect(messages.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
    expect(find.text(l10n.ellaChatCouldntSend), findsOneWidget);
    await send;
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('empty history failure offers a history-only retry', (tester) async {
    var fetches = 0;
    var sends = 0;
    final messages = _ChatMessageProvider(
      activeAuthority: () => _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatHistoryFetcher: (
          {required limit, required before, required expectedAuthenticatedUid, required exactAuthority}) async {
        fetches++;
        return fetches == 1
            ? const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true))
            : const EllaServiceResult.success(EllaChatHistoryPage(messages: [], hasMore: false));
      },
      ellaChatStreamSender: (text,
          {required clientMessageId, required clientSentAt, expectedAuthenticatedUid, exactAuthority}) async* {
        sends++;
      },
    );
    addTearDown(messages.dispose);
    final l10n = await mountChat(tester, messages);
    await messages.refreshSyntheticHistory();
    await tester.pump();
    expect(find.text(l10n.ellaChatCouldntSend), findsNothing);
    expect(find.text(l10n.ellaChatHistoryUnavailableTitle), findsOneWidget);
    await tester.tap(find.byKey(const Key('ella-chat-retry-history')));
    await tester.pump();
    expect(fetches, 2);
    expect(sends, 0);
    expect(messages.lastHistoryFailure, isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  for (final failure in ['timeout', '503', 'socket', 'stale-no-interface']) {
    testWidgets('$failure passive probe does not contradict a successful Chat send', (tester) async {
      switch (failure) {
        case 'timeout':
          service.applyHealthProbeForTest(error: TimeoutException('synthetic'));
        case '503':
          service.applyHealthProbeForTest(statusCode: 503);
        case 'socket':
          service.applyHealthProbeForTest(error: StateError('synthetic'));
        case 'stale-no-interface':
          service.applyMissingInterfaceProbeForTest();
      }
      final messages = provider();
      await messages.sendMessageStreamToServer('Test message');
      final l10n = await mountChat(tester, messages);
      expect(find.text('Reply received'), findsOneWidget);
      expect(messages.messages.first.clientDeliveryState, isNull);
      expect(find.byKey(const Key('passive-backend-probe-banner')), findsNothing);
      expect(find.text(l10n.ellaServerUnreachableBanner), findsNothing);
      expect(find.text(l10n.ellaChatCouldntSend), findsNothing);
      expect(find.text(l10n.retry), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }

  testWidgets('no interface shows truthful localized copy without automatic delivery promise', (tester) async {
    final l10n = await mountChat(tester, provider(), interfaceAvailable: false);
    final banner = find.byKey(const Key('passive-backend-probe-banner'));
    expect(banner, findsOneWidget);
    expect(find.descendant(of: banner, matching: find.text(l10n.noInternetConnection)), findsOneWidget);
    expect(find.text(l10n.pleaseCheckInternetConnectionAndTryAgain), findsOneWidget);
    expect(find.text(l10n.ellaServerUnreachableBanner), findsNothing);
    expect(find.textContaining('Messages will send'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('real send failure keeps the original Chat Retry with a failed passive probe', (tester) async {
    service.applyHealthProbeForTest(statusCode: 503);
    var sends = 0;
    final messages = provider(failSend: true, onSend: () => sends++);
    await messages.sendMessageStreamToServer('Test message');
    final originalTurn = messages.messages.single.canonicalTurnId;
    final l10n = await mountChat(tester, messages);
    expect(find.text(l10n.ellaChatCouldntSend), findsOneWidget);
    expect(find.text(l10n.retry), findsOneWidget);
    expect(find.byKey(const Key('passive-backend-probe-banner')), findsNothing);
    await tester.tap(find.text(l10n.retry));
    await tester.pump();
    expect(sends, 2);
    expect(messages.messages.single.canonicalTurnId, originalTurn);
    expect(messages.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  for (final denied in ['account', 'consent']) {
    testWidgets('$denied denial still prevents Chat transport when probe fails', (tester) async {
      service.applyHealthProbeForTest(error: TimeoutException('synthetic'));
      var sends = 0;
      final messages =
          provider(authorityAvailable: denied != 'account', consent: denied != 'consent', onSend: () => sends++);
      await messages.sendMessageStreamToServer('Test message');
      final l10n = await mountChat(tester, messages);
      expect(sends, 0);
      expect(messages.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
      expect(find.text(l10n.ellaChatCouldntSend), findsOneWidget);
      expect(find.byKey(const Key('passive-backend-probe-banner')), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }

  testWidgets('no interface hides internal probe tokens for every retained probe failure', (tester) async {
    final cases = <void Function()>[
      () => service.applyHealthProbeForTest(error: TimeoutException('probe')),
      () => service.applyHealthProbeForTest(statusCode: 503),
      () => service.applyHealthProbeForTest(error: StateError('socket failed')),
      service.applyMissingInterfaceProbeForTest,
    ];

    for (final arrange in cases) {
      arrange();
      final provider = _InterfaceConnectivityProvider(false);
      addTearDown(provider.dispose);
      await tester.pumpWidget(
        ChangeNotifierProvider<ConnectivityProvider>.value(
          value: provider,
          child: const MaterialApp(
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: Scaffold(body: PassiveBackendProbeBanner()),
          ),
        ),
      );

      final l10n = AppLocalizations.of(tester.element(find.byType(PassiveBackendProbeBanner)));
      expect(find.text(l10n.noInternetConnection), findsOneWidget);
      expect(find.text(l10n.pleaseCheckInternetConnectionAndTryAgain), findsOneWidget);
      expect(find.text(l10n.ellaServerUnreachableBanner), findsNothing);
      expect(find.byKey(const Key('passive-backend-probe-banner')), findsOneWidget);
      final rendered = tester
          .widgetList<Text>(
            find.descendant(of: find.byKey(const Key('passive-backend-probe-banner')), matching: find.byType(Text)),
          )
          .map((text) => text.data)
          .join(' ');
      for (final forbidden in [
        'timeout',
        'http_503',
        'http_',
        'no_network_interface',
        'TimeoutException',
        'StateError',
        'Backend health probe',
        'api.ella-ai-care.com',
        'Exception',
      ]) {
        expect(rendered.contains(forbidden), isFalse, reason: 'banner exposed $forbidden');
      }
    }

    service.applyHealthProbeForTest(statusCode: 200);
    final provider = ConnectivityProvider();
    addTearDown(provider.dispose);
    await tester.pumpWidget(
      ChangeNotifierProvider<ConnectivityProvider>.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: PassiveBackendProbeBanner()),
        ),
      ),
    );
    expect(find.byKey(const Key('passive-backend-probe-banner')), findsNothing);
  });
}
