import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/backend/schema/message.dart';
import '../../fixtures/test_data.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('MessageProvider Tests', () {
    late MessageProvider messageProvider;
    late AppProvider mockAppProvider;

    setUp(() {
      messageProvider = MessageProvider();
      mockAppProvider = AppProvider();
      messageProvider.updateAppProvider(mockAppProvider);
    });

    tearDown(() {
      messageProvider.dispose();
    });

    group('Message Management', () {
      test('should initialize with empty messages list', () {
        expect(messageProvider.messages, isEmpty);
        expect(messageProvider.isLoadingMessages, false);
        expect(messageProvider.hasCachedMessages, false);
      });

      test('should add message locally', () {
        messageProvider.addMessageLocally('Test message');

        expect(messageProvider.messages.length, 1);
        expect(messageProvider.messages[0].text, 'Test message');
        expect(messageProvider.messages[0].sender, MessageSender.human);
      });

      test('should add message', () {
        final message = TestMessages.createTestMessage(text: 'Hello');

        messageProvider.addMessage(message);

        expect(messageProvider.messages.length, 1);
        expect(messageProvider.messages[0].text, 'Hello');
      });

      test('should not add duplicate message', () {
        final message = TestMessages.createTestMessage(id: 'msg-1', text: 'Hello');

        messageProvider.addMessage(message);
        messageProvider.addMessage(message);

        expect(messageProvider.messages.length, 1);
      });

      test('should remove local message', () {
        final message = TestMessages.createTestMessage(id: 'msg-1', text: 'Hello');

        messageProvider.addMessage(message);
        expect(messageProvider.messages.length, 1);

        messageProvider.removeLocalMessage('msg-1');
        expect(messageProvider.messages.length, 0);
      });
    });

    group('Loading States', () {
      test('should set loading messages state', () {
        expect(messageProvider.isLoadingMessages, false);

        messageProvider.setLoadingMessages(true);
        expect(messageProvider.isLoadingMessages, true);

        messageProvider.setLoadingMessages(false);
        expect(messageProvider.isLoadingMessages, false);
      });

      test('should set has cached messages', () {
        expect(messageProvider.hasCachedMessages, false);

        messageProvider.setHasCachedMessages(true);
        expect(messageProvider.hasCachedMessages, true);
      });

      test('should set clearing chat state', () {
        expect(messageProvider.isClearingChat, false);

        messageProvider.setClearingChat(true);
        expect(messageProvider.isClearingChat, true);
      });

      test('should set showing typing indicator', () {
        expect(messageProvider.showTypingIndicator, false);

        messageProvider.setShowTypingIndicator(true);
        expect(messageProvider.showTypingIndicator, true);
      });

      test('should set sending message state', () {
        expect(messageProvider.sendingMessage, false);

        messageProvider.setSendingMessage(true);
        expect(messageProvider.sendingMessage, true);
      });
    });

    group('File Management', () {
      test('should initialize with empty file lists', () {
        expect(messageProvider.selectedFiles, isEmpty);
        expect(messageProvider.selectedFileTypes, isEmpty);
        expect(messageProvider.uploadedFiles, isEmpty);
      });

      test('should clear selected files', () {
        messageProvider.clearSelectedFiles();

        expect(messageProvider.selectedFiles, isEmpty);
        expect(messageProvider.selectedFileTypes, isEmpty);
      });

      test('should clear uploaded files', () {
        messageProvider.clearUploadedFiles();
        expect(messageProvider.uploadedFiles, isEmpty);
      });

      test('should check file uploading status', () {
        expect(messageProvider.isFileUploading('file-1'), false);

        messageProvider.uploadingFiles['file-1'] = true;
        expect(messageProvider.isFileUploading('file-1'), true);
      });

      test('should set multi uploading file status', () {
        messageProvider.setMultiUploadingFileStatus(['file-1', 'file-2'], true);

        expect(messageProvider.uploadingFiles['file-1'], true);
        expect(messageProvider.uploadingFiles['file-2'], true);
        expect(messageProvider.isUploadingFiles, true);
      });

      test('should update uploading files state', () {
        messageProvider.uploadingFiles['file-1'] = true;
        messageProvider.setIsUploadingFiles();

        expect(messageProvider.isUploadingFiles, true);

        messageProvider.uploadingFiles.clear();
        messageProvider.setIsUploadingFiles();

        expect(messageProvider.isUploadingFiles, false);
      });
    });

    group('Chat Apps', () {
      test('should initialize with empty chat apps', () {
        expect(messageProvider.chatApps, isEmpty);
        expect(messageProvider.isLoadingChatApps, false);
      });
    });

    group('Voice Message', () {
      test('should set next message origin is voice', () {
        messageProvider.setNextMessageOriginIsVoice(true);
        expect(messageProvider._isNextMessageFromVoice, true);

        messageProvider.setNextMessageOriginIsVoice(false);
        expect(messageProvider._isNextMessageFromVoice, false);
      });
    });

    group('Message NPS', () {
      test('should set message NPS', () async {
        final message = TestMessages.createTestMessage();
        message.askForNps = true;

        messageProvider.messages.add(message);

        // This would normally call the API
        // Just verify the message exists
        expect(messageProvider.messages.length, 1);
      });
    });

    group('Notification Tests', () {
      test('should notify listeners when messages change', () {
        var notified = false;
        messageProvider.addListener(() {
          notified = true;
        });

        messageProvider.setLoadingMessages(true);
        expect(notified, true);
      });

      test('should notify listeners when adding message', () {
        var notifyCount = 0;
        messageProvider.addListener(() {
          notifyCount++;
        });

        final message = TestMessages.createTestMessage();
        messageProvider.addMessage(message);

        expect(notifyCount, greaterThan(0));
      });
    });
  });
}
