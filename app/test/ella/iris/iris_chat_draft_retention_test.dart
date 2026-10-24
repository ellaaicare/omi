import 'dart:async';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import '../voice/foreground_dictation_host_test.dart' show Engine, Output;
import 'dart:ui' show SemanticsAction;
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/pages/chat/widgets/voice_recorder_widget.dart';
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
  int sends = 0;
  @override
  Future sendMessageStreamToServer(String text, {String? localMessageId}) async {
    sends++;
  }

  @override
  Future refreshMessages({bool dropdownSelected = false}) async {}
  @override
  Future<void> fetchChatApps() async {}
}

class _Recorder extends VoiceRecorderProvider {
  bool active = false;
  int starts = 0;
  Function(String)? captured;
  @override
  bool get isActive => active;
  @override
  bool get isRecording => active;
  @override
  VoiceRecorderState get state => active ? VoiceRecorderState.transcribeSuccess : VoiceRecorderState.idle;
  @override
  String get transcript => 'Synthetic late transcript';
  @override
  void setCallbacks({Function(String)? onTranscriptReady, VoidCallback? onClose}) {
    captured = onTranscriptReady;
  }

  @override
  Future<void> startRecording() async {
    starts++;
    setActive(true);
  }

  @override
  void close() {
    setActive(false);
  }

  void setActive(bool value) {
    active = value;
    notifyListeners();
  }
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
  });

  tearDown(() {
    EllaCaptureHost.resetForTesting();
    ForegroundDictationRegistration.current = null;
  });
  testWidgets('Real composer opens accessible local dictation controls at 3.5x; absent registration has no capture',
      (tester) async {
    final fixture = _Fixture()..scale = 3.5;
    await fixture.mount(tester);
    final chat = fixture.chat(tester);
    final original =
        const TextEditingValue(text: 'Unsent protected draft', selection: TextSelection.collapsed(offset: 5));
    chat.textController.value = original;
    await tester.pump();
    chat.textFieldFocusNode.unfocus();
    await tester.pump();
    final semantics = tester.ensureSemantics();
    await tester.tap(find.byKey(const Key('chat-composer-dictation')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-dictation-sheet')), findsOneWidget);
    for (final key in ['chat-dictation-start', 'chat-dictation-finish', 'chat-dictation-cancel']) {
      final button = find.byKey(Key(key));
      expect(tester.getSize(button).height, greaterThanOrEqualTo(48));
    }
    final start = tester.getSemantics(find.byKey(const Key('chat-dictation-start')));
    expect(start.getSemanticsData().hasAction(SemanticsAction.tap), false);
    final cancel = tester.getSemantics(find.byKey(const Key('chat-dictation-cancel')));
    expect(cancel.getSemanticsData().hasAction(SemanticsAction.tap), true);
    expect(find.text('Dictation is unavailable right now. You can type your message.'), findsOneWidget);
    expect(chat.textController.value, original);
    expect(fixture.recorder.starts, 0);
    expect(tester.takeException(), isNull);
    tester.binding.pipelineOwner.semanticsOwner!.performAction(cancel.id, SemanticsAction.tap);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-dictation-sheet')), findsNothing);
    expect(chat.textController.value, original);
    semantics.dispose();
    await tester.pumpWidget(const SizedBox.shrink());
  });

  for (final refusal in ['uidOnly', 'consentUnverified', 'provisioningUnverified', 'processorMismatch', 'contentful']) {
    testWidgets('Actual composer $refusal cannot admit permission or stop another capture', (tester) async {
      final capture = _FreshCapture();
      final fixture = _Fixture()
        ..useDictationIdentity = true
        ..capture = capture;
      await fixture.mount(tester);
      if (refusal != 'uidOnly') await _seedDictationAuthority(fixture.uid);
      final prefs = SharedPreferencesUtil();
      if (refusal == 'consentUnverified') SharedPreferencesUtil.clearAiConsentServerVerification();
      if (refusal == 'provisioningUnverified') prefs.invalidateEllaProvisioningServerVerification();
      if (refusal == 'processorMismatch') prefs.saveString('aiConsentProcessorSetHash', 'unaccepted-processor');
      if (refusal == 'contentful') capture.content = true;
      final engine = Engine();
      final output = Output();
      ForegroundDictationRegistration.current = ForegroundDictationRegistration(
          isPolicyEnabled: () => true, reserveOutput: () => output, createEngine: () => engine);
      final chat = fixture.chat(tester);
      chat.textFieldFocusNode.unfocus();
      await tester.pump();
      await tester.tap(find.byKey(const Key('chat-composer-dictation')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('chat-dictation-start')));
      await tester.pumpAndSettle();
      expect(engine.permissions, 0);
      expect(engine.initializations, 0);
      expect(engine.port.generation, 0);
      expect(fixture.recorder.starts, 0);
      expect(capture.stops, 0);
      expect(output.releases, 0);
      await tester.tap(find.byKey(const Key('chat-dictation-cancel')));
      await tester.pumpAndSettle();
      await tester.pumpWidget(const SizedBox.shrink());
      capture.dispose();
    });
  }

  for (final invalidate in ['none', 'selectionABA', 'account', 'tab', 'dispose', 'lifecycle']) {
    testWidgets('Actual composer held terminal $invalidate preserves exact authority and applies once', (tester) async {
      final fixture = _Fixture()
        ..useDictationIdentity = true
        ..capture = _FreshCapture();
      if (invalidate == 'none') fixture.scale = 3.5;
      await fixture.mount(tester);
      await _seedDictationAuthority(fixture.uid);
      final engine = Engine();
      final output = Output();
      ForegroundDictationRegistration.current = ForegroundDictationRegistration(
          isPolicyEnabled: () => true, reserveOutput: () => output, createEngine: () => engine);
      final chat = fixture.chat(tester);
      final original = const TextEditingValue(text: 'Keep this draft', selection: TextSelection.collapsed(offset: 4));
      chat.textController.value = original;
      chat.textFieldFocusNode.unfocus();
      await tester.pump();
      await tester.tap(find.byKey(const Key('chat-composer-dictation')));
      await tester.pumpAndSettle();
      if (invalidate == 'none') {
        final handle = tester.ensureSemantics();
        final startNode = tester.getSemantics(find.byKey(const Key('chat-dictation-start')));
        tester.binding.pipelineOwner.semanticsOwner!.performAction(startNode.id, SemanticsAction.tap);
        await tester.pumpAndSettle();
        handle.dispose();
      } else {
        await tester.tap(find.byKey(const Key('chat-dictation-start')));
        await tester.pumpAndSettle();
      }
      expect(engine.permissions, 1);
      expect(engine.initializations, 1);
      expect(find.byKey(const Key('chat-dictation-finish')), findsOneWidget);
      engine.port.terminalGate = Completer<void>();
      await tester.tap(find.byKey(const Key('chat-dictation-finish')));
      await tester.pump();
      expect(output.releases, 0);
      expect(chat.textController.value, original);
      if (invalidate == 'selectionABA') {
        chat.textController.selection = const TextSelection.collapsed(offset: 1);
        chat.textController.value = original;
      }
      if (invalidate == 'account') {
        fixture.uid = 'fictional-successor';
        SharedPreferencesUtil().uid = fixture.uid;
        fixture.messages.notifyListeners();
        await tester.pump();
      }
      if (invalidate == 'tab') {
        fixture.home.setIndex(0);
        await tester.pump();
      }
      if (invalidate == 'lifecycle') {
        for (final state in [AppLifecycleState.inactive, AppLifecycleState.hidden, AppLifecycleState.paused]) {
          tester.binding.handleAppLifecycleStateChanged(state);
        }
        await tester.pump();
      }
      if (invalidate == 'dispose') {
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      }
      engine.port.terminalGate!.complete();
      await tester.pumpAndSettle();
      expect(output.releases, 1);
      expect(engine.port.drains, 1);
      if (invalidate == 'none') {
        expect(chat.textController.text, 'Keep final words this draft');
      }
      if (invalidate == 'selectionABA' || invalidate == 'tab' || invalidate == 'lifecycle') {
        expect(chat.textController.value, original);
      }
      if (invalidate == 'account') {
        expect(chat.textController.text, isEmpty);
      }
      expect(fixture.recorder.starts, 0);
      expect(fixture.messages.sends, 0);
      expect(tester.takeException(), isNull);
      if (invalidate == 'lifecycle') {
        for (final state in [AppLifecycleState.hidden, AppLifecycleState.inactive, AppLifecycleState.resumed]) {
          tester.binding.handleAppLifecycleStateChanged(state);
        }
      }
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      fixture.capture!.dispose();
    });
  }

  testWidgets('Actual composer retains same cleanup after release refusal and retries Cancel before dismiss',
      (tester) async {
    final fixture = _Fixture()
      ..useDictationIdentity = true
      ..capture = _FreshCapture();
    await fixture.mount(tester);
    await _seedDictationAuthority(fixture.uid);
    final engine = Engine();
    final output = Output()..releaseError = StateError('fictional unknown release');
    ForegroundDictationRegistration.current = ForegroundDictationRegistration(
        isPolicyEnabled: () => true, reserveOutput: () => output, createEngine: () => engine);
    final chat = fixture.chat(tester);
    chat.textController.text = 'Protected draft';
    chat.textFieldFocusNode.unfocus();
    await tester.pump();
    await tester.tap(find.byKey(const Key('chat-composer-dictation')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('chat-dictation-start')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('chat-dictation-finish')));
    await tester.pumpAndSettle();
    expect(output.releases, 1);
    expect(output.current, true);
    await tester.tap(find.byKey(const Key('chat-dictation-cancel')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-dictation-sheet')), findsOneWidget);
    expect(output.releases, 2);
    expect(engine.permissions, 1);
    expect(engine.port.generation, 1);
    expect(engine.port.drains, 1);
    output.releaseError = null;
    await tester.tap(find.byKey(const Key('chat-dictation-cancel')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-dictation-sheet')), findsNothing);
    expect(output.releases, 3);
    expect(engine.port.drains, 1);
    expect(fixture.messages.sends, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    fixture.capture!.dispose();
  });

  testWidgets('Actual composer captured verified authority expiry stops own input and prevents late final insertion',
      (tester) async {
    final fixture = _Fixture()
      ..useDictationIdentity = true
      ..capture = _FreshCapture();
    await fixture.mount(tester);
    await _seedDictationAuthority(fixture.uid);
    final prefs = SharedPreferencesUtil();
    prefs.markAiConsentServerVerified(
        uid: fixture.uid,
        receiptId: prefs.aiConsentReceiptId,
        profileBindingId: prefs.aiConsentProfileBindingId,
        policyVersion: prefs.aiConsentContractVersion,
        processorSetHash: prefs.aiConsentProcessorSetHash,
        scopeVersion: prefs.aiConsentScopeVersion,
        scopeHash: prefs.aiConsentScopeHash,
        verifiedAt:
            DateTime.now().subtract(SharedPreferencesUtil.aiConsentServerVerificationTtl - const Duration(seconds: 1)));
    final engine = Engine();
    final output = Output();
    ForegroundDictationRegistration.current = ForegroundDictationRegistration(
        isPolicyEnabled: () => true, reserveOutput: () => output, createEngine: () => engine);
    final chat = fixture.chat(tester);
    const original = TextEditingValue(text: 'Unchanged draft', selection: TextSelection.collapsed(offset: 4));
    chat.textController.value = original;
    chat.textFieldFocusNode.unfocus();
    await tester.pump();
    await tester.tap(find.byKey(const Key('chat-composer-dictation')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('chat-dictation-start')));
    await tester.pumpAndSettle();
    expect(engine.permissions, 1);
    engine.port.terminalGate = Completer<void>();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(engine.port.drains, 1);
    expect(output.releases, 0);
    expect(chat.textController.value, original);
    engine.port.terminalGate!.complete();
    await tester.pumpAndSettle();
    expect(output.releases, 1);
    expect(chat.textController.value, original);
    expect(fixture.messages.sends, 0);
    await tester.tap(find.byKey(const Key('chat-dictation-cancel')));
    await tester.pumpAndSettle();
    await tester.pumpWidget(const SizedBox.shrink());
    fixture.capture!.dispose();
  });

  testWidgets('Exact draft survives responsive boundary, keyboard, navigation and lifecycle', (tester) async {
    final fixture = _Fixture();
    await fixture.mount(tester);
    final chat = fixture.chat(tester);
    var value = const TextEditingValue(
        text: 'First\nSecond\nLong unsent draft',
        selection: TextSelection(baseOffset: 3, extentOffset: 12),
        composing: TextRange(start: 0, end: 5));
    chat.textController.value = value;
    await tester.pump();
    for (final scale in [3.5, 1.0, 3.0, 1.0]) {
      fixture.scale = scale;
      await fixture.mount(tester);
      expect(identical(fixture.chat(tester), chat), isTrue);
      expect(chat.textController.value, value);
      tester.view.viewInsets = const FakeViewPadding(bottom: 320);
      await tester.pump(const Duration(milliseconds: 400));
      expect(chat.textController.value, value);
      tester.view.viewInsets = const FakeViewPadding();
      fixture.home.setIndex(0);
      await tester.pump();
      fixture.home.setIndex(1);
      await tester.pump();
      expect(identical(fixture.chat(tester), chat), isTrue);
      expect(chat.textController.text, value.text);
      expect(chat.textController.selection, value.selection);
      // Flutter ends IME composition when navigation removes editor focus.
      value = chat.textController.value;
      expect(tester.takeException(), isNull);
    }
    tester.view.physicalSize = const Size(844, 390);
    await tester.pump();
    tester.view.physicalSize = const Size(390, 844);
    await tester.pump();
    expect(identical(fixture.chat(tester), chat), isTrue);
    expect(chat.textController.value, value);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    expect(chat.textController.value, value);
    fixture.messages.notifyListeners();
    await tester.pump();
    expect(chat.textController.value, value, reason: 'history notifications do not retire the editor');
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });

  testWidgets('Account and same UID retirement clear synchronously and cannot resurrect', (tester) async {
    final fixture = _Fixture();
    await fixture.mount(tester);
    var chat = fixture.chat(tester);
    chat.textController.text = 'Account A draft';
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    expect(chat.textController.text, isEmpty);
    await tester.pump();
    chat.textController.text = 'New A draft';
    SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty,
        reason: 'same UID terminal generation is checked without barrier callback');
    chat.textController.text = 'A before logout';
    fixture.uid = '';
    SharedPreferencesUtil().uid = '';
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty);
    fixture.uid = 'owner-b';
    SharedPreferencesUtil().uid = 'owner-b';
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty);
    chat.textController.text = 'B draft';
    fixture.uid = 'owner-a';
    // Deliberate mismatched auth/prefs; no old text may survive.
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty);
    SharedPreferencesUtil().uid = 'owner-a';
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty, reason: 'returning A cannot resurrect discarded A/B text');
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    expect(tester.takeException(), isNull, reason: 'disposed draft lease is closed');
  });

  testWidgets('App/context/reset discard drafts; held B voice action cannot cross the boundary', (tester) async {
    final fixture = _Fixture();
    await fixture.mount(tester);
    final chat = fixture.chat(tester);
    chat.textController.text = 'Conversation A';
    fixture.apps.setSelectedChatAppId('assistant-b');
    await tester.pump();
    expect(chat.textController.text, isEmpty);
    fixture.apps.setSelectedChatAppId('');
    await tester.pump();
    expect(chat.textController.text, isEmpty);
    chat.textController.text = 'Context A';
    fixture.conversation = 'conversation-b';
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty);
    chat.textController.text = 'Context B';
    fixture.conversation = 'conversation-a';
    await fixture.mount(tester);
    expect(chat.textController.text, isEmpty);
    chat.textController.text = 'Context replacement draft';
    final oldVoice = tester.widget<IconButton>(find.byKey(const Key('iris-composer-talk'))).onPressed!;
    fixture.messages.reset();
    await tester.pump();
    expect(chat.textController.text, isEmpty);
    oldVoice();
    expect(fixture.home.selectedIndex, 1, reason: 'stale voice navigation must not cross reset');
    expect(fixture.recorder.starts, 0);
    expect(find.byType(VoiceRecorderWidget), findsNothing, reason: 'Composer B never starts editor dictation');
    await tester.tap(find.byKey(const Key('iris-composer-talk')));
    await tester.pump();
    expect(fixture.home.selectedIndex, 2, reason: 'fresh B action opens existing live voice');
    expect(fixture.recorder.starts, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });

  testWidgets('Fresh Chat cannot adopt a recording from another UI context', (tester) async {
    final fixture = _Fixture();
    fixture.recorder.active = true;
    await fixture.mount(tester);
    expect(find.byType(VoiceRecorderWidget), findsNothing);
    expect(fixture.recorder.captured, isNull);
    expect(fixture.chat(tester).textController.text, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });

  testWidgets('Stale mic closure rejects retirement and active-provider race', (tester) async {
    final fixture = _Fixture();
    await fixture.mount(tester);
    final microphone = find.byKey(const Key('iris-composer-talk'));
    var mic = tester.widget<IconButton>(microphone).onPressed!;
    fixture.uid = 'retired-owner';
    mic();
    expect(fixture.recorder.starts, 0);
    expect(fixture.home.selectedIndex, 1);
    fixture.uid = 'owner-a';
    fixture.messages.notifyListeners();
    await tester.pump();
    mic = tester.widget<IconButton>(microphone).onPressed!;
    fixture.recorder.active = true;
    mic();
    expect(fixture.recorder.starts, 0);
    expect(fixture.home.selectedIndex, 1);
    await tester.pump();
    expect(fixture.recorder.captured, isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });

  testWidgets('Current caret stays within editor and ancestor clips during sequential input and refocus',
      (tester) async {
    final fixture = _Fixture()..scale = 3.5;
    await fixture.mount(tester);
    tester.view.physicalSize = const Size(402, 874);
    tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
    tester.view.viewPadding = const FakeViewPadding(top: 59, bottom: 34);
    addTearDown(tester.view.resetPadding);
    addTearDown(tester.view.resetViewPadding);
    tester.view.viewInsets = const FakeViewPadding(bottom: 328);
    await tester.pump();
    final chat = fixture.chat(tester);
    chat.textFieldFocusNode.requestFocus();
    await tester.pump(const Duration(milliseconds: 400));
    final fieldFinder = find.descendant(of: find.byType(ChatPage), matching: find.byType(TextField));
    final editableFinder = find.descendant(of: fieldFinder, matching: find.byType(EditableText));
    final editable = tester.state<EditableTextState>(editableFinder);
    expect(identical(chat.textFieldFocusNode.context?.findAncestorStateOfType<EditableTextState>(), editable), isTrue);
    void assertVisible(TextEditingValue expected) {
      expect(chat.textController.value, expected);
      expect(editable.widget.controller.selection.extentOffset, expected.selection.extentOffset);
      final render = editable.renderEditable;
      final local = render.getLocalRectForCaret(expected.selection.extent);
      final global = local.shift(render.localToGlobal(Offset.zero));
      final ancestorScrollables = find.ancestor(of: editableFinder, matching: find.byType(Scrollable));
      expect(ancestorScrollables, findsAtLeastNWidgets(1), reason: 'footer viewport is included');
      final origin = render.localToGlobal(Offset.zero);
      final boxes = [
        Rect.fromLTWH(origin.dx, origin.dy, render.size.width, render.size.height),
        tester.getRect(fieldFinder),
        for (final element in ancestorScrollables.evaluate())
          tester.getRect(find.byElementPredicate((e) => identical(e, element)))
      ];
      for (final box in boxes) {
        expect(global.left, greaterThanOrEqualTo(box.left - 1), reason: 'caret inside horizontal viewport');
        expect(global.right, lessThanOrEqualTo(box.right + 1), reason: 'caret inside horizontal viewport');
        expect(global.top, greaterThanOrEqualTo(box.top - 1), reason: 'full caret above each viewport clip');
        expect(global.bottom, lessThanOrEqualTo(box.bottom + 1), reason: 'full caret below each viewport clip');
      }
      expect(global.bottom, lessThanOrEqualTo(874 - tester.view.viewInsets.bottom + 1));
      debugPrint('CARET-GLOBAL local=$local global=$global preferredLine=${render.preferredLineHeight} '
          'offset=${render.offset.pixels} extent=${(render.offset as ScrollPosition).maxScrollExtent} boxes=$boxes');
    }

    var text = '';
    for (final char in 'Qqqqqqqqqqqqqqqqqqqqqqqqqz'.split('')) {
      text += char;
      final value = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
      // Controller-driven/semantic delivery reproduces the actual missed reveal path.
      chat.textController.value = value;
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      assertVisible(value);
    }
    final middle = TextEditingValue(
        text: text, selection: const TextSelection.collapsed(offset: 3), composing: const TextRange(start: 0, end: 5));
    chat.textController.value = middle;
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(middle);
    final end = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
    editable.userUpdateTextEditingValue(end, SelectionChangedCause.keyboard);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(end);
    chat.textFieldFocusNode.unfocus();
    tester.view.viewInsets = const FakeViewPadding();
    await tester.pump(const Duration(milliseconds: 400));
    chat.textFieldFocusNode.requestFocus();
    tester.view.viewInsets = const FakeViewPadding(bottom: 328);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(end);
    // Queued reveal must not overwrite a later draft or run after owner retirement/dispose.
    chat.textController.value = middle;
    fixture.uid = 'different-owner';
    await tester.pump();
    expect(chat.textController.text, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
    expect(tester.takeException(), isNull);
  });

  for (final platform in [TargetPlatform.iOS, TargetPlatform.android]) {
    for (final width in [320.0, 402.0]) {
      for (final scale in [1.0, 3.5]) {
        testWidgets('Native text input keeps caret and draft inside bounded composer $platform $width $scale',
            (tester) async {
          final fixture = _Fixture()..scale = scale;
          await fixture.mount(tester);
          tester.view.physicalSize = Size(width, 874);
          tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
          tester.view.viewPadding = const FakeViewPadding(top: 59, bottom: 34);
          addTearDown(tester.view.resetPadding);
          addTearDown(tester.view.resetViewPadding);
          tester.view.viewInsets = const FakeViewPadding(bottom: 328);
          await tester.pump();
          final chat = fixture.chat(tester);
          chat.textFieldFocusNode.requestFocus();
          await tester.pump(const Duration(milliseconds: 400));
          final fieldFinder = find.descendant(of: find.byType(ChatPage), matching: find.byType(TextField));
          await tester.showKeyboard(fieldFinder);
          final editableFinder = find.descendant(of: fieldFinder, matching: find.byType(EditableText));
          final editable = tester.state<EditableTextState>(editableFinder);
          expect(identical(chat.textFieldFocusNode.context?.findAncestorStateOfType<EditableTextState>(), editable),
              isTrue);
          void assertVisible(TextEditingValue expected) {
            expect(chat.textController.value, expected);
            expect(editable.widget.controller.selection.extentOffset, expected.selection.extentOffset);
            final render = editable.renderEditable;
            final local = render.getLocalRectForCaret(expected.selection.extent);
            final global = local.shift(render.localToGlobal(Offset.zero));
            final ancestorScrollables = find.ancestor(of: editableFinder, matching: find.byType(Scrollable));
            expect(ancestorScrollables, findsAtLeastNWidgets(1), reason: 'footer viewport is included');
            final origin = render.localToGlobal(Offset.zero);
            final boxes = [
              Rect.fromLTWH(origin.dx, origin.dy, render.size.width, render.size.height),
              tester.getRect(fieldFinder),
              for (final element in ancestorScrollables.evaluate())
                tester.getRect(find.byElementPredicate((e) => identical(e, element)))
            ];
            for (final box in boxes) {
              expect(global.left, greaterThanOrEqualTo(box.left - 1), reason: 'caret inside horizontal viewport');
              expect(global.right, lessThanOrEqualTo(box.right + 1), reason: 'caret inside horizontal viewport');
              expect(global.top, greaterThanOrEqualTo(box.top - 1), reason: 'full caret above each viewport clip');
              expect(global.bottom, lessThanOrEqualTo(box.bottom + 1), reason: 'full caret below each viewport clip');
            }
            expect(global.bottom, lessThanOrEqualTo(874 - tester.view.viewInsets.bottom + 1));
            debugPrint('CARET-GLOBAL local=$local global=$global preferredLine=${render.preferredLineHeight} '
                'offset=${render.offset.pixels} extent=${(render.offset as ScrollPosition).maxScrollExtent} boxes=$boxes');
          }

          var text = '';
          for (final char in 'Qqqqqqqqqqqqqqqqqqqqqqqqqz'.split('')) {
            text += char;
            final value = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
            // Deliver the same editing values through Flutter platform-text-input handling.
            tester.testTextInput.updateEditingValue(value);
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 400));
            assertVisible(value);
          }
          final middle = TextEditingValue(
              text: text,
              selection: const TextSelection.collapsed(offset: 3),
              composing: const TextRange(start: 0, end: 5));
          tester.testTextInput.updateEditingValue(middle);
          await tester.pump();
          await tester.pump(const Duration(milliseconds: 400));
          assertVisible(middle);
          final end = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
          editable.userUpdateTextEditingValue(end, SelectionChangedCause.keyboard);
          await tester.pump();
          await tester.pump(const Duration(milliseconds: 400));
          assertVisible(end);
          chat.textFieldFocusNode.unfocus();
          tester.view.viewInsets = const FakeViewPadding();
          await tester.pump(const Duration(milliseconds: 400));
          chat.textFieldFocusNode.requestFocus();
          tester.view.viewInsets = const FakeViewPadding(bottom: 328);
          await tester.pump();
          await tester.pump(const Duration(milliseconds: 400));
          assertVisible(end);
          // Queued reveal must not overwrite a later draft or run after owner retirement/dispose.
          tester.testTextInput.updateEditingValue(middle);
          fixture.uid = 'different-owner';
          await tester.pump();
          expect(chat.textController.text, isEmpty);
          await tester.pumpWidget(const SizedBox.shrink());
          await tester.pump(const Duration(seconds: 1));
          expect(tester.takeException(), isNull);
        }, variant: TargetPlatformVariant.only(platform));
      }
    }
  }

  testWidgets('iOS taller caret stays within editor during native-scale input and refocus', (tester) async {
    final fixture = _Fixture()..scale = 3.125;
    await fixture.mount(tester);
    tester.view.physicalSize = const Size(402, 874);
    tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
    tester.view.viewPadding = const FakeViewPadding(top: 59, bottom: 34);
    addTearDown(tester.view.resetPadding);
    addTearDown(tester.view.resetViewPadding);
    tester.view.viewInsets = const FakeViewPadding(bottom: 328);
    await tester.pump();
    final chat = fixture.chat(tester);
    chat.textFieldFocusNode.requestFocus();
    await tester.pump(const Duration(milliseconds: 400));
    final fieldFinder = find.descendant(of: find.byType(ChatPage), matching: find.byType(TextField));
    final editableFinder = find.descendant(of: fieldFinder, matching: find.byType(EditableText));
    final editable = tester.state<EditableTextState>(editableFinder);
    expect(identical(chat.textFieldFocusNode.context?.findAncestorStateOfType<EditableTextState>(), editable), isTrue);
    void assertVisible(TextEditingValue expected) {
      expect(chat.textController.value, expected);
      expect(editable.widget.controller.selection.extentOffset, expected.selection.extentOffset);
      final render = editable.renderEditable;
      final local = render.getLocalRectForCaret(expected.selection.extent);
      final global = local.shift(render.localToGlobal(Offset.zero));
      expect(render.preferredLineHeight, closeTo(74, 0.1));
      expect(local.height, closeTo(76, 0.1));
      final ancestorScrollables = find.ancestor(of: editableFinder, matching: find.byType(Scrollable));
      expect(ancestorScrollables, findsAtLeastNWidgets(1), reason: 'footer viewport is included');
      final origin = render.localToGlobal(Offset.zero);
      final boxes = [
        Rect.fromLTWH(origin.dx, origin.dy, render.size.width, render.size.height),
        tester.getRect(fieldFinder),
        for (final element in ancestorScrollables.evaluate())
          tester.getRect(find.byElementPredicate((e) => identical(e, element)))
      ];
      for (final box in boxes) {
        expect(global.left, greaterThanOrEqualTo(box.left - 1), reason: 'caret inside horizontal viewport');
        expect(global.right, lessThanOrEqualTo(box.right + 1), reason: 'caret inside horizontal viewport');
        expect(global.top, greaterThanOrEqualTo(box.top - 1), reason: 'full caret above each viewport clip');
        expect(global.bottom, lessThanOrEqualTo(box.bottom + 1), reason: 'full caret below each viewport clip');
      }
      expect(global.bottom, lessThanOrEqualTo(874 - tester.view.viewInsets.bottom + 1));
      debugPrint('CARET-GLOBAL local=$local global=$global preferredLine=${render.preferredLineHeight} '
          'offset=${render.offset.pixels} extent=${(render.offset as ScrollPosition).maxScrollExtent} boxes=$boxes');
    }

    var text = '';
    for (final char in 'Qqqqqqqqqqqqqqqqqqqqqqqqqz'.split('')) {
      text += char;
      final value = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
      // Controller-driven/semantic delivery reproduces the actual missed reveal path.
      chat.textController.value = value;
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      assertVisible(value);
    }
    final middle = TextEditingValue(
        text: text, selection: const TextSelection.collapsed(offset: 3), composing: const TextRange(start: 0, end: 5));
    chat.textController.value = middle;
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(middle);
    final end = TextEditingValue(text: text, selection: TextSelection.collapsed(offset: text.length));
    editable.userUpdateTextEditingValue(end, SelectionChangedCause.keyboard);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(end);
    chat.textFieldFocusNode.unfocus();
    tester.view.viewInsets = const FakeViewPadding();
    await tester.pump(const Duration(milliseconds: 400));
    chat.textFieldFocusNode.requestFocus();
    tester.view.viewInsets = const FakeViewPadding(bottom: 328);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    assertVisible(end);
    // Queued reveal must not overwrite a later draft or run after owner retirement/dispose.
    chat.textController.value = middle;
    fixture.uid = 'different-owner';
    await tester.pump();
    expect(chat.textController.text, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
    expect(tester.takeException(), isNull);
  }, variant: TargetPlatformVariant.only(TargetPlatform.iOS));

  testWidgets('Null or mismatched owner cannot send a local draft', (tester) async {
    final fixture = _Fixture();
    await fixture.mount(tester);
    for (final uid in ['', 'different-auth-owner']) {
      fixture.uid = 'owner-a';
      fixture.messages.notifyListeners();
      await tester.pump();
      final chat = fixture.chat(tester);
      chat.textController.text = 'Unsent synthetic text';
      await tester.pump();
      final arrow = find.byWidgetPredicate((w) => w is FaIcon && w.icon == FontAwesomeIcons.arrowUp);
      final staleSend =
          tester.widget<GestureDetector>(find.ancestor(of: arrow, matching: find.byType(GestureDetector)).first).onTap!;
      final count = fixture.messages.messages.length;
      fixture.uid = uid;
      // Identity changed immediately before input; no auth-stream or frame notification required.
      staleSend();
      await tester.pump();
      expect(chat.textController.text, isEmpty);
      expect(fixture.messages.messages.length, count);
      expect(fixture.messages.sendingMessage, isFalse);
    }
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });

  testWidgets('Queued notification text cannot publish into a replacement conversation/account', (tester) async {
    final fixture = _Fixture()..autoMessage = 'Old synthetic notification';
    await fixture.mount(tester);
    fixture.conversation = 'replacement-conversation';
    fixture.uid = 'replacement-owner';
    SharedPreferencesUtil().uid = fixture.uid;
    await fixture.mount(tester);
    await tester.pump(const Duration(milliseconds: 900));
    expect(fixture.messages.messages.any((m) => m.text == 'Old synthetic notification'), isFalse);
    expect(fixture.chat(tester).textController.text, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 1));
  });
}

class _Fixture {
  String uid = 'owner-a';
  final conversationNotifier = ValueNotifier('conversation-a');
  String get conversation => conversationNotifier.value;
  set conversation(String value) => conversationNotifier.value = value;
  double scale = 1;
  String? autoMessage;
  final home = HomeProvider()..setIndex(1);
  final messages = _Messages()
    ..hasCachedMessages = true
    ..messages = [_message('local-fixture', 'Synthetic local history', MessageSender.human)];
  final apps = AppProvider();
  final connection = ConnectivityProvider();
  final recorder = _Recorder();
  final conversations = ConversationProvider();
  final integrations = IntegrationProvider();
  bool initialized = false;
  bool useDictationIdentity = false;
  CaptureProvider? capture;

  ChatPageState chat(WidgetTester tester) => tester.state<ChatPageState>(find.byType(ChatPage));
  Future<void> mount(WidgetTester tester) async {
    if (useDictationIdentity && tester.binding.lifecycleState != AppLifecycleState.resumed) {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    }
    if (!initialized) {
      initialized = true;
      SharedPreferencesUtil().uid = uid;
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
    }
    await tester.pumpWidget(MultiProvider(
        providers: [
          if (capture != null) ChangeNotifierProvider<CaptureProvider>.value(value: capture!),
          ChangeNotifierProvider<HomeProvider>.value(value: home),
          ChangeNotifierProvider<MessageProvider>.value(value: messages),
          ChangeNotifierProvider<AppProvider>.value(value: apps),
          ChangeNotifierProvider<ConnectivityProvider>.value(value: connection),
          ChangeNotifierProvider<VoiceRecorderProvider>.value(value: recorder),
          ChangeNotifierProvider<ConversationProvider>.value(value: conversations),
          ChangeNotifierProvider<IntegrationProvider>.value(value: integrations),
        ],
        child: MaterialApp(
            theme: ellaDarkThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
                data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                child: child!),
            home: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
              const SizedBox.shrink(),
              ValueListenableBuilder<String>(
                  valueListenable: conversationNotifier,
                  builder: (_, conversation, __) => ChatPage(
                      isPivotBottom: true,
                      draftConversationId: conversation,
                      draftAuthenticatedUid: () => uid,
                      dictationAuthenticatedUid: useDictationIdentity ? () => uid : null,
                      autoMessage: autoMessage)),
              const SizedBox.shrink(),
              const SizedBox.shrink()
            ]))));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
  }
}

class _FreshCapture extends CaptureProvider {
  bool content = false;
  int stops = 0;
  @override
  Future<void> stopForAccountTransition() async {
    stops++;
  }

  @override
  bool get isCaptureQuiescentForDiagnostic => true;
  @override
  bool get hasUnfinalizedPhoneCaptureContent => content;
  @override
  bool get hasCapturableContent => false;
  @override
  bool get havingRecordingDevice => false;
}

Future<void> _seedDictationAuthority(String uid) async {
  final p = SharedPreferencesUtil();
  const receipt = 'aicr_fictional_dictation';
  p.acceptAiConsent(
      uid: uid,
      receiptId: receipt,
      profileBindingId: 'fictional-profile',
      serverDecidedAt: DateTime.now().toUtc().toIso8601String(),
      locale: 'en');
  p.markAiConsentServerVerified(
      uid: uid,
      receiptId: receipt,
      profileBindingId: 'fictional-profile',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash);
  await p.saveEllaProvisioningReceipt(uid, {
    'state': 'ready',
    'binding_state': 'active',
    'binding_revision': 1,
    'effective_policy_revision': 'fictional-policy'
  });
  await p.markEllaProvisioningVerified(uid);
}
