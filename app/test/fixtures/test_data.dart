import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';

/// Test fixtures for BLE devices
class TestDevices {
  static BtDevice createTestDevice({
    String id = 'test-device-001',
    String name = 'Test Omi Device',
    int rssi = -50,
  }) {
    return BtDevice(
      id: id,
      name: name,
      rssi: rssi,
      type: DeviceType.omi,
      firmwareRevision: '1.0.0',
      hardwareRevision: '1.0',
      modelNumber: 'OMI-001',
      manufacturerName: 'Omi',
    );
  }

  static BtDevice createEmptyDevice() {
    return BtDevice.empty();
  }
}

/// Test fixtures for Apps
class TestApps {
  static App createTestApp({
    String id = 'test-app-001',
    String name = 'Test App',
    bool enabled = false,
    bool private = false,
    String? uid,
  }) {
    return App(
      id: id,
      name: name,
      author: 'Test Author',
      description: 'Test app description',
      image: '',
      capabilities: ['chat', 'memories'],
      enabled: enabled,
      chatPrompt: 'Test chat prompt',
      conversationPrompt: 'Test conversation prompt',
      uid: uid,
      private: private,
      deleted: false,
      approved: true,
      category: 'productivity',
    );
  }

  static List<App> createTestAppsList() {
    return [
      createTestApp(id: 'app-1', name: 'App 1', enabled: true),
      createTestApp(id: 'app-2', name: 'App 2', enabled: false),
      createTestApp(id: 'app-3', name: 'App 3', enabled: true, private: true),
    ];
  }
}

/// Test fixtures for Messages
class TestMessages {
  static ServerMessage createTestMessage({
    String? id,
    String text = 'Test message',
    MessageSender sender = MessageSender.human,
    String? appId,
  }) {
    return ServerMessage(
      id ?? 'msg-${DateTime.now().millisecondsSinceEpoch}',
      DateTime.now(),
      text,
      sender,
      MessageType.text,
      appId,
      false,
      [],
      [],
      [],
    );
  }

  static List<ServerMessage> createTestMessagesList() {
    return [
      createTestMessage(text: 'Hello', sender: MessageSender.human),
      createTestMessage(text: 'Hi there!', sender: MessageSender.ai),
      createTestMessage(text: 'How are you?', sender: MessageSender.human),
    ];
  }
}

/// Test fixtures for Conversations
class TestConversations {
  static ServerConversation createTestConversation({
    String? id,
    String title = 'Test Conversation',
    ConversationStatus status = ConversationStatus.completed,
  }) {
    return ServerConversation(
      id: id ?? 'conv-${DateTime.now().millisecondsSinceEpoch}',
      createdAt: DateTime.now(),
      structured: Structured(title, 'Test conversation overview'),
      status: status,
      transcriptSegments: [],
      photos: [],
    );
  }

  static List<ServerConversation> createTestConversationsList() {
    return [
      createTestConversation(title: 'Conversation 1'),
      createTestConversation(title: 'Conversation 2', status: ConversationStatus.processing),
      createTestConversation(title: 'Conversation 3'),
    ];
  }
}
