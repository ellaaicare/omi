import 'package:mockito/mockito.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/providers/usage_provider.dart';

// Mock Providers
class MockCaptureProvider extends Mock implements CaptureProvider {}

class MockDeviceProvider extends Mock implements DeviceProvider {}

class MockAppProvider extends Mock implements AppProvider {}

class MockMessageProvider extends Mock implements MessageProvider {}

class MockConversationProvider extends Mock implements ConversationProvider {}

class MockPeopleProvider extends Mock implements PeopleProvider {}

class MockUsageProvider extends Mock implements UsageProvider {}
