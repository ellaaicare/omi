import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/app_provider.dart';
import '../fixtures/test_data.dart';

void main() {
  group('Chat Widget Tests', () {
    late MessageProvider messageProvider;
    late AppProvider appProvider;

    setUp(() {
      messageProvider = MessageProvider();
      appProvider = AppProvider();
      messageProvider.updateAppProvider(appProvider);
      messageProvider.messages = TestMessages.createTestMessagesList();
    });

    tearDown(() {
      messageProvider.dispose();
      appProvider.dispose();
    });

    testWidgets('should display messages in chat', (WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: MultiProvider(
            providers: [
              ChangeNotifierProvider<MessageProvider>.value(value: messageProvider),
              ChangeNotifierProvider<AppProvider>.value(value: appProvider),
            ],
            child: Scaffold(
              body: Consumer<MessageProvider>(
                builder: (context, provider, child) {
                  return ListView.builder(
                    reverse: true,
                    itemCount: provider.messages.length,
                    itemBuilder: (context, index) {
                      final message = provider.messages[index];
                      return ListTile(
                        key: Key('message-${message.id}'),
                        title: Text(message.text),
                      );
                    },
                  );
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byType(ListTile), findsWidgets);
    });

    testWidgets('should show typing indicator when loading', (WidgetTester tester) async {
      messageProvider.setShowTypingIndicator(true);

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<MessageProvider>.value(
            value: messageProvider,
            child: Scaffold(
              body: Consumer<MessageProvider>(
                builder: (context, provider, child) {
                  return Column(
                    children: [
                      if (provider.showTypingIndicator)
                        const LinearProgressIndicator(
                          key: Key('typing-indicator'),
                        ),
                      const Expanded(child: SizedBox.shrink()),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('typing-indicator')), findsOneWidget);
    });

    testWidgets('should allow message input', (WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<MessageProvider>.value(
            value: messageProvider,
            child: Scaffold(
              body: Column(
                children: [
                  const Expanded(child: SizedBox.shrink()),
                  TextField(
                    key: const Key('message-input'),
                    onSubmitted: (text) {
                      messageProvider.addMessageLocally(text);
                    },
                  ),
                ],
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Enter message
      await tester.enterText(find.byKey(const Key('message-input')), 'Test message');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(messageProvider.messages.length, greaterThan(3));
    });
  });
}
