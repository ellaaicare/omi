import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/ella/widgets/ella_companion_avatar.dart';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/services.dart';

ServerMessage _message(String id, String text, MessageSender sender) =>
    ServerMessage(id, DateTime(2026, 10, 9, 9, 41), text, sender, MessageType.text, null, false, [], [], [],
        askForNps: false);

class _Messages extends MessageProvider {
  _Messages({this.streamFailure = true});
  final bool streamFailure;
  int historyRefreshes = 0;
  @override
  ClientApiFailure? get lastHistoryFailure => const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
  @override
  ClientApiFailure? get lastStreamFailure =>
      streamFailure ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true) : null;
  @override
  Future refreshMessages({bool dropdownSelected = false}) async {
    historyRefreshes++;
  }

  @override
  Future<void> fetchChatApps() async {}
}

void main() {
  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeSolid')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'keyboard-fixture-owner';
  });

  tearDown(EllaCaptureHost.resetForTesting);
  for (final light in [false, true]) {
    for (final viewport in [const Size(393, 852), const Size(440, 956), const Size(390, 844)]) {
      for (final captureOn in [false, true]) {
        for (final scale in [1.0, 1.35, 2.0, 3.5]) {
          for (final keyboard in [0.0, 390.0]) {
            testWidgets('capture=$captureOn errors light=$light viewport=$viewport scale=$scale inset=$keyboard',
                (tester) async {
              tester.view.physicalSize = viewport;
              tester.view.devicePixelRatio = 1;
              addTearDown(tester.view.resetPhysicalSize);
              addTearDown(tester.view.resetDevicePixelRatio);
              addTearDown(tester.view.resetViewInsets);
              tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
              tester.view.viewPadding = const FakeViewPadding(top: 59, bottom: 34);
              addTearDown(tester.view.resetPadding);
              addTearDown(tester.view.resetViewPadding);
              final home = HomeProvider()..setIndex(1);
              final messages = _Messages(streamFailure: captureOn)
                ..isLoadingMessages = captureOn
                ..hasCachedMessages = true
                ..messages = List.generate(
                    25,
                    (i) => _message('message-$i', i == 24 ? 'Latest local fixture' : 'Older local fixture $i',
                        MessageSender.human));
              final apps = AppProvider();
              final connection = ConnectivityProvider();
              final recorder = VoiceRecorderProvider();
              final conversations = ConversationProvider();
              final integrations = IntegrationProvider();
              // The only visible control is the bounded header gear; capture state
              // and failure notices retain independent geometry below the safe-area fade.
              EllaCaptureHost.installForTesting(
                  homeCaptureDockBuilder: (_) => IconButton(
                      tooltip: 'Labelled capture state fixture',
                      onPressed: () {},
                      icon: const Icon(Icons.settings_outlined)));
              EllaCaptureHost.installCompanionRecordingReader(() => captureOn);
              await tester.pumpWidget(MultiProvider(
                  providers: [
                    ChangeNotifierProvider<HomeProvider>.value(value: home),
                    ChangeNotifierProvider<MessageProvider>.value(value: messages),
                    ChangeNotifierProvider<AppProvider>.value(value: apps),
                    ChangeNotifierProvider<ConnectivityProvider>.value(value: connection),
                    ChangeNotifierProvider<VoiceRecorderProvider>.value(value: recorder),
                    ChangeNotifierProvider<ConversationProvider>.value(value: conversations),
                    ChangeNotifierProvider<IntegrationProvider>.value(value: integrations),
                  ],
                  child: MaterialApp(
                      theme: light ? ellaThemeData() : ellaDarkThemeData(),
                      localizationsDelegates: AppLocalizations.localizationsDelegates,
                      supportedLocales: AppLocalizations.supportedLocales,
                      builder: (context, child) => MediaQuery(
                          data: MediaQuery.of(context)
                              .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                          child: child!),
                      home: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                        const SizedBox.shrink(),
                        ChatPage(isPivotBottom: true, draftAuthenticatedUid: () => 'keyboard-fixture-owner'),
                        const SizedBox.shrink(),
                        const SizedBox.shrink()
                      ]))));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 400));
              expect(find.byType(EllaCompanionAvatar), findsOneWidget);
              final avatarPaint = tester.widget<CustomPaint>(
                  find.descendant(of: find.byType(EllaCompanionAvatar), matching: find.byType(CustomPaint)));
              expect((avatarPaint.painter! as LumePainter).state, captureOn ? LumeState.listening : LumeState.paused,
                  reason: 'fixture represents the selected capture state while exercising error and keyboard geometry');
              if (scale == 1 && keyboard == 0) {
                final avatarRect = tester.getRect(find.byType(EllaCompanionAvatar));
                final barRect = tester.getRect(find.byKey(const Key('iris-chat-floating-header')));
                expect(avatarRect.height, 48);
                expect(avatarRect.top, greaterThanOrEqualTo(barRect.top));
                expect(avatarRect.bottom, lessThanOrEqualTo(barRect.bottom));
                final waveform = find.byKey(const Key('iris-composer-talk'));
                expect(waveform.hitTestable(), findsOneWidget);
                expect(find.byIcon(Icons.mic_rounded), findsOneWidget);
                expect(find.byKey(const Key('chat-composer-add')), findsNothing);
                expect(
                    find.byWidgetPredicate((w) => w is FaIcon && w.icon == FontAwesomeIcons.microphone), findsNothing);
                expect(tester.getSize(waveform), const Size(56, 56));
                await tester.tap(waveform);
                await tester.pump();
                expect(home.selectedIndex, 2);
                expect(recorder.isActive, false);
                home.setIndex(1);
                await tester.pump();
              }
              // 927 physical acceptance found the idle action clipped. Previous checks
              // scrolled/focused before Retry and did not cover this state.
              primaryFocus?.unfocus();
              await tester.pump(const Duration(milliseconds: 400));
              expect(home.isChatFieldFocused, isFalse);
              final idleRetry = find.byKey(const Key('ella-chat-retry-history'));
              final idleStatusRect = tester.getRect(find.byKey(const Key('chat-status-viewport')));
              final idleRetryRect = tester.getRect(idleRetry);
              expect(tester.widget<TextField>(find.byType(TextField)).focusNode!.hasFocus, isFalse);
              expect(idleRetryRect.top, greaterThanOrEqualTo(idleStatusRect.top));
              expect(idleRetryRect.bottom, lessThanOrEqualTo(idleStatusRect.bottom),
                  reason: 'entire idle Retry target visible with navigation present, without scrolling or focus');
              expect(idleRetry.hitTestable(), findsOneWidget);
              final idleRefreshes = messages.historyRefreshes;
              await tester.tapAt(idleRetryRect.bottomRight - const Offset(2, 2));
              await tester.pump();
              expect(messages.historyRefreshes, idleRefreshes + 1,
                  reason: 'idle Retry receives a tap at its lower corner without focus or scroll');
              expect(messages.messages.length, 25);
              expect(tester.widget<TextField>(find.byType(TextField)).controller!.text, isEmpty);
              // Regression: at the actual standard enlarged text scales the action
              // must remain a separate sibling beside the field, never below/inside it.
              final surface = find.byKey(const Key('iris-composer-field-surface'));
              final talk = find.byKey(const Key('iris-composer-talk'));
              expect(find.descendant(of: surface, matching: talk), findsNothing);
              expect(tester.getSize(talk), const Size(56, 56));
              expect(tester.getRect(talk).bottom, lessThanOrEqualTo(viewport.height - 34));
              final talkSurface = tester.widget<DecoratedBox>(find.byKey(const Key('iris-composer-talk-surface')));
              final decoration = talkSurface.decoration as BoxDecoration;
              expect(decoration.shape, BoxShape.circle);
              expect(decoration.color, EllaPalette.of(tester.element(talk)).userBubbleFill);
              expect(tester.getRect(talk).left, greaterThanOrEqualTo(tester.getRect(surface).right + 7));
              expect(tester.getRect(surface).height, lessThanOrEqualTo(56 + scale * 24));
              expect(talk.hitTestable(), findsOneWidget,
                  reason: 'history error must not require scrolling to discover microphone');
              final field = find.byType(TextField);
              final send = find.byWidgetPredicate((w) => w is FaIcon && w.icon == FontAwesomeIcons.arrowUp);
              final history = find.byType(ListView).first;
              expect(tester.takeException(), isNull, reason: 'scaled syncing header fits before keyboard focus');
              expect(find.text('Syncing messages with server...'), captureOn ? findsOneWidget : findsNothing);
              expect(tester.getSize(history).height, greaterThan(30));

              for (var reopen = 0; reopen < 2; reopen++) {
                await tester.tap(field);
                tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
                tester.view.padding = const FakeViewPadding(top: 59);
                await tester.pump(const Duration(milliseconds: 400));
                for (final draft in [
                  'Qqqqqqqqqqqqqqqqqqqqqqqqqz',
                  'One line',
                  'First\nSecond\nThird',
                  List.filled(14, 'Full multiline draft').join('\n'),
                  'https://example.invalid/${List.filled(30, 'long-link').join()}',
                  'One line again'
                ]) {
                  expect(field.hitTestable(), findsOneWidget, reason: 'composer visible before scrolling footer');
                  await tester.ensureVisible(field);
                  await tester.pump();
                  await tester.showKeyboard(field);
                  tester
                      .state<EditableTextState>(find.descendant(of: field, matching: find.byType(EditableText)))
                      .updateEditingValue(
                          TextEditingValue(text: draft, selection: TextSelection.collapsed(offset: draft.length)));
                  await tester.pump();
                  await tester.pump(const Duration(milliseconds: 150));
                  await tester.pump(const Duration(milliseconds: 350));
                  expect(tester.takeException(), isNull);
                  expect(tester.getSize(history).height, greaterThan(30));
                  debugPrint(
                      'KEYBOARD-DIAGNOSTIC scale=$scale inset=$keyboard draftLength=${draft.length} field=${tester.getRect(field)} send=${tester.getRect(send)} history=${tester.getRect(history)} focus=${tester.widget<TextField>(field).focusNode?.hasFocus}');
                  expect(field.hitTestable(), findsOneWidget);
                  final sendRect = tester.getRect(find.byKey(const Key('chat-composer-send')));
                  expect(sendRect.top, greaterThanOrEqualTo(0));
                  expect(sendRect.bottom, lessThanOrEqualTo(viewport.height - keyboard + 1),
                      reason: 'Send clears keyboard without scrolling status');
                  for (final key in ['chat-composer-send']) {
                    final target = find.byKey(Key(key));
                    expect(target, findsOneWidget);
                    // On the smallest viewport the existing scrollable composer exposes controls.
                    await tester.ensureVisible(target);
                    await tester.pump();
                    final rect = tester.getRect(target);
                    expect(rect.width, greaterThanOrEqualTo(44));
                    expect(rect.height, greaterThanOrEqualTo(44));
                    final listener =
                        tester.renderObject(find.descendant(of: target, matching: find.byType(Listener)).first);
                    for (final point in [
                      rect.topLeft + const Offset(1, 1),
                      rect.topRight + const Offset(-1, 1),
                      rect.bottomLeft + const Offset(1, -1),
                      rect.bottomRight + const Offset(-1, -1)
                    ]) {
                      final hit = tester.hitTestOnBinding(point);
                      expect(hit.path.any((entry) => identical(entry.target, listener)), isTrue,
                          reason:
                              'all four corners receive hits: key=$key point=$point rect=$rect scale=$scale inset=$keyboard draft=$draft');
                    }
                    final gesture = tester.widget<GestureDetector>(target);
                    expect(gesture.behavior, HitTestBehavior.opaque,
                        reason: 'the full rectangular target includes corners outside the painted circle');
                  }
                  expect(send.hitTestable(), findsOneWidget,
                      reason: 'Send remains reachable through the existing footer scroll');
                  final restoredFocus = tester.widget<TextField>(field).focusNode!;
                  restoredFocus.unfocus();
                  await tester.pump();
                  restoredFocus.requestFocus();
                  await tester.pump();
                  await tester.pump(const Duration(milliseconds: 350));
                  final editable = tester
                      .state<EditableTextState>(find.descendant(of: field, matching: find.byType(EditableText)))
                      .renderEditable;
                  final caret = editable.getLocalRectForCaret(TextPosition(offset: draft.length));
                  expect(caret.top, greaterThanOrEqualTo(-1));
                  expect(caret.bottom, lessThanOrEqualTo(editable.size.height + 1),
                      reason: 'full caret stays in editor viewport');
                  final globalCaret = caret.shift(editable.localToGlobal(Offset.zero));
                  final clip =
                      tester.getRect(find.ancestor(of: field, matching: find.byType(SingleChildScrollView)).first);
                  expect(globalCaret.top, greaterThanOrEqualTo(clip.top - 1),
                      reason: 'restored caret clears footer top clip');
                  expect(globalCaret.bottom, lessThanOrEqualTo(clip.bottom + 1),
                      reason: 'restored caret clears footer bottom clip');
                  expect(globalCaret.bottom, lessThanOrEqualTo(viewport.height - keyboard + 1),
                      reason: 'restored caret clears keyboard');
                  expect(tester.widget<TextField>(field).controller!.text, draft,
                      reason: 'exposing actions and restoring editor never changes the draft');
                  expect(tester.widget<TextField>(field).controller!.selection.extentOffset, draft.length);
                }
                final chat = tester.state<ChatPageState>(find.byType(ChatPage));
                await tester.scrollUntilVisible(find.byKey(const ValueKey('message-0')), 200,
                    scrollable: find.descendant(of: history, matching: find.byType(Scrollable)).first, maxScrolls: 100);
                expect(find.byKey(const ValueKey('message-0')), findsOneWidget);
                expect(
                    tester.getRect(history).overlaps(tester.getRect(find.byKey(const ValueKey('message-0')))), isTrue);
                chat.scrollController.jumpTo(0);
                await tester.pump();
                await tester.scrollUntilVisible(find.byKey(const ValueKey('message-24')), 100,
                    scrollable: find.descendant(of: history, matching: find.byType(Scrollable)).first, maxScrolls: 100);
                expect(
                    tester.getRect(history).overlaps(tester.getRect(find.byKey(const ValueKey('message-24')))), isTrue);
                messages.messages
                    .add(_message('local-response', '[Ella] Synthetic local streaming response', MessageSender.ai));
                messages.setSendingMessage(true);
                messages.setShowTypingIndicator(true);
                await tester.pump();
                await tester.pump(const Duration(milliseconds: 200));
                expect(tester.takeException(), isNull);
                messages.setSendingMessage(false);
                messages.setShowTypingIndicator(false);
                await tester.pump();
                await tester.ensureVisible(find.byKey(const Key('chat-composer-send')));
                await tester.pump();
                expect(send.hitTestable(), findsOneWidget,
                    reason: 'Send remains reachable after history/streaming changes');
                messages.messages.removeLast();
                messages.setShowTypingIndicator(false);
                await tester.drag(history, const Offset(0, 900));
                await tester.pump();
                await tester.drag(history, const Offset(0, -900));
                await tester.pump();
                expect(tester.takeException(), isNull);
                final draft = tester.widget<TextField>(field).controller!.text;
                primaryFocus?.unfocus();
                tester.view.viewInsets = const FakeViewPadding();
                tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
                await tester.pump(const Duration(milliseconds: 400));
                home.setIndex(0);
                await tester.pump();
                home.setIndex(1);
                await tester.pump();
                for (final state in [
                  AppLifecycleState.inactive,
                  AppLifecycleState.hidden,
                  AppLifecycleState.paused,
                  AppLifecycleState.hidden,
                  AppLifecycleState.inactive,
                  AppLifecycleState.resumed
                ]) {
                  tester.binding.handleAppLifecycleStateChanged(state);
                }
                await tester.pump();
                expect(tester.widget<TextField>(field).controller!.text, draft);
                expect(messages.messages.length, 25, reason: 'local draft tests never send');
                expect(tester.takeException(), isNull);
              }
              final retry = tester.widget<TextButton>(find.byKey(const Key('ella-chat-retry-history')));
              expect(retry.style!.foregroundColor!.resolve({}),
                  EllaPalette.of(tester.element(find.byType(TextField))).textPrimary);
              expect(retry.style!.minimumSize!.resolve({}), const Size(44, 44));
              final palette = EllaPalette.of(tester.element(find.byType(TextField)));
              final ink = retry.style!.foregroundColor!.resolve({})!.computeLuminance();
              final paper = palette.bgSecondary.computeLuminance();
              expect(
                  ((ink > paper ? ink : paper) + .05) / ((ink < paper ? ink : paper) + .05), greaterThanOrEqualTo(4.5));
              final refreshes = messages.historyRefreshes;
              await tester.scrollUntilVisible(find.byKey(const Key('ella-chat-retry-history')), 60,
                  scrollable: find
                      .descendant(of: find.byKey(const Key('chat-status-viewport')), matching: find.byType(Scrollable))
                      .first,
                  maxScrolls: 100);
              expect(tester.getRect(find.byKey(const Key('ella-chat-retry-history'))).top,
                  greaterThanOrEqualTo(tester.getRect(find.byKey(const Key('iris-chat-floating-header'))).bottom),
                  reason: 'scrolling error details must never move Retry under the floating gear');
              await tester.tap(find.byKey(const Key('ella-chat-retry-history')));
              await tester.pump();
              expect(messages.historyRefreshes, refreshes + 1);
              expect(messages.messages.length, 25, reason: 'retrying the notice preserves the cached timeline');
              expect(tester.takeException(), isNull);
              await tester.pumpWidget(const SizedBox.shrink());
              await tester.pump(const Duration(seconds: 1));
              for (final provider in [home, messages, apps, connection, recorder, conversations, integrations]) {
                provider.dispose();
              }
            });
          }
        }
      }
    }
  }
}
